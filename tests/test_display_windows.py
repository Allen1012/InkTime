"""展示页生效时间段在服务端的行为测试。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.configuration import ConfigurationActor
from src.server import gallery
from src.server.app import create_app
from src.server.services import DisplayService
from tests.support import TemporaryDatabaseTestCase


# 2026-08-12 是周三，08-15 是周六
WEDNESDAY_MORNING = datetime(2026, 8, 12, 8, 0)
WEDNESDAY_NIGHT = datetime(2026, 8, 12, 23, 30)
SATURDAY_MORNING = datetime(2026, 8, 15, 8, 0)


class DisplayActiveWindowsTestCase(TemporaryDatabaseTestCase):
    """校验生效时间段内外的取片、记账与休息期画面。"""

    def setUp(self) -> None:
        """准备应用、管理员与两张可展示照片。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.first_id = self.create_photo("first.jpg", analysis_status="succeeded")
        self.second_id = self.create_photo("second.jpg", analysis_status="succeeded")
        self.app = create_app(self.application_config())
        self.configuration = self.app.extensions["inktime_services"]["configuration"]

    def change(self, **values: Any) -> None:
        """提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def service(self, moment: datetime) -> DisplayService:
        """构造使用固定时刻的展示服务。"""
        return DisplayService(
            gallery, self.database_path, self.configuration, clock=lambda: moment
        )

    def display_stats(self) -> dict[int, tuple[int, str | None]]:
        """读取当前展示计数快照。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT photo_id,show_count,last_shown_at FROM display_stats "
                "WHERE channel='web'"
            ).fetchall()
        return {
            int(row["photo_id"]): (int(row["show_count"]), row["last_shown_at"])
            for row in rows
        }

    def test_without_windows_behavior_is_unchanged(self) -> None:
        """验证未配置时间段时任意时刻都正常取片并记账。"""
        payload, status = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, sum(count for count, _ in self.display_stats().values()))

    def test_inside_window_picks_and_records(self) -> None:
        """验证生效时间段内取片并累加展示计数。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00;18:00-23:00")

        payload, status = self.service(WEDNESDAY_MORNING).next_photo(None)

        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, sum(count for count, _ in self.display_stats().values()))

    def test_outside_window_returns_idle_and_never_records(self) -> None:
        """验证休息期返回 idle 且展示计数完全不变。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00;18:00-23:00")
        self.service(WEDNESDAY_MORNING).next_photo(None)
        before = self.display_stats()

        payload, status = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual(200, status)
        self.assertEqual("idle", payload["status"])
        self.assertEqual("outside_active_windows", payload["reason"])
        self.assertEqual(before, self.display_stats())

    def test_freeze_mode_returns_the_last_shown_photo(self) -> None:
        """验证 freeze 模式返回生效时间段内最后展示的那张。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00")
        shown, _ = self.service(WEDNESDAY_MORNING).next_photo(None)
        shown_id = shown["data"]["id"]

        payload, _ = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual("freeze", payload["idle_mode"])
        self.assertEqual(shown_id, payload["data"]["id"])
        self.assertIn("thumbnail_url", payload["data"])

    def test_freeze_mode_falls_back_to_rest_without_history(self) -> None:
        """验证从未展示过时 freeze 退化为休息文案而不是空白。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00")

        payload, _ = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual("rest", payload["idle_mode"])
        self.assertIsNone(payload["data"])
        self.assertEqual("休息中", payload["message"])

    def test_photo_mode_returns_the_configured_photo(self) -> None:
        """验证 photo 模式固定返回指定编号的照片。"""
        self.change(
            DISPLAY_ACTIVE_WINDOWS="07:00-09:00",
            DISPLAY_IDLE_MODE="photo",
            DISPLAY_IDLE_PHOTO_ID=self.second_id,
        )

        payload, _ = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual("photo", payload["idle_mode"])
        self.assertEqual(self.second_id, payload["data"]["id"])
        self.assertEqual({}, self.display_stats())

    def test_photo_mode_falls_back_when_photo_unavailable(self) -> None:
        """验证指定照片被删除后回退为停在最后一张。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00")
        shown, _ = self.service(WEDNESDAY_MORNING).next_photo(None)
        self.change(DISPLAY_IDLE_MODE="photo", DISPLAY_IDLE_PHOTO_ID=self.second_id)
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET is_deleted=1 WHERE id=?", (self.second_id,)
            )

        payload, _ = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual("freeze", payload["idle_mode"])
        self.assertEqual(shown["data"]["id"], payload["data"]["id"])

    def test_rest_mode_returns_configured_text(self) -> None:
        """验证 rest 模式返回自定义文案且不带照片。"""
        self.change(
            DISPLAY_ACTIVE_WINDOWS="07:00-09:00",
            DISPLAY_IDLE_MODE="rest",
            DISPLAY_REST_TEXT="晚安",
        )

        payload, _ = self.service(WEDNESDAY_NIGHT).next_photo(None)

        self.assertEqual("rest", payload["idle_mode"])
        self.assertIsNone(payload["data"])
        self.assertEqual("晚安", payload["message"])

    def test_idle_response_carries_resume_hint_and_backoff(self) -> None:
        """验证休息期返回下一段开始时刻与退避秒数。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="07:00-09:00;18:00-23:00")

        payload, _ = self.service(datetime(2026, 8, 12, 9, 0)).next_photo(None)

        self.assertEqual("2026-08-12T18:00:00", payload["resume_at"])
        self.assertEqual(300, payload["next_check_after_sec"])

        payload, _ = self.service(datetime(2026, 8, 12, 17, 58)).next_photo(None)
        self.assertEqual("2026-08-12T18:00:00", payload["resume_at"])
        self.assertEqual(120, payload["next_check_after_sec"])

    def test_weekday_windows_differ_between_weekday_and_weekend(self) -> None:
        """验证按星期配置在工作日与周末分别生效。"""
        self.change(
            DISPLAY_ACTIVE_WINDOWS="Mon-Fri@18:00-23:00;Sat,Sun@07:00-23:00"
        )

        weekday_morning, _ = self.service(WEDNESDAY_MORNING).next_photo(None)
        weekend_morning, _ = self.service(SATURDAY_MORNING).next_photo(None)

        self.assertEqual("idle", weekday_morning["status"])
        self.assertEqual("ok", weekend_morning["status"])

    def test_invalid_windows_are_rejected_on_save(self) -> None:
        """验证非法时间段在保存时被拒绝且不落库。"""
        from src.configuration import ConfigurationValidationError

        version = self.configuration.list_admin_settings()["version"]
        with self.assertRaises(ConfigurationValidationError) as captured:
            self.change(DISPLAY_ACTIVE_WINDOWS="25:00-26:00")

        self.assertIn("小时", captured.exception.errors["DISPLAY_ACTIVE_WINDOWS"])
        self.assertEqual(
            version, self.configuration.list_admin_settings()["version"]
        )

    def test_api_endpoint_returns_idle_payload(self) -> None:
        """验证公开接口在休息期返回 idle，且默认时钟链路可用。"""
        self.change(DISPLAY_ACTIVE_WINDOWS="00:00-00:01")
        client = self.app.test_client()

        response = client.get("/api/display/next")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("idle", payload["status"])
        self.assertIn(payload["idle_mode"], ("freeze", "rest"))
