"""从图片提取标准化 EXIF 字段，供上传与分析两条链路共用。

**为什么必须共用一套实现**：这两处原先各写了一份 GPS 解析，能力并不一致，结果是同一张
照片在上传时能读出坐标、在分析时读不出。数据库里于是出现「有经纬度、城市为空」这种
自相矛盾的记录：坐标是上传写的，城市是分析算的，而分析拿到的坐标是空。

分析那份用的是 `exif.get("GPSInfo")` 再判断 `isinstance(..., dict)`。这在现代 Pillow 上
永远为假——`GPSInfo`（标签 34853）在顶层 IFD 里存的是**指向子 IFD 的偏移量整数**，不是
字典，必须用 `get_ifd(34853)` 才能拿到真正的 GPS 标签表。判断为假之后代码静默跳过，
坐标就成了空值，没有任何报错。

因此这里只保留正确的那一份，两侧都调用它，各自再映射到自己的输出结构。
"""

from __future__ import annotations

from typing import Any, Mapping

from PIL import Image


# EXIF 标签号。用数字而不是 `ExifTags.TAGS` 反查名字：名字在不同 Pillow 版本间有过变化
# （如 ISOSpeedRatings 与 PhotographicSensitivity），标签号是标准里固定的。
_GPS_IFD = 34853
# 拍摄参数（ISO、光圈、快门、焦距、拍摄时间）存在 Exif 子 IFD 里，不在顶层。
# 旧代码用私有的 `_getexif()` 恰好能读到它们，因为那个接口会把子 IFD 扁平合并；
# 换成公开的 `getexif()` 后必须显式读这个子 IFD，否则这些字段会静默变成空值。
_EXIF_IFD = 34665
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME = 306
_TAG_EXPOSURE_TIME = 33434
_TAG_F_NUMBER = 33437
_TAG_ISO = 34855
_TAG_DATETIME_ORIGINAL = 36867
_TAG_DATETIME_DIGITIZED = 36868
_TAG_FOCAL_LENGTH = 37386
# GPS 子 IFD 内的标签号
_GPS_LATITUDE_REF = 1
_GPS_LATITUDE = 2
_GPS_LONGITUDE_REF = 3
_GPS_LONGITUDE = 4
_GPS_ALTITUDE_REF = 5
_GPS_ALTITUDE = 6

EXIF_FIELDS = (
    "datetime",
    "make",
    "model",
    "iso",
    "exposure_time",
    "f_number",
    "focal_length",
    "gps_lat",
    "gps_lon",
    "gps_alt",
)


def optional_integer(value: Any) -> int | None:
    """把 EXIF 整数字段转换为整数，非法值返回空。

    畸形 EXIF 很常见：手机导出的照片里 ISO 字段可能是 `b'\\x00'` 这种字节串，直接
    `int()` 会抛 ValueError。元数据只是可选信息，不能因为一个字段把整张照片拖死。
    """
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            return int(value.decode("ascii", errors="strict").strip() or 0)
        return int(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def byte_flag(value: Any) -> int | None:
    """把 EXIF 中 BYTE 类型的标志位解析为整数。

    与文本型数字不同：BYTE 字段的数值是字节序数，`b"\\x01"` 表示 1 而不是字符 "1"。
    GPSAltitudeRef 就是这种类型，用文本解析会永远得不到 1，导致海平面以下的海拔取不到
    负号。
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value[0] if len(value) == 1 else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_text(value: Any) -> str | None:
    """把 EXIF 文本字段转换为去空白字符串，空值与异常返回空。"""
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value)
    except Exception:
        return None
    text = text.strip().strip("\x00").strip()
    return text or None


def optional_number(value: Any) -> float | None:
    """把 Pillow 有理数等数值转换为浮点数，非法值返回空值。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def gps_decimal(values: Any, reference: Any) -> float | None:
    """把 EXIF 度分秒坐标转换为带方向的十进制度。"""
    if not values or len(values) != 3:
        return None
    parts = [optional_number(item) for item in values]
    if any(item is None for item in parts):
        return None
    result = float(parts[0]) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0
    if str(reference).upper() in {"S", "W"}:
        result = -result
    return result


def extract_gps(exif: Any) -> tuple[float | None, float | None, float | None]:
    """从 EXIF 对象读取纬度、经度与海拔。

    GPS 段单独解析并单独兜底：整段兜底会把已经解析成功的拍摄时间与相机字段一起丢掉，
    等于用「不崩」换掉了全部信息。实际事故正是如此——`GPSAltitudeRef` 按标准是 BYTE
    类型、值本来就是 `b'\\x00'`，一个 `int()` 让每张带 GPS 的照片都丢光元数据。

    Args:
        exif: `Image.getexif()` 返回的 EXIF 对象。

    Returns:
        纬度、经度与海拔的三元组，取不到的项为空值。
    """
    try:
        # 必须走 get_ifd：顶层的 34853 存的是子 IFD 偏移量整数，不是标签字典。
        gps: Mapping[int, Any] = exif.get_ifd(_GPS_IFD) or {}
        latitude = gps_decimal(gps.get(_GPS_LATITUDE), gps.get(_GPS_LATITUDE_REF))
        longitude = gps_decimal(gps.get(_GPS_LONGITUDE), gps.get(_GPS_LONGITUDE_REF))
        altitude = optional_number(gps.get(_GPS_ALTITUDE))
        # GPSAltitudeRef 是 BYTE 类型，b"\x00" 表示海平面以上，1 表示以下
        if altitude is not None and byte_flag(gps.get(_GPS_ALTITUDE_REF)) == 1:
            altitude = -altitude
        return latitude, longitude, altitude
    except Exception:
        return None, None, None


def extract_exif_fields(image: Image.Image) -> dict[str, Any]:
    """从已打开的图片提取标准化 EXIF 字段。

    上传与分析两条链路都调用这一个函数，因此不会再出现「一侧读得到、另一侧读不到」。
    调用方负责把结果映射到自己的输出结构（数据库列名或分析结果键名）。

    Args:
        image: 已打开的 Pillow 图片对象。

    Returns:
        含 `EXIF_FIELDS` 全部键的字典，取不到的项为空值。
    """
    exif = image.getexif()
    latitude, longitude, altitude = extract_gps(exif)
    try:
        detail: Mapping[int, Any] = exif.get_ifd(_EXIF_IFD) or {}
    except Exception:
        detail = {}

    def pick(tag: int) -> Any:
        """先取 Exif 子 IFD，再回退顶层。

        两处都查是为了兼容各种写入器：标准把拍摄参数放在子 IFD，但也有工具把它们
        写在顶层，只查一处会漏。
        """
        value = detail.get(tag)
        return exif.get(tag) if value is None else value

    return {
        "datetime": optional_text(
            pick(_TAG_DATETIME_ORIGINAL)
            or pick(_TAG_DATETIME_DIGITIZED)
            or exif.get(_TAG_DATETIME)
        ),
        "make": optional_text(exif.get(_TAG_MAKE)),
        "model": optional_text(exif.get(_TAG_MODEL)),
        "iso": optional_integer(pick(_TAG_ISO)),
        "exposure_time": optional_number(pick(_TAG_EXPOSURE_TIME)),
        "f_number": optional_number(pick(_TAG_F_NUMBER)),
        "focal_length": optional_number(pick(_TAG_FOCAL_LENGTH)),
        "gps_lat": latitude,
        "gps_lon": longitude,
        "gps_alt": altitude,
    }
