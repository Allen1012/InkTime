"""后台展示原始文件名的回归测试。

上传的照片落盘名是随机十六进制串（防重名与路径穿越），后台此前直接展示磁盘名，
一串随机字符看不出是哪张照片。原始名一直存在 `original_filename` 里，本用例固定
展示口径：优先原始名，扫描入库的照片回退磁盘名，且磁盘名仍然可追溯。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

from src.server.app import create_app
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class OriginalFilenameDisplayTestCase(TemporaryDatabaseTestCase):
    """校验后台列表、详情与回收站的展示名口径一致。"""

    ADMIN_USERNAME = "filename-admin"
    ADMIN_PASSWORD = "inktime-filename-test-password"

    def setUp(self) -> None:
        """创建应用与已登录会话。"""
        super().setUp()
        self.application = create_app(self.application_config())
        with self.application.app_context():
            self.application.extensions["inktime_services"]["auth"].create_admin(
                self.ADMIN_USERNAME, self.ADMIN_PASSWORD
            )
        self.client = self.application.test_client()
        body = self.client.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token, "登录页应包含跨站请求伪造令牌")
        response = self.client.post(
            "/admin/login",
            data={
                "username": self.ADMIN_USERNAME,
                "password": self.ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
        )
        self.assertIn(response.status_code, (302, 303))

    def _create_uploaded_photo(self, original_filename: str) -> tuple[int, str]:
        """构造一张「上传来的」照片：随机磁盘名 + 保留原始名。"""
        stored_name = f"{uuid.uuid4().hex}.jpg"
        target = self.image_directory / "uploads" / "2026" / "08" / stored_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,original_filename,analysis_status,"
                "is_deleted,created_at,updated_at,version) "
                "VALUES (?,?,'succeeded',0,?,?,1)",
                (str(target), original_filename, TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            return int(cursor.lastrowid), stored_name

    def _list_items(self) -> dict[str, dict]:
        """按展示名索引后台列表结果。"""
        with self.application.test_request_context("/admin/photos"):
            service = self.application.extensions["inktime_services"]["admin_photo"]
            result = service.list_photos(1, 24, "", "", "", "", "", "latest", "grid")
        return {item["title"]: item for item in result["items"]}

    def test_uploaded_photo_shows_original_filename(self) -> None:
        """上传照片展示原始名，而不是随机磁盘名。"""
        photo_id, stored_name = self._create_uploaded_photo("全家福-外婆生日.jpg")

        items = self._list_items()

        self.assertIn("全家福-外婆生日.jpg", items)
        self.assertNotIn(stored_name, items)
        self.assertEqual(photo_id, items["全家福-外婆生日.jpg"]["id"])

    def test_stored_filename_stays_traceable(self) -> None:
        """展示名换成原始名后，磁盘名仍要能查到，否则无法排查文件。"""
        _photo_id, stored_name = self._create_uploaded_photo("聚餐.jpg")

        item = self._list_items()["聚餐.jpg"]

        self.assertEqual(stored_name, item["stored_filename"])
        self.assertTrue(item["path"].endswith(stored_name))

    def test_scanned_photo_falls_back_to_disk_name(self) -> None:
        """扫描入库的照片没有原始名，回退磁盘名而不是显示「未命名」。"""
        self.create_photo("2019-婚礼现场.jpg")

        items = self._list_items()

        self.assertIn("2019-婚礼现场.jpg", items)
        self.assertEqual("", items["2019-婚礼现场.jpg"]["original_filename"])

    def test_blank_original_filename_falls_back(self) -> None:
        """原始名为空白字符串时同样回退，不能展示成空标题。"""
        target = self.image_directory / "blank-original.jpg"
        target.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            connection.execute(
                "INSERT INTO photo_scores (path,original_filename,analysis_status,"
                "is_deleted,created_at,updated_at,version) "
                "VALUES (?,'   ','succeeded',0,?,?,1)",
                (str(target), TEST_TIMESTAMP, TEST_TIMESTAMP),
            )

        self.assertIn("blank-original.jpg", self._list_items())

    def test_photos_page_renders_original_filename(self) -> None:
        """照片列表页面上出现原始名，不出现随机磁盘名。"""
        _photo_id, stored_name = self._create_uploaded_photo("海边日落.jpg")

        body = self.client.get("/admin/photos").get_data(as_text=True)

        self.assertIn("海边日落.jpg", body)
        self.assertNotIn(stored_name, body)

    def test_detail_page_shows_both_names(self) -> None:
        """详情页展示原始名做标题，并保留存储文件名供排查。"""
        photo_id, stored_name = self._create_uploaded_photo("生日蜡烛.jpg")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn("生日蜡烛.jpg", body)
        self.assertIn("存储文件名", body)
        self.assertIn(stored_name, body)

    def test_detail_page_hides_stored_name_when_identical(self) -> None:
        """扫描入库的照片两个名字相同，不重复展示存储文件名。"""
        photo_id = self.create_photo("same-name.jpg")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn("same-name.jpg", body)
        self.assertNotIn("存储文件名", body)

    def test_trash_list_uses_display_name(self) -> None:
        """回收站同样展示可读名，并保留删除前的完整路径。"""
        stored_name = f"{uuid.uuid4().hex}.jpg"
        original_path = str(
            self.image_directory / "uploads" / "2026" / "08" / stored_name
        )
        with self.database() as connection:
            connection.execute(
                "INSERT INTO photo_scores (path,original_filename,original_path,"
                "analysis_status,is_deleted,deleted_at,created_at,updated_at,version) "
                "VALUES (?,?,?,'succeeded',1,?,?,?,1)",
                (
                    str(self.image_directory / ".trash" / stored_name),
                    "毕业典礼.jpg",
                    original_path,
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                ),
            )

        with self.application.test_request_context("/admin/trash"):
            service = self.application.extensions["inktime_services"]["photo_lifecycle"]
            result = service.list_trash(1, 24)

        self.assertEqual("毕业典礼.jpg", result["items"][0]["display_name"])
        self.assertEqual(original_path, result["items"][0]["original_path"])
