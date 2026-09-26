"""照片路径日期候选解析，结果只供人工确认，不直接充当拍摄时间。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Sequence

PATH_DATETIME_CANDIDATE_KEY = "path_datetime_candidate"
SYSTEM_UPLOAD_DIRECTORY_NAME = "uploads"
_MINIMUM_YEAR = 1900
_TIMESTAMP_MINIMUM = 946684800
_TIMESTAMP_MAXIMUM = 4102444800


def _validated_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> str | None:
    """构造经过真实日历和未来日期校验的标准时间文本。"""
    try:
        value = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if year < _MINIMUM_YEAR or value.date() > datetime.now().date():
        return None
    return value.strftime("%Y:%m:%d %H:%M:%S")


def datetime_candidate_from_path_text(text: str) -> str | None:
    """从单个目录名提取完整日期，可选包含时分秒。"""
    for match in re.finditer(r"(?<!\d)(\d{13}|\d{10})(?!\d)", text):
        raw = match.group(1)
        timestamp = int(raw) / 1000.0 if len(raw) == 13 else float(raw)
        if _TIMESTAMP_MINIMUM <= timestamp <= _TIMESTAMP_MAXIMUM:
            value = datetime.fromtimestamp(timestamp)
            candidate = _validated_datetime(
                value.year, value.month, value.day,
                value.hour, value.minute, value.second,
            )
            if candidate:
                return candidate

    patterns = (
        re.compile(
            r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})"
            r"(?:[_-]?(\d{2})(\d{2})(\d{2}))?(?!\d)"
        ),
        re.compile(
            r"(?<!\d)((?:19|20)\d{2})[-_.](\d{1,2})[-_.](\d{1,2})"
            r"(?:[T _-](\d{1,2})[-:.](\d{2})(?:[-:.](\d{2}))?)?(?!\d)"
        ),
        re.compile(
            r"(?<!\d)((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日"
            r"(?:[ _-]?(\d{1,2})时(\d{1,2})分(?:(\d{1,2})秒)?)?(?!\d)"
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            candidate = _validated_datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4) or 0),
                int(match.group(5) or 0),
                int(match.group(6) or 0),
            )
            if candidate:
                return candidate
    return None


def _split_directory_candidate(parts: Sequence[str], start: int) -> str | None:
    """从连续的年、月、日三级目录构造日期候选。"""
    if start + 2 >= len(parts):
        return None
    if not re.fullmatch(r"(?:19|20)\d{2}", parts[start]):
        return None
    if not re.fullmatch(r"\d{1,2}", parts[start + 1]):
        return None
    if not re.fullmatch(r"\d{1,2}", parts[start + 2]):
        return None
    return _validated_datetime(
        int(parts[start]), int(parts[start + 1]), int(parts[start + 2])
    )


def path_datetime_candidate(
    path: str | Path,
    image_roots: Sequence[str | Path],
) -> str | None:
    """从照片根目录内的父目录提取离文件最近的完整日期候选。

    主照片目录下系统生成的 ``uploads/YYYY/MM`` 路径记录的是上传时间，整条路径
    禁止参与推断。只解析相对于已配置照片根目录的部分，避免宿主机备份目录污染。

    Args:
        path: 照片文件路径。
        image_roots: 按配置顺序排列的照片根目录，第一个是上传主目录。

    Returns:
        EXIF 风格的候选时间；无线索、路径越界、系统上传目录或日期非法时返回空。
    """
    resolved_path = Path(path).expanduser().resolve()
    for root_index, raw_root in enumerate(image_roots):
        root = Path(raw_root).expanduser().resolve()
        try:
            relative = resolved_path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if not parts:
            return None
        if (
            root_index == 0
            and len(parts) >= 4
            and parts[0] == SYSTEM_UPLOAD_DIRECTORY_NAME
            and re.fullmatch(r"(?:19|20)\d{2}", parts[1])
            and re.fullmatch(r"\d{2}", parts[2])
        ):
            return None

        parents = parts[:-1]
        candidates: list[tuple[int, int, str]] = []
        for index, segment in enumerate(parents):
            candidate = datetime_candidate_from_path_text(segment)
            if candidate:
                distance = len(parents) - 1 - index
                candidates.append((distance, 0, candidate))
        for index in range(max(0, len(parents) - 2)):
            candidate = _split_directory_candidate(parents, index)
            if candidate:
                distance = len(parents) - 1 - (index + 2)
                candidates.append((distance, 1, candidate))
        if not candidates:
            return None
        return min(candidates)[2]
    return None
