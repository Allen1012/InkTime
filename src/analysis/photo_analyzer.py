"""供批量脚本和后台工作进程共同调用的单张照片分析编排。"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from src.server.model_providers import resolve_endpoint

from . import analyze_photos_docker as legacy

_LEGACY_CONFIGURATION_LOCK = threading.RLock()
_LEGACY_CONFIGURATION_KEYS = (
    "API_URL",
    "API_BASE_URL",
    "MODEL_NAME",
    "API_KEY",
    "TIMEOUT",
    "VLM_MAX_LONG_EDGE",
    "WORLD_CITIES_CSV",
    "CITY_GRID_DEG",
    "CITY_MAX_DISTANCE_KM",
    "HOME_LAT",
    "HOME_LON",
    "HOME_RADIUS_KM",
)


class NarrationGenerationError(RuntimeError):
    """表示旁白生成失败，整次分析不得写入部分结果。"""


def _optional_int(value: Any) -> int | None:
    """把可选 EXIF 值转换为整数，无法转换时返回空值。"""
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    """把可选 EXIF 值转换为浮点数，无法转换时返回空值。"""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError, OverflowError):
        return None


@contextmanager
def _temporary_legacy_configuration(
    settings: Mapping[str, Any] | None,
    api_key: str | None,
    provider: Mapping[str, Any] | None = None,
) -> Iterator[None]:
    """在重入锁内临时覆盖旧分析模块配置并完整恢复全局状态。

    Args:
        settings: 任务固化的 analysis 作用域配置；为空时保持旧环境行为。
        api_key: 执行时现读的模型密钥，不进入任务快照。
        provider: 可选认领时固化的公开厂商执行参数；为空时使用旧 settings。

    Yields:
        配置覆盖有效期间的执行上下文。
    """
    with _LEGACY_CONFIGURATION_LOCK:
        if settings is None:
            yield
            return
        previous = {key: getattr(legacy, key) for key in _LEGACY_CONFIGURATION_KEYS}
        previous_cities = legacy._CITY_CACHE_CITIES
        previous_grid = legacy._CITY_CACHE_GRID
        # 与批量脚本共用同一套定位顺序（显式配置 -> data -> resources）：
        # 配置留空时自动回退到随代码分发的那份，不必逼用户填路径
        city_path = legacy.resolve_city_index_path(settings["WORLD_CITIES_CSV"])
        provider_url = str(provider["base_url"]) if provider else str(settings["API_URL"])
        provider_model = str(provider["model_name"]) if provider else str(settings["MODEL_NAME"])
        provider_timeout = float(provider["timeout_seconds"]) if provider else float(settings["TIMEOUT"])
        provider_edge = int(provider["max_long_edge"]) if provider else int(settings["VLM_MAX_LONG_EDGE"])
        endpoint = resolve_endpoint(provider_url)
        base_url = (
            provider_url.rstrip("/")
            if not provider_url.rstrip("/").endswith("/chat/completions")
            else provider_url.rstrip("/")[:-len("/chat/completions")]
        )
        overrides = {
            "API_URL": endpoint,
            "API_BASE_URL": base_url,
            "MODEL_NAME": provider_model,
            "API_KEY": api_key or "",
            "TIMEOUT": provider_timeout,
            "VLM_MAX_LONG_EDGE": provider_edge,
            "WORLD_CITIES_CSV": city_path,
            "CITY_GRID_DEG": float(settings["CITY_GRID_DEG"]),
            "CITY_MAX_DISTANCE_KM": float(settings["CITY_MAX_DISTANCE_KM"]),
            "HOME_LAT": float(settings["HOME_LAT"]),
            "HOME_LON": float(settings["HOME_LON"]),
            "HOME_RADIUS_KM": float(settings["HOME_RADIUS_KM"]),
        }
        try:
            for key, value in overrides.items():
                setattr(legacy, key, value)
            legacy._CITY_CACHE_CITIES = None
            legacy._CITY_CACHE_GRID = None
            yield
        finally:
            for key, value in previous.items():
                setattr(legacy, key, value)
            legacy._CITY_CACHE_CITIES = previous_cities
            legacy._CITY_CACHE_GRID = previous_grid


def generate_narration(
    image_path: Path,
    *,
    settings: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    provider: Mapping[str, Any] | None = None,
    image_b64: str | None = None,
) -> str:
    """为单张照片生成必需的中文旁白，可使用隔离的任务配置。

    Args:
        image_path: 已验证且可读取的照片路径。
        settings: 可选任务配置；为空时保持旧模块环境配置。
        api_key: 执行时现读的模型密钥，不会持久化到任务。
        provider: 可选认领时固化的旁白厂商公开参数。
        image_b64: 可选的已编码图片，由同一次分析的评分环节复用；省略时在旧
            模块内自行编码，独立的旁白重写任务走这条路径。

    Returns:
        去除首尾空白后的旁白。

    Raises:
        NarrationGenerationError: 模型没有返回有效旁白。
    """
    with _temporary_legacy_configuration(settings, api_key, provider):
        narration = legacy.generate_side_caption(Path(image_path), image_b64)
    if not narration or not narration.strip():
        raise NarrationGenerationError("narration_generation_failed")
    return narration.strip()


def analyze_single_photo(
    image_path: Path,
    *,
    city_resolver: Callable[[float | None, float | None], str] | None = None,
    settings: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    provider: Mapping[str, Any] | None = None,
    narration_provider: Mapping[str, Any] | None = None,
    narration_api_key: str | None = None,
    original_filename: str | None = None,
) -> dict[str, Any]:
    """完成单张照片评分、EXIF、城市解析和旁白生成，不执行数据库写入。

    图片处理和模型调用均在调用方事务外发生；任务配置在模块级重入锁内临时应用，
    以阻止未来同进程并行调用污染旧模块全局值。旁白失败会抛出异常，避免部分写入。

    Args:
        image_path: 已存在的照片文件路径。
        city_resolver: 可选城市解析器；省略时复用批量脚本的缓存解析器。
        settings: 可选任务配置；为空时保持旧直接调用兼容。
        api_key: 执行时现读的分析厂商密钥，不会持久化到任务。
        provider: 可选认领时固化的分析厂商公开参数。
        narration_provider: 可选认领时固化的旁白厂商公开参数；省略时跟随分析厂商。
        narration_api_key: 执行时现读的旁白厂商密钥。
        original_filename: 可选原始文件名，用于上传重命名后的拍摄日期兜底。

    Returns:
        可直接交给批量入口或后台任务仓储写入的规范字段字典。
    """
    path = Path(image_path)
    with _temporary_legacy_configuration(settings, api_key, provider):
        # 编码必须在配置覆盖生效期内完成：缩放长边取自任务快照的
        # VLM_MAX_LONG_EDGE。评分与旁白共用这一份结果，同一张照片只做一次
        # 解码、旋转矫正、缩放与重编码。
        try:
            image_b64 = legacy.encode_image_to_b64(path)
        except Exception as error:
            raise RuntimeError(f"读取图片失败：{error}") from error
        model_result, exif_info = legacy.call_vlm(path, image_b64)
        effective_narration_provider = narration_provider or provider
        narration_uses_analysis_image = (
            provider is None and effective_narration_provider is None
        ) or (
            provider is not None
            and effective_narration_provider is not None
            and effective_narration_provider.get("name") == provider.get("name")
        )
        narration = generate_narration(
            path,
            settings=settings,
            provider=effective_narration_provider,
            api_key=(narration_api_key if narration_provider else api_key),
            image_b64=image_b64 if narration_uses_analysis_image else None,
        )
        exif_datetime, date_source = legacy.resolve_datetime(
            path, exif_info.get("datetime"), original_filename=original_filename
        )
        exif_info["datetime"] = exif_datetime
        exif_info["date_source"] = date_source
        latitude = _optional_float(exif_info.get("gps_lat"))
        longitude = _optional_float(exif_info.get("gps_lon"))
        resolver = city_resolver or legacy.get_city_resolver()
        city = resolver(latitude, longitude) if latitude is not None and longitude is not None else ""
        try:
            memory_score = float(model_result.get("memory_score", 0.0))
        except (TypeError, ValueError):
            memory_score = 0.0
        if latitude is not None and longitude is not None and not legacy.in_home(latitude, longitude):
            memory_score = min(memory_score + 5.0, 100.0)
        try:
            beauty_score = float(model_result.get("beauty_score", 0.0))
        except (TypeError, ValueError):
            beauty_score = 0.0
        return {
            "caption": str(model_result.get("caption", "")).strip(),
            "type": legacy.normalize_type(model_result.get("type")),
            "memory_score": memory_score,
            "beauty_score": beauty_score,
            "reason": str(model_result.get("reason", "")).strip(),
            "width": _optional_int(exif_info.get("width")),
            "height": _optional_int(exif_info.get("height")),
            "orientation": exif_info.get("orientation"),
            "exif_json": json.dumps(exif_info, ensure_ascii=False, default=str),
            "raw_json": None,
            "exif_datetime": exif_datetime,
            "exif_make": exif_info.get("make"),
            "exif_model": exif_info.get("model"),
            "exif_iso": _optional_int(exif_info.get("iso")),
            "exif_exposure_time": _optional_float(exif_info.get("exposure_time")),
            "exif_f_number": _optional_float(exif_info.get("f_number")),
            "exif_focal_length": _optional_float(exif_info.get("focal_length")),
            "exif_gps_lat": latitude,
            "exif_gps_lon": longitude,
            "exif_gps_alt": _optional_float(exif_info.get("gps_alt")),
            "side_caption": narration,
            "exif_city": city,
            "date_source": date_source,
        }
