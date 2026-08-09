"""公开照片接口的临时数据库集成测试。"""

from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


class PublicPhotosTestCase(TemporaryDatabaseTestCase):
    """使用 Flask 测试客户端验证公开照片查询的真实仓储契约。"""

    def test_only_visible_photos_are_paginated_with_media_contract(self) -> None:
        """验证 legacy 和 succeeded 可见，而 pending 与已删除照片不进入分页结果。"""
        legacy_id = self.create_photo(
            "legacy-visible.jpg",
            analysis_status="legacy",
            date_taken="2024:04:01 10:00:00",
        )
        succeeded_id = self.create_photo(
            "succeeded-visible.jpg",
            analysis_status="succeeded",
            date_taken="2023:04:01 10:00:00",
        )
        self.create_photo(
            "pending-hidden.jpg",
            analysis_status="pending",
            date_taken="2025:04:01 10:00:00",
        )
        self.create_photo(
            "deleted-hidden.jpg",
            analysis_status="succeeded",
            is_deleted=1,
            date_taken="2026:04:01 10:00:00",
        )

        app = create_app(self.application_config())
        client = app.test_client()

        first_response = client.get("/api/photos?page=1&limit=1")
        self.assertEqual(200, first_response.status_code)
        first_payload = first_response.get_json()
        self.assertEqual("ok", first_payload["status"])
        self.assertEqual(2, first_payload["data"]["total"])
        self.assertEqual(1, first_payload["data"]["page"])
        self.assertEqual(1, first_payload["data"]["limit"])
        self.assertEqual(1, len(first_payload["data"]["items"]))
        first_item = first_payload["data"]["items"][0]
        self.assertEqual(legacy_id, first_item["id"])
        self.assertEqual("legacy-visible.jpg", first_item["title"])
        self.assertTrue(first_item["thumbnail_url"].startswith("/api/photo/thumbnail?path="))
        self.assertTrue(first_item["full_url"].startswith("/api/photo/full?path="))

        second_response = client.get("/api/photos?page=2&limit=1")
        self.assertEqual(200, second_response.status_code)
        second_payload = second_response.get_json()
        self.assertEqual("ok", second_payload["status"])
        self.assertEqual(2, second_payload["data"]["page"])
        self.assertEqual(2, second_payload["data"]["total"])
        self.assertEqual(succeeded_id, second_payload["data"]["items"][0]["id"])
        self.assertEqual("succeeded-visible.jpg", second_payload["data"]["items"][0]["title"])
