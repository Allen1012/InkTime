"""缩略图分辨率、质量与缓存的测试。

起因：后台照片网格看着模糊。缩略图上限是 300×200，而网格单元约 293 px 宽，
高清屏下需要约 586 px 的像素——先缩到 218 再放大 2.7 倍，模糊是数学上必然的。
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from src.configuration import ConfigurationActor, ConfigurationValidationError
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


def _write_photo(path, width: int = 1600, height: int = 1200) -> None:
    """在指定位置写入可解码的大图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (40, 110, 170)).save(path, format="JPEG", quality=92)


class ThumbnailTestCase(TemporaryDatabaseTestCase):
    """校验缩略图尺寸可配置、质量可控与响应可缓存。"""

    def setUp(self) -> None:
        """准备应用、管理员与一张可见照片。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.photo_path = self.image_directory / "big.jpg"
        _write_photo(self.photo_path)
        self.photo_id = self.create_photo("big.jpg", analysis_status="succeeded")
        self.app = create_app(self.application_config())
        self.configuration = self.app.extensions["inktime_services"]["configuration"]
        self.client = self.app.test_client()

    def change(self, **values: Any) -> None:
        """提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def fetch(self, headers: dict | None = None):
        """请求缩略图。"""
        return self.client.get(
            "/api/photo/thumbnail",
            query_string={"path": str(self.photo_path)},
            headers=headers or {},
        )

    def size_of(self, response) -> tuple[int, int]:
        """解析响应体得到缩略图尺寸。"""
        with Image.open(io.BytesIO(response.data)) as image:
            return image.size

    def test_default_thumbnail_is_large_enough_for_high_dpi(self) -> None:
        """验证默认长边足够高清屏使用，而不是原先的 300×200。"""
        response = self.fetch()

        self.assertEqual(200, response.status_code)
        width, height = self.size_of(response)
        self.assertEqual(640, max(width, height))

    def test_thumbnail_size_follows_configuration(self) -> None:
        """验证长边上限可在线调整，无需重启。"""
        self.change(THUMBNAIL_MAX_EDGE=320)

        width, height = self.size_of(self.fetch())

        self.assertEqual(320, max(width, height))

    def test_thumbnail_never_upscales_small_photos(self) -> None:
        """验证小图不会被放大，避免凭空变虚且浪费体积。"""
        small = self.image_directory / "small.jpg"
        _write_photo(small, 200, 150)
        self.create_photo("small.jpg", analysis_status="succeeded")

        response = self.client.get(
            "/api/photo/thumbnail", query_string={"path": str(small)}
        )

        with Image.open(io.BytesIO(response.data)) as image:
            self.assertEqual((200, 150), image.size)

    def test_quality_configuration_affects_payload_size(self) -> None:
        """验证质量配置生效：高质量的体积明显更大。"""
        self.change(THUMBNAIL_QUALITY=95)
        high = len(self.fetch().data)
        self.change(THUMBNAIL_QUALITY=50)
        low = len(self.fetch().data)

        self.assertGreater(high, low)

    def test_invalid_configuration_is_rejected(self) -> None:
        """验证越界的尺寸与质量被拒绝且不落库。"""
        version = self.configuration.list_admin_settings()["version"]
        for values in ({"THUMBNAIL_MAX_EDGE": 10}, {"THUMBNAIL_QUALITY": 101}):
            with self.subTest(values=values):
                with self.assertRaises(ConfigurationValidationError):
                    self.change(**values)
        self.assertEqual(version, self.configuration.list_admin_settings()["version"])

    def test_admin_thumbnail_shares_the_same_settings(self) -> None:
        """验证后台缩略图与公开缩略图走同一套尺寸与质量配置。

        实际踩坑：后台照片管理页用的是 `/admin/photos/<id>/thumbnail`，与公开接口是
        两条独立实现，其中一条仍硬编码 300×200，导致改配置后后台看着毫无变化。
        """
        from src.server.services import AdminPhotoService

        media = self.app.extensions["inktime_services"]["media"]
        admin_photos = self.app.extensions["inktime_services"]["admin_photo"]
        self.assertIsInstance(admin_photos, AdminPhotoService)

        self.change(THUMBNAIL_MAX_EDGE=320)
        with self.app.test_request_context():
            content = admin_photos.admin_thumbnail(self.photo_id)
        with Image.open(io.BytesIO(content.data)) as image:
            self.assertEqual(320, max(image.size))
        self.assertTrue(content.etag)

        self.change(THUMBNAIL_MAX_EDGE=750)
        with self.app.test_request_context():
            content = admin_photos.admin_thumbnail(self.photo_id)
        with Image.open(io.BytesIO(content.data)) as image:
            self.assertEqual(750, max(image.size))

    def test_admin_thumbnail_route_carries_cache_headers(self) -> None:
        """验证后台缩略图路由同样带缓存头并支持 304。"""
        import re

        with self.app.app_context():
            self.app.extensions["inktime_services"]["auth"].create_admin(
                "thumb-admin", "inktime-thumbnail-password"
            )
        client = self.app.test_client()
        form = client.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', form).group(1)
        client.post(
            "/admin/login",
            data={
                "username": "thumb-admin",
                "password": "inktime-thumbnail-password",
                "csrf_token": token,
            },
        )

        first = client.get(f"/admin/photos/{self.photo_id}/thumbnail")

        self.assertEqual(200, first.status_code)
        self.assertTrue(first.headers.get("ETag"))
        self.assertIn("max-age", first.headers.get("Cache-Control", ""))
        second = client.get(
            f"/admin/photos/{self.photo_id}/thumbnail",
            headers={"If-None-Match": first.headers["ETag"]},
        )
        self.assertEqual(304, second.status_code)

    def test_response_carries_cache_headers(self) -> None:
        """验证响应带缓存头：缩略图每次都要解码原图，重复生成很贵。"""
        response = self.fetch()

        self.assertIn("max-age", response.headers.get("Cache-Control", ""))
        self.assertTrue(response.headers.get("ETag"))

    def test_matching_etag_returns_not_modified(self) -> None:
        """验证带 If-None-Match 命中时返回 304 且不重复生成。"""
        first = self.fetch()
        etag = first.headers["ETag"]

        second = self.fetch({"If-None-Match": etag})

        self.assertEqual(304, second.status_code)
        self.assertEqual(b"", second.data)

    def test_etag_changes_when_size_configuration_changes(self) -> None:
        """验证改尺寸后校验值随之变化，避免浏览器继续用旧缓存。"""
        etag = self.fetch().headers["ETag"]

        self.change(THUMBNAIL_MAX_EDGE=320)

        self.assertNotEqual(etag, self.fetch().headers["ETag"])
