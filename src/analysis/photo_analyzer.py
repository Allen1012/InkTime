"""供批量脚本和后台工作进程共同调用的单张照片分析编排。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import analyze_photos_docker as legacy


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


def generate_narration(image_path: Path) -> str:
    """为单张照片生成必需的中文旁白。

    Args:
        image_path: 已验证且可读取的照片路径。

    Returns:
        去除首尾空白后的旁白。

    Raises:
        NarrationGenerationError: 模型没有返回有效旁白。
    """
    narration = legacy.generate_side_caption(Path(image_path))
    if not narration or not narration.strip():
        raise NarrationGenerationError("narration_generation_failed")
    return narration.strip()


def analyze_single_photo(
    image_path: Path,
    *,
    city_resolver: Callable[[float | None, float | None], str] | None = None,
) -> dict[str, Any]:
    """完成单张照片评分、EXIF、城市解析和旁白生成，不执行数据库写入。

    图片处理和模型调用均在调用方事务外发生；旁白失败会抛出异常，避免调用方写入
    只有评分没有旁白的部分结果。

    Args:
        image_path: 已存在的照片文件路径。
        city_resolver: 可选城市解析器；省略时复用批量脚本的缓存解析器。

    Returns:
        可直接交给批量入口或后台任务仓储写入的规范字段字典。
    """
    path = Path(image_path)
    model_result, exif_info = legacy.call_vlm(path)
    narration = generate_narration(path)
    exif_datetime, date_source = legacy.resolve_datetime(path, exif_info.get("datetime"))
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
