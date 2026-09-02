"""供批量脚本和后台工作进程共同调用的单张照片分析编排。"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from src.provider_fallback import fallback_reason
from src.server.model_providers import resolve_endpoint

from . import analyze_photos_docker as legacy

_LEGACY_CONFIGURATION_LOCK = threading.RLock()
_LEGACY_CONFIGURATION_KEYS = (
    "API_URL", "API_BASE_URL", "MODEL_NAME", "API_KEY", "TIMEOUT",
    "VLM_MAX_LONG_EDGE", "PROVIDER_REQUEST_OPTIONS", "WORLD_CITIES_CSV",
    "CITY_GRID_DEG", "CITY_MAX_DISTANCE_KM", "HOME_LAT", "HOME_LON",
    "HOME_RADIUS_KM",
)
FallbackCallback = Callable[[str, str, Mapping[str, Any], Mapping[str, Any]], None]


class NarrationGenerationError(RuntimeError):
    """表示旁白生成失败，整次分析不得写入部分结果。"""


class NoModelProviderError(RuntimeError):
    """表示没有可用的模型厂商档案，无法发起模型请求。

    单独成类而不是复用 RuntimeError：这是配置问题而不是模型故障，不该被降级逻辑
    当成"换一家再试"的理由——整条候选链都空的时候，换谁都一样。
    """


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


def _runtime_chain(
    chain: Sequence[Mapping[str, Any]] | None,
    provider: Mapping[str, Any] | None,
    api_key: str | None,
) -> list[dict[str, Any]]:
    """把候选链或单厂商参数统一为带运行时密钥的非空执行列表。

    两者都为空时不再返回一个"空档案候选"占位。那个占位是兜底时代的产物：它让调用方
    一路走到发请求才因缺字段失败，报出来的是 KeyError 而不是"你没配厂商"。

    Raises:
        NoModelProviderError: 既没有候选链也没有单厂商参数。
    """
    if chain:
        return [dict(candidate) for candidate in chain]
    if provider is not None:
        candidate = dict(provider)
        candidate.setdefault("api_key", api_key or "")
        return [candidate]
    raise NoModelProviderError(
        "没有可用的模型厂商：请在后台「模型厂商」页建档，"
        "并在配置管理页把 ANALYSIS_PROVIDER 等用途路由指向该档案名称"
    )


def _candidate_key(provider: Mapping[str, Any]) -> tuple[Any, ...]:
    """构造图片编码复用键，同厂商同边长才能共享已编码图片。"""
    return (
        provider.get("id"), provider.get("name"), provider.get("max_long_edge")
    )


def _candidate_name(provider: Mapping[str, Any]) -> str:
    """返回仅供结构化日志使用的厂商名，旧配置路径使用 legacy。"""
    return str(provider.get("name") or "legacy")


def _notify_fallback(
    callback: FallbackCallback | None,
    purpose: str,
    reason: str,
    current: Mapping[str, Any],
    following: Mapping[str, Any],
) -> None:
    """在确实存在下一候选时通知工作线程记录一次实际转向。"""
    if callback is not None:
        callback(purpose, reason, current, following)


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
        city_path = legacy.resolve_city_index_path(settings["WORLD_CITIES_CSV"])
        # 没有厂商档案就没有模型可用：注册表里那套兜底键已经移除，这里必须明确失败。
        # 早先的静默回退是个陷阱——路由填错或档案被删时分析照样"成功"，只是悄悄换了
        # 一套配置，页面上看不出任何异常，等发现时已经烧了一批额度。
        if not provider or not provider.get("base_url"):
            raise NoModelProviderError(
                "没有可用的模型厂商：请在后台「模型厂商」页建档，"
                "并在配置管理页把 ANALYSIS_PROVIDER 等用途路由指向该档案名称"
            )
        provider_url = str(provider["base_url"])
        provider_model = str(provider["model_name"])
        provider_timeout = float(provider["timeout_seconds"])
        provider_edge = int(provider["max_long_edge"])
        endpoint = resolve_endpoint(provider_url)
        base_url = (
            provider_url.rstrip("/")
            if not provider_url.rstrip("/").endswith("/chat/completions")
            else provider_url.rstrip("/")[:-len("/chat/completions")]
        )
        # 厂商特有的额外请求参数随候选一起覆盖：它和地址、模型一样是「这次用哪家」的
        # 一部分，必须跟着候选切换，不能留成上一个候选的值。
        provider_options = (
            dict(provider.get("request_options") or {}) if provider else {}
        )
        overrides = {
            "API_URL": endpoint,
            "API_BASE_URL": base_url,
            "MODEL_NAME": provider_model,
            "API_KEY": api_key or "",
            "TIMEOUT": provider_timeout,
            "VLM_MAX_LONG_EDGE": provider_edge,
            "PROVIDER_REQUEST_OPTIONS": provider_options,
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


def _encode_for_candidate(
    path: Path,
    settings: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
    cache: dict[tuple[Any, ...], str],
) -> str:
    """按候选厂商图片边长编码，并只复用同厂商同边长结果。"""
    key = _candidate_key(candidate)
    if key in cache:
        return cache[key]
    provider = candidate if candidate.get("name") else None
    with _temporary_legacy_configuration(settings, str(candidate.get("api_key") or ""), provider):
        try:
            encoded = legacy.encode_image_to_b64(path)
        except Exception as error:
            raise RuntimeError(f"读取图片失败：{error}") from error
    cache[key] = encoded
    return encoded


def generate_narration(
    image_path: Path,
    *,
    settings: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    provider: Mapping[str, Any] | None = None,
    image_b64: str | None = None,
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
    on_provider_fallback: FallbackCallback | None = None,
) -> str:
    """为单张照片逐候选生成必需旁白，同时兼容旧单厂商参数。

    Args:
        image_path: 已验证且可读取的照片路径。
        settings: 可选任务配置；为空时保持旧模块环境配置。
        api_key: 旧单厂商路径的运行时密钥。
        provider: 旧单厂商公开参数。
        image_b64: 可选的已编码图片，仅供旧单候选或明确同编码配置复用。
        provider_chain: 带运行时密钥的有序旁白候选链。
        on_provider_fallback: 实际转向下一候选前的回调。

    Returns:
        去除首尾空白后的旁白。

    Raises:
        NarrationGenerationError: 模型没有返回有效旁白。
    """
    candidates = _runtime_chain(provider_chain, provider, api_key)
    for index, candidate in enumerate(candidates):
        public_provider = candidate if candidate.get("name") else None
        reusable = image_b64 if len(candidates) == 1 else None
        try:
            with _temporary_legacy_configuration(
                settings, str(candidate.get("api_key") or ""), public_provider
            ):
                narration = legacy.generate_side_caption(Path(image_path), reusable)
        except Exception as error:
            reason = fallback_reason(error)
            if reason is not None and index + 1 < len(candidates):
                _notify_fallback(
                    on_provider_fallback, "narration", reason,
                    candidate, candidates[index + 1],
                )
                continue
            raise
        if not narration or not narration.strip():
            raise NarrationGenerationError("narration_generation_failed")
        return narration.strip()
    raise NarrationGenerationError("narration_generation_failed")


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
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
    narration_provider_chain: Sequence[Mapping[str, Any]] | None = None,
    on_provider_fallback: FallbackCallback | None = None,
) -> dict[str, Any]:
    """独立遍历评分与旁白候选链并返回完整分析结果。

    评分成功后只遍历旁白链，旁白降级不会重复评分。同厂商同图片边长可复用编码，
    不同厂商按各自快照中的 max_long_edge 重新编码。所有数据库写入仍由调用方完成。

    Args:
        image_path: 已存在的照片文件路径。
        city_resolver: 可选城市解析器。
        settings: 可选任务配置；为空时保持旧直接调用兼容。
        api_key: 旧分析厂商运行时密钥。
        provider: 旧分析厂商公开参数。
        narration_provider: 旧旁白厂商公开参数；省略时跟随成功的分析厂商。
        narration_api_key: 旧旁白厂商运行时密钥。
        original_filename: 上传重命名前的原始文件名。
        provider_chain: 带运行时密钥的有序分析候选链。
        narration_provider_chain: 带运行时密钥的有序旁白候选链。
        on_provider_fallback: 实际转向下一候选前的回调。

    Returns:
        可直接交给后台任务仓储写入的规范字段字典。
    """
    path = Path(image_path)
    analysis_candidates = _runtime_chain(provider_chain, provider, api_key)
    encoded_cache: dict[tuple[Any, ...], str] = {}
    model_result: Mapping[str, Any]
    exif_info: dict[str, Any]
    successful_analysis: Mapping[str, Any] | None = None
    for index, candidate in enumerate(analysis_candidates):
        image_b64 = _encode_for_candidate(path, settings, candidate, encoded_cache)
        public_provider = candidate if candidate.get("name") else None
        try:
            with _temporary_legacy_configuration(
                settings, str(candidate.get("api_key") or ""), public_provider
            ):
                model_result, exif_info = legacy.call_vlm(path, image_b64)
        except Exception as error:
            reason = fallback_reason(error)
            if reason is not None and index + 1 < len(analysis_candidates):
                _notify_fallback(
                    on_provider_fallback, "analysis", reason,
                    candidate, analysis_candidates[index + 1],
                )
                continue
            raise
        successful_analysis = candidate
        break
    else:  # pragma: no cover - _runtime_chain 始终返回非空列表
        raise RuntimeError("analysis_provider_chain_empty")

    if narration_provider_chain:
        narration_candidates = _runtime_chain(narration_provider_chain, None, None)
    elif narration_provider is not None:
        narration_candidates = _runtime_chain(
            None, narration_provider, narration_api_key
        )
    else:
        narration_candidates = [dict(successful_analysis or analysis_candidates[0])]

    narration: str | None = None
    for index, candidate in enumerate(narration_candidates):
        image_b64 = _encode_for_candidate(path, settings, candidate, encoded_cache)
        public_provider = candidate if candidate.get("name") else None
        try:
            with _temporary_legacy_configuration(
                settings, str(candidate.get("api_key") or ""), public_provider
            ):
                narration = legacy.generate_side_caption(path, image_b64)
        except Exception as error:
            reason = fallback_reason(error)
            if reason is not None and index + 1 < len(narration_candidates):
                _notify_fallback(
                    on_provider_fallback, "narration", reason,
                    candidate, narration_candidates[index + 1],
                )
                continue
            raise
        if not narration or not narration.strip():
            raise NarrationGenerationError("narration_generation_failed")
        narration = narration.strip()
        break

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
