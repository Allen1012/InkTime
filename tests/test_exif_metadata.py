"""共用 EXIF 提取的字段与 GPS 解析测试。

存在的意义：上传与分析原先各写一份提取实现，能力不一致，结果同一张照片在上传时读出
坐标、在分析时读不出，数据库里留下「有经纬度、城市为空」这种自相矛盾的记录。这里用
真实写入 EXIF 的图片钉住两件事：

1. GPS 必须走 `get_ifd(34853)`。顶层的 34853 是子 IFD 偏移量整数，判断它是不是字典
   永远为假，坐标会静默变成空值且不报错。
2. 拍摄参数在 Exif 子 IFD（34665）里。旧代码用私有的 `_getexif()` 恰好能读到，因为
   那个接口会扁平合并子 IFD；换成公开 `getexif()` 后必须显式读子 IFD。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.exif_metadata import (
    EXIF_FIELDS,
    byte_flag,
    extract_exif_fields,
    gps_decimal,
    optional_integer,
    optional_number,
    optional_text,
)


def _write_photo_with_exif(path: Path, **overrides) -> None:
    """写一张带真实 EXIF 的 JPEG，GPS 与拍摄参数各写进对应的子 IFD。"""
    image = Image.new("RGB", (8, 6), "gray")
    exif = image.getexif()
    exif[271] = overrides.get("make", "Xiaomi")
    exif[272] = overrides.get("model", "Xiaomi 17 Ultra")
    detail = exif.get_ifd(34665)
    detail[36867] = overrides.get("datetime", "2026:08:30 14:33:44")
    detail[34855] = overrides.get("iso", 50)
    detail[33434] = overrides.get("exposure_time", 0.002)
    detail[33437] = overrides.get("f_number", 2.2)
    detail[37386] = overrides.get("focal_length", 2.13)
    gps = exif.get_ifd(34853)
    gps.update(overrides.get("gps", {
        1: "N", 2: (40.0, 5.0, 38.1264),
        3: "E", 4: (116.0, 17.0, 32.2224),
        5: b"\x00", 6: 0.0,
    }))
    image.save(path, "JPEG", exif=exif)


class ExtractExifFieldsTestCase(unittest.TestCase):
    """用真实文件验证字段提取。"""

    def setUp(self) -> None:
        """准备临时目录。"""
        self.directory = tempfile.TemporaryDirectory(prefix="inktime-exif-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sample.jpg"

    def extract(self, **overrides) -> dict:
        """写图并提取字段。"""
        _write_photo_with_exif(self.path, **overrides)
        with Image.open(self.path) as image:
            return extract_exif_fields(image)

    def test_gps_is_read_from_the_gps_sub_ifd(self) -> None:
        """坐标必须能读出来，这正是先前分析链路读不到的那一项。"""
        fields = self.extract()

        self.assertAlmostEqual(40.093924, fields["gps_lat"], places=5)
        self.assertAlmostEqual(116.292284, fields["gps_lon"], places=5)

    def test_shooting_parameters_come_from_the_exif_sub_ifd(self) -> None:
        """ISO、光圈、快门、焦距都在 Exif 子 IFD 里，不能只查顶层。"""
        fields = self.extract()

        self.assertEqual(50, fields["iso"])
        self.assertAlmostEqual(2.2, fields["f_number"], places=3)
        self.assertAlmostEqual(2.13, fields["focal_length"], places=3)
        self.assertIsNotNone(fields["exposure_time"])

    def test_camera_and_datetime_are_read(self) -> None:
        """相机型号在顶层，拍摄时间优先取子 IFD 的原始时间。"""
        fields = self.extract()

        self.assertEqual("Xiaomi", fields["make"])
        self.assertEqual("Xiaomi 17 Ultra", fields["model"])
        self.assertEqual("2026:08:30 14:33:44", fields["datetime"])

    def test_southern_and_western_hemisphere_get_negative_values(self) -> None:
        """南纬与西经必须取负号，否则会定位到地球另一侧。"""
        fields = self.extract(gps={
            1: "S", 2: (33.0, 51.0, 0.0),
            3: "W", 4: (70.0, 39.0, 0.0),
        })

        self.assertAlmostEqual(-33.85, fields["gps_lat"], places=4)
        self.assertAlmostEqual(-70.65, fields["gps_lon"], places=4)

    def test_altitude_below_sea_level_is_negative(self) -> None:
        """GPSAltitudeRef 是 BYTE 类型，值为 1 表示海平面以下。"""
        fields = self.extract(gps={
            1: "N", 2: (40.0, 0.0, 0.0),
            3: "E", 4: (116.0, 0.0, 0.0),
            5: b"\x01", 6: 120.0,
        })

        self.assertAlmostEqual(-120.0, fields["gps_alt"], places=3)

    def test_photo_without_exif_yields_all_empty_fields(self) -> None:
        """没有 EXIF 的图片返回全空字段，而不是抛异常。"""
        Image.new("RGB", (4, 4), "white").save(self.path, "JPEG")
        with Image.open(self.path) as image:
            fields = extract_exif_fields(image)

        self.assertEqual(set(EXIF_FIELDS), set(fields))
        self.assertTrue(all(value is None for value in fields.values()))

    def test_result_always_contains_every_field(self) -> None:
        """键集合固定，调用方可以直接按列名映射而不必逐个判存在。"""
        self.assertEqual(set(EXIF_FIELDS), set(self.extract()))


class HelperTestCase(unittest.TestCase):
    """验证畸形取值的容错，这些都是线上真实遇到过的形态。"""

    def test_optional_integer_tolerates_byte_strings(self) -> None:
        """手机导出的照片里 ISO 可能是字节串，直接 int() 会抛异常。

        `b"\\x00"` 这种取值判为无效而不是 0：0 会当成一个真实的 ISO 值显示出来，
        而它实际表示「这个字段没有内容」。
        """
        self.assertEqual(50, optional_integer(50))
        self.assertEqual(100, optional_integer(b"100"))
        self.assertIsNone(optional_integer(b"\x00"))
        self.assertIsNone(optional_integer(b"\xff\xfe"))
        self.assertIsNone(optional_integer(None))

    def test_byte_flag_reads_ordinal_not_text(self) -> None:
        """BYTE 字段的值是字节序数，按文本解析永远得不到 1。"""
        self.assertEqual(0, byte_flag(b"\x00"))
        self.assertEqual(1, byte_flag(b"\x01"))
        self.assertEqual(1, byte_flag(1))
        self.assertIsNone(byte_flag(None))

    def test_optional_text_strips_null_bytes(self) -> None:
        """EXIF 文本常带尾随空字节。"""
        self.assertEqual("Xiaomi", optional_text(b"Xiaomi\x00"))
        self.assertEqual("Xiaomi", optional_text("  Xiaomi  "))
        self.assertIsNone(optional_text("   "))
        self.assertIsNone(optional_text(None))

    def test_optional_number_tolerates_zero_division(self) -> None:
        """Pillow 有理数分母为零时不应把整张照片拖死。"""
        from fractions import Fraction

        self.assertAlmostEqual(2.2, optional_number(Fraction(11, 5)), places=3)
        self.assertIsNone(optional_number("not a number"))
        self.assertIsNone(optional_number(None))

    def test_gps_decimal_requires_three_components(self) -> None:
        """度分秒必须三项齐全，缺项时判无效而不是猜。"""
        self.assertAlmostEqual(40.0939, gps_decimal((40.0, 5.0, 38.1264), "N"), places=4)
        self.assertIsNone(gps_decimal((40.0, 5.0), "N"))
        self.assertIsNone(gps_decimal(None, "N"))
        self.assertIsNone(gps_decimal((40.0, 5.0, None), "N"))


if __name__ == "__main__":
    unittest.main()
