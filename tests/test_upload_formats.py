"""上传照片的格式受理与压缩测试。

覆盖三类真实场景：微信或浏览器保存下来的「WebP 但文件名叫 .jpg」、手机 HDR 与人像
模式产出的 MPO、iPhone 默认的 HEIC，以及五十兆量级大图需要压到目标体积。
"""

from __future__ import annotations

import io
import unittest
from typing import Any

from PIL import Image

from pathlib import Path

from src.configuration import ConfigurationActor
from src.server.admin_jobs import UploadValidationError
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase

try:  # pillow-heif 缺失时跳过 HEIC 用例，而不是整个模块报错
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于部署环境
    HEIF_AVAILABLE = False


def _noise_image(width: int, height: int) -> Image.Image:
    """生成难以压缩的噪声图，确保编码后体积足够大。"""
    import os

    return Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))


def _encode(image: Image.Image, image_format: str, **options: Any) -> bytes:
    """把图片编码为字节。"""
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


class _FakeUpload:
    """模拟 Werkzeug FileStorage 的最小上传对象。"""

    def __init__(self, filename: str, payload: bytes) -> None:
        """保存文件名与字节流。"""
        self.filename = filename
        self.stream = io.BytesIO(payload)


class UploadFormatTestCase(TemporaryDatabaseTestCase):
    """校验扩展名与真实格式不一致、多帧与 HEIC 的受理规则。"""

    def setUp(self) -> None:
        """准备应用与管理员。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.app = create_app(self.application_config())
        self.uploads = self.app.extensions["inktime_services"]["uploads"]
        self.configuration = self.app.extensions["inktime_services"]["configuration"]

    def change(self, **values: Any) -> None:
        """提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def upload(self, filename: str, payload: bytes) -> dict:
        """上传单个文件并返回批次结果。"""
        return self.uploads.upload([_FakeUpload(filename, payload)], self.user_id)

    def stored_path(self, result: dict):
        """取出本次落盘的路径。"""
        from pathlib import Path

        self.assertEqual(1, result["counts"]["accepted"], result)
        return Path(result["items"][0]["path"])

    def test_webp_named_jpg_is_accepted_and_stored_as_webp(self) -> None:
        """验证真实格式为 WebP 但文件名为 .jpg 时按真实格式落盘。

        这类文件来自微信或浏览器另存，手机与电脑都能正常查看，被拒绝没有道理：
        上传流程本来就会重新编码并按真实格式生成扩展名。
        """
        payload = _encode(_noise_image(64, 48), "WEBP", lossless=True)

        path = self.stored_path(self.upload("girl.jpg", payload))

        self.assertEqual(".webp", path.suffix)
        self.assertTrue(path.is_file())
        with Image.open(path) as image:
            self.assertEqual("WEBP", image.format)

    def test_png_named_jpeg_is_accepted_and_stored_as_png(self) -> None:
        """验证 PNG 内容配 .jpeg 文件名同样按真实格式落盘。"""
        path = self.stored_path(
            self.upload("shot.jpeg", _encode(_noise_image(48, 48), "PNG"))
        )

        self.assertEqual(".png", path.suffix)

    def test_mpo_is_accepted_as_jpeg_first_frame(self) -> None:
        """验证手机 HDR 与人像模式的 MPO 被当作 JPEG 首帧受理。"""
        first = _noise_image(80, 60)
        second = _noise_image(80, 60)
        buffer = io.BytesIO()
        first.save(buffer, format="MPO", append_images=[second])

        path = self.stored_path(self.upload("IMG_0001.jpg", buffer.getvalue()))

        self.assertEqual(".jpg", path.suffix)
        with Image.open(path) as image:
            self.assertEqual("JPEG", image.format)
            self.assertEqual(1, getattr(image, "n_frames", 1))

    @unittest.skipUnless(HEIF_AVAILABLE, "未安装 pillow-heif")
    def test_heic_is_converted_to_jpeg(self) -> None:
        """验证 HEIC 被转码为 JPEG。

        浏览器与墨水屏渲染链路都不支持 HEIC，因此必须转码而不是原样保存，
        否则照片墙会显示不出来。
        """
        payload = _encode(_noise_image(96, 72), "HEIF")

        path = self.stored_path(self.upload("IMG_1234.HEIC", payload))

        self.assertEqual(".jpg", path.suffix)
        with Image.open(path) as image:
            self.assertEqual("JPEG", image.format)

    def test_stored_file_keeps_exif_for_later_recovery(self) -> None:
        """验证落盘文件保留 EXIF，使元数据可事后重新提取。

        原实现重新编码时丢弃全部 EXIF，数据库里只留解析结果。一旦解析逻辑有缺陷
        （实际发生过），服务器上的副本已无信息可补救，只能让使用者重新上传原片。
        """
        exif = Image.Exif()
        exif[36867] = "2026:06:07 13:01:47"
        exif[271] = "Xiaomi"
        buffer = io.BytesIO()
        _noise_image(64, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        path = self.stored_path(self.upload("keep-exif.jpg", buffer.getvalue()))

        with Image.open(path) as image:
            stored = image.getexif()
        self.assertEqual("2026:06:07 13:01:47", stored.get(36867))
        self.assertEqual("Xiaomi", stored.get(271))

    def test_stored_file_drops_orientation_tag(self) -> None:
        """验证落盘文件不保留方向标签，避免看图软件二次旋转。

        像素已按 EXIF 方向固化，若同时保留 Orientation，查看时会再转一次。
        """
        exif = Image.Exif()
        exif[274] = 6  # 顺时针 90 度
        exif[36867] = "2026:06:07 13:01:47"
        buffer = io.BytesIO()
        _noise_image(64, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        path = self.stored_path(self.upload("rotated.jpg", buffer.getvalue()))

        with Image.open(path) as image:
            stored = image.getexif()
            self.assertEqual((48, 64), image.size)
        self.assertIsNone(stored.get(274))
        self.assertEqual("2026:06:07 13:01:47", stored.get(36867))

    def test_gps_metadata_is_extracted(self) -> None:
        """验证带 GPS 的照片能取到经纬度、海拔与拍摄时间。

        实际事故：`GPSAltitudeRef` 按 EXIF 标准就是 BYTE 类型，值本来是 `b'\\x00'`，
        而代码里写的是 `int(gps.get(5, 0) or 0)`，必然抛 ValueError。这不是畸形数据
        而是正常数据，结果**每一张带 GPS 的照片都丢掉了全部元数据**。
        """
        exif = Image.Exif()
        exif[36867] = "2026:06:07 13:01:47"
        exif[271] = "Xiaomi"
        exif[272] = "Test Phone"
        exif[34853] = {
            1: "N", 2: (39.0, 54.0, 0.0),
            3: "E", 4: (116.0, 23.0, 0.0),
            5: b"\x00", 6: 50.0,
        }
        buffer = io.BytesIO()
        _noise_image(64, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        result = self.upload("with-gps.jpg", buffer.getvalue())
        photo_id = result["items"][0]["photo_id"]

        with self.database() as connection:
            row = connection.execute(
                "SELECT exif_datetime,exif_make,exif_model,exif_gps_lat,exif_gps_lon,"
                "exif_gps_alt FROM photo_scores WHERE id=?",
                (photo_id,),
            ).fetchone()
        self.assertEqual("2026:06:07 13:01:47", row["exif_datetime"])
        self.assertEqual("Xiaomi", row["exif_make"])
        self.assertEqual("Test Phone", row["exif_model"])
        self.assertAlmostEqual(39.9, row["exif_gps_lat"], places=3)
        self.assertAlmostEqual(116.383, row["exif_gps_lon"], places=2)
        self.assertAlmostEqual(50.0, row["exif_gps_alt"], places=3)

    def test_negative_altitude_reference_is_honored(self) -> None:
        """验证海拔参考为 1 时海拔取负值。"""
        exif = Image.Exif()
        exif[34853] = {1: "N", 2: (10.0, 0.0, 0.0), 3: "E", 4: (20.0, 0.0, 0.0), 5: 1, 6: 30.0}
        buffer = io.BytesIO()
        _noise_image(48, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        result = self.upload("below-sea.jpg", buffer.getvalue())

        with self.database() as connection:
            row = connection.execute(
                "SELECT exif_gps_alt FROM photo_scores WHERE id=?",
                (result["items"][0]["photo_id"],),
            ).fetchone()
        self.assertAlmostEqual(-30.0, row["exif_gps_alt"], places=3)

    def test_broken_gps_does_not_discard_other_fields(self) -> None:
        """验证 GPS 解析抛错时，拍摄时间与相机字段仍然保留。

        整段兜底会把已解析成功的字段一起丢掉，等于用「不崩」换掉全部信息，因此降级
        必须按字段独立进行。这里直接构造一个读取 GPS 就抛错的 EXIF，Pillow 无法把
        这种结构写进真实文件。
        """

        class _ExplodingExif(dict):
            """读取 GPS 子目录时抛错的 EXIF 替身。"""

            def get_ifd(self, tag: int) -> dict:
                raise ValueError("broken gps ifd")

        class _FakeImage:
            """只提供 getexif 的最小图片替身。"""

            def getexif(self) -> _ExplodingExif:
                exif = _ExplodingExif()
                exif[36867] = "2025:01:02 03:04:05"
                exif[271] = "Canon"
                exif[34855] = b"\x00"
                return exif

        metadata = self.uploads._extract_original_metadata(_FakeImage())

        self.assertEqual("2025:01:02 03:04:05", metadata["exif_datetime"])
        self.assertEqual("Canon", metadata["exif_make"])
        self.assertIsNone(metadata["exif_iso"])
        self.assertIsNone(metadata["exif_gps_lat"])

    def test_malformed_exif_does_not_block_upload(self) -> None:
        """验证畸形 EXIF 不会让整张照片上传失败。

        实际事故：手机上传的照片 ISO 字段是字节串 `b'\\x00'`，元数据提取里的
        `int()` 直接抛 ValueError，被当成「无法解码该图片」拒收。元数据只是可选的
        锦上添花，绝不该阻断照片本身入库。
        """
        exif = Image.Exif()
        exif[34855] = b"\x00"  # ISOSpeedRatings 写成字节串
        exif[271] = "TestMake"
        buffer = io.BytesIO()
        _noise_image(64, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        path = self.stored_path(self.upload("weird-exif.jpg", buffer.getvalue()))

        self.assertTrue(path.is_file())

    def test_malformed_exif_keeps_other_fields(self) -> None:
        """验证单个字段畸形时其余可用字段仍被保留。"""
        exif = Image.Exif()
        exif[34855] = b"\x00"
        exif[36867] = "2024:05:01 08:30:00"
        buffer = io.BytesIO()
        _noise_image(48, 48).save(buffer, format="JPEG", exif=exif.tobytes())

        result = self.upload("mixed-exif.jpg", buffer.getvalue())
        photo_id = result["items"][0]["photo_id"]

        with self.database() as connection:
            row = connection.execute(
                "SELECT exif_datetime,exif_iso FROM photo_scores WHERE id=?", (photo_id,)
            ).fetchone()
        self.assertEqual("2024:05:01 08:30:00", row["exif_datetime"])
        self.assertIsNone(row["exif_iso"])

    def test_animated_image_is_flattened_to_first_frame(self) -> None:
        """验证动图取首帧转为静态图，而不是整张拒收。

        手机相册里的动态照片很常见，展示与渲染链路只处理静态图，但首帧就是使用者
        想要的那一张，没有理由让整批上传失败。
        """
        frames = [_noise_image(48, 48) for _ in range(3)]
        buffer = io.BytesIO()
        frames[0].save(
            buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=100
        )

        path = self.stored_path(self.upload("moving.webp", buffer.getvalue()))

        with Image.open(path) as image:
            self.assertEqual(1, int(getattr(image, "n_frames", 1)))
            self.assertFalse(bool(getattr(image, "is_animated", False)))

    def test_unsupported_content_format_is_reported_per_file(self) -> None:
        """验证白名单之外的格式作为单项失败返回，且带可读原因。"""
        payload = _encode(Image.new("P", (32, 32)), "GIF")

        result = self.upload("weird.jpg", payload)

        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(0, result["counts"]["accepted"])
        self.assertEqual("weird.jpg", result["items"][0]["original_filename"])
        self.assertIn("支持", result["items"][0]["message"])

    def test_broken_file_reports_underlying_reason(self) -> None:
        """验证损坏文件的失败原因带上底层异常，便于判断到底是哪种问题。"""
        result = self.upload("broken.jpg", b"not an image at all")

        self.assertEqual(1, result["counts"]["failed"])
        message = result["items"][0]["message"]
        self.assertIn("无法解码", message)
        # 只说「损坏或无法解码」无法区分格式不支持、文件截断与编解码器缺失
        self.assertIn("Error", message)

    def test_partial_batch_uploads_valid_files(self) -> None:
        """验证一批中的非法文件不再拖累其余文件。

        这是使用者反馈的核心问题：一次勾选十张，其中一张是动图或坏文件，结果整批
        都没成功，只能重新勾选。
        """
        good_first = _encode(_noise_image(64, 48), "JPEG", quality=90)
        good_second = _encode(_noise_image(48, 64), "PNG")
        uploads = [
            _FakeUpload("first.jpg", good_first),
            _FakeUpload("bad.jpg", b"definitely not an image"),
            _FakeUpload("second.png", good_second),
        ]

        result = self.uploads.upload(uploads, self.user_id)

        self.assertEqual(2, result["counts"]["accepted"])
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(3, result["total"])
        # 顺序与输入一致，前端按序标注状态
        self.assertEqual(
            ["accepted", "failed", "accepted"],
            [item["status"] for item in result["items"]],
        )
        self.assertEqual(
            ["first.jpg", "bad.jpg", "second.png"],
            [item["original_filename"] for item in result["items"]],
        )
        for item in result["items"]:
            if item["status"] == "accepted":
                self.assertTrue(Path(item["path"]).is_file())

    def test_all_invalid_batch_does_not_raise(self) -> None:
        """验证整批都非法时返回逐项失败，而不是抛异常导致前端只看到一句报错。"""
        result = self.uploads.upload(
            [
                _FakeUpload("a.jpg", b"broken one"),
                _FakeUpload("b.png", b"broken two"),
            ],
            self.user_id,
        )

        self.assertEqual(0, result["counts"]["accepted"])
        self.assertEqual(2, result["counts"]["failed"])

    def test_batch_level_limits_still_raise(self) -> None:
        """验证批次级限制仍整批拒绝：文件数超限属于操作错误，不是单文件问题。"""
        payload = _encode(_noise_image(32, 32), "JPEG", quality=80)
        self.change(UPLOAD_MAX_FILES=1)

        with self.assertRaises(UploadValidationError) as captured:
            self.uploads.upload(
                [_FakeUpload("a.jpg", payload), _FakeUpload("b.jpg", payload)],
                self.user_id,
            )
        self.assertIn("每批最多", str(captured.exception))


class UploadCompressionTestCase(TemporaryDatabaseTestCase):
    """校验大图按目标体积压缩与长边限制。"""

    def setUp(self) -> None:
        """准备应用与管理员。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.app = create_app(self.application_config())
        self.uploads = self.app.extensions["inktime_services"]["uploads"]
        self.configuration = self.app.extensions["inktime_services"]["configuration"]

    def change(self, **values: Any) -> None:
        """提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def upload(self, filename: str, payload: bytes):
        """上传单个文件并返回落盘路径。"""
        from pathlib import Path

        result = self.uploads.upload([_FakeUpload(filename, payload)], self.user_id)
        self.assertEqual(1, result["counts"]["accepted"], result)
        return Path(result["items"][0]["path"])

    def test_large_jpeg_is_compressed_below_target(self) -> None:
        """验证超过目标体积的大图被压缩到目标以下。"""
        self.change(UPLOAD_TARGET_BYTES=200 * 1024, UPLOAD_MAX_LONG_EDGE=1200)
        payload = _encode(_noise_image(1600, 1200), "JPEG", quality=95)
        self.assertGreater(len(payload), 200 * 1024)

        path = self.upload("big.jpg", payload)

        self.assertLessEqual(path.stat().st_size, 200 * 1024)
        with Image.open(path) as image:
            self.assertEqual("JPEG", image.format)
            self.assertLessEqual(max(image.size), 1200)

    def test_long_edge_limit_downscales_image(self) -> None:
        """验证长边超限时按比例缩放并保持宽高比。"""
        self.change(UPLOAD_TARGET_BYTES=0, UPLOAD_MAX_LONG_EDGE=400)
        payload = _encode(_noise_image(1000, 500), "JPEG", quality=90)

        path = self.upload("wide.jpg", payload)

        with Image.open(path) as image:
            self.assertEqual((400, 200), image.size)

    def test_target_zero_disables_compression(self) -> None:
        """验证目标体积为零时不做压缩，只受长边限制约束。"""
        self.change(UPLOAD_TARGET_BYTES=0, UPLOAD_MAX_LONG_EDGE=0)
        source = _noise_image(900, 700)
        payload = _encode(source, "JPEG", quality=95)

        path = self.upload("keep.jpg", payload)

        with Image.open(path) as image:
            self.assertEqual(source.size, image.size)

    def test_small_image_is_not_upscaled_or_degraded(self) -> None:
        """验证小图不会被放大，也不会因压缩逻辑而改变尺寸。"""
        self.change(UPLOAD_TARGET_BYTES=5 * 1024 * 1024, UPLOAD_MAX_LONG_EDGE=4096)
        source = _noise_image(320, 240)

        path = self.upload("small.jpg", _encode(source, "JPEG", quality=90))

        with Image.open(path) as image:
            self.assertEqual(source.size, image.size)

    def test_png_keeps_format_and_is_not_converted_to_jpeg(self) -> None:
        """验证 PNG 只缩放不转码：截图类图片转 JPEG 会让文字发虚。"""
        self.change(UPLOAD_TARGET_BYTES=50 * 1024, UPLOAD_MAX_LONG_EDGE=600)
        payload = _encode(_noise_image(1000, 800), "PNG")

        path = self.upload("screenshot.png", payload)

        self.assertEqual(".png", path.suffix)
        with Image.open(path) as image:
            self.assertEqual("PNG", image.format)
            self.assertLessEqual(max(image.size), 600)

    def test_fifty_megabyte_class_upload_is_accepted(self) -> None:
        """验证默认上限能接收五十兆量级的照片并压到目标体积。"""
        limits = self.configuration.get_many(
            ("UPLOAD_MAX_BYTES", "UPLOAD_TARGET_BYTES")
        )
        self.assertGreaterEqual(int(limits["UPLOAD_MAX_BYTES"]), 50 * 1024 * 1024)
        self.assertLessEqual(int(limits["UPLOAD_TARGET_BYTES"]), 8 * 1024 * 1024)
