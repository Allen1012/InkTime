"""上传照片的格式受理与压缩测试。

覆盖三类真实场景：微信或浏览器保存下来的「WebP 但文件名叫 .jpg」、手机 HDR 与人像
模式产出的 MPO、iPhone 默认的 HEIC，以及五十兆量级大图需要压到目标体积。
"""

from __future__ import annotations

import io
import unittest
from typing import Any

from PIL import Image

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

    def test_animated_webp_is_still_rejected(self) -> None:
        """验证动图仍被拒绝：展示与渲染链路都只处理静态图。"""
        frames = [_noise_image(48, 48) for _ in range(3)]
        buffer = io.BytesIO()
        frames[0].save(
            buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=100
        )

        with self.assertRaises(UploadValidationError) as captured:
            self.upload("moving.webp", buffer.getvalue())
        self.assertIn("动画", str(captured.exception))

    def test_unsupported_content_format_is_rejected(self) -> None:
        """验证白名单之外的真实格式仍被拒绝。"""
        payload = _encode(Image.new("P", (32, 32)), "GIF")

        with self.assertRaises(UploadValidationError) as captured:
            self.upload("weird.jpg", payload)
        self.assertIn("支持", str(captured.exception))

    def test_broken_file_is_rejected(self) -> None:
        """验证损坏文件仍被拒绝且提示可读。"""
        with self.assertRaises(UploadValidationError) as captured:
            self.upload("broken.jpg", b"not an image at all")
        self.assertIn("损坏", str(captured.exception))


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
