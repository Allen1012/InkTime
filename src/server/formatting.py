"""后台页面的展示格式化工具（时间、文件大小）。

只作用于展示层：数据库里的时间保持原值不动。`photo_scores.exif_datetime` 存的是 EXIF
标准格式 `YYYY:MM:DD HH:MM:SS`，照片分析、每日渲染与展示选片都按该格式解析，改存储
会牵动整条链路；而把冒号分隔的日期原样显示给人看并不符合阅读习惯。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 按尝试顺序排列：EXIF 标准格式在前，其余是历史数据与手工录入可能出现的写法
_PATTERNS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y:%m:%d %H:%M",
    "%Y:%m:%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)


def _parse(value: str) -> tuple[datetime, bool] | None:
    """解析时间字符串，返回时间与是否包含时分。"""
    text = value.strip()
    if not text:
        return None
    try:
        # 先按 ISO 解析：带时区的时间戳（如创建时间、更新时间）走这条路
        return datetime.fromisoformat(text), True
    except ValueError:
        pass
    for pattern in _PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed, "%H" in pattern
    return None


def readable_time(value: Any) -> str:
    """把各种来源的时间格式化为中文常见读法。

    带时区的时间戳会换算到本机时区再展示：后台的创建时间与更新时间以协调世界时存储，
    原样展示会比本地时间差好几个小时。不带时区的值（EXIF 拍摄时间就是这种）按本地
    时间直接展示，不做换算——EXIF 里没有时区信息，擅自换算只会让时间变错。

    无法解析时原样返回，既不隐藏数据也不抛错：历史数据与手工录入的内容格式不可控。

    Args:
        value: 时间字符串或时间对象。

    Returns:
        形如 `2026年1月31日 14:27` 的字符串；只有日期时省略时间部分；空值返回空串。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        parsed, with_time = value, True
    else:
        text = str(value)
        result = _parse(text)
        if result is None:
            return text.strip()
        parsed, with_time = result
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    formatted = f"{parsed.year}年{parsed.month}月{parsed.day}日"
    if with_time:
        formatted += f" {parsed.hour:02d}:{parsed.minute:02d}"
    return formatted


# 用 1024 进制与 KB/MB/GB 标签：文件管理器与手机相册都这么显示，
# 跟着走比标签严谨（KiB/MiB）更符合阅读习惯。
# 不足 1 KB 时保留中文「字节」：这个量级本来就好读，中文界面里
# 「128 字节」比「128 B」自然，上传超限的错误信息也走这条路。
_SIZE_UNITS = ("KB", "MB", "GB", "TB")


def readable_size(value: Any) -> str:
    """把字节数格式化为常用的 KB/MB/GB 读法。

    `4630341 字节` 这样的原始数字对人没有体感，读起来还要自己数位数。

    不足 1 KB 时保留「N 字节」；到 KB 以上保留一位小数并去掉多余的零，
    因此 `1048576` 显示为 `1 MB` 而不是 `1.0 MB`。

    Args:
        value: 字节数，可为字符串、浮点或空值。

    Returns:
        形如 `4.4 MB` 的字符串；空值或非法值返回空串。
    """
    if value is None or isinstance(value, bool):
        return ""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    if size < 0:
        return ""
    if size < 1024:
        return f"{int(size)} 字节"
    for unit in _SIZE_UNITS:
        size /= 1024.0
        # 换算后不足 1024 就落在当前单位；TB 是最后一档，兜住超大值
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            text = f"{size:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{size:.1f} {_SIZE_UNITS[-1]}"
