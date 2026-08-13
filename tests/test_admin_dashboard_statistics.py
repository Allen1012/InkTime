"""后台首页统计卡片的回归测试。

首页此前只有照片总数与元数据覆盖，回收站、分析状态、后台任务和入库时间都
写着「后续阶段接入」。这些字段与数据表早已就位，本用例覆盖接入后的取数口径：
活动照片与回收站照片必须分开计数，两条任务队列要合并统计。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from src.server.admin_jobs import AdminJobRepository
from src.server.app import create_app
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class AdminDashboardStatisticsTestCase(TemporaryDatabaseTestCase):
    """用真实迁移与真实登录校验首页统计取数。"""

    ADMIN_USERNAME = "dashboard-admin"
    ADMIN_PASSWORD = "inktime-dashboard-test-password"

    def setUp(self) -> None:
        """创建应用、管理员并完成登录。"""
        super().setUp()
        self.application = create_app(self.application_config())
        with self.application.app_context():
            self.application.extensions["inktime_services"]["auth"].create_admin(
                self.ADMIN_USERNAME, self.ADMIN_PASSWORD
            )
        with self.database() as connection:
            self.admin_id = int(
                connection.execute(
                    "SELECT id FROM admin_users WHERE username=?",
                    (self.ADMIN_USERNAME,),
                ).fetchone()["id"]
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

    def _dashboard(self) -> dict:
        """在应用上下文内直接取统计结构，避免解析 HTML。"""
        with self.application.test_request_context("/admin"):
            service = self.application.extensions["inktime_services"]["admin_photo"]
            return service.dashboard()

    def _set_created_at(self, photo_id: int, value: str | None) -> None:
        """改写照片入库时间，用于构造近期与陈旧记录。"""
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET created_at=? WHERE id=?", (value, photo_id)
            )

    def test_trash_count_excludes_active_photos(self) -> None:
        """回收站只统计 is_deleted=1，活动照片不计入。"""
        self.create_photo("active-a.jpg")
        self.create_photo("active-b.jpg")
        self.create_photo("trashed-a.jpg", is_deleted=1)

        statistics = self._dashboard()

        self.assertTrue(statistics["trash"]["available"])
        self.assertEqual(1, statistics["trash"]["data"])
        self.assertEqual(2, statistics["total"]["data"])

    def test_analysis_summary_counts_each_status(self) -> None:
        """分析状态逐项计数，且不含回收站照片。"""
        self.create_photo("p-pending.jpg", analysis_status="pending")
        self.create_photo("p-running.jpg", analysis_status="running")
        self.create_photo("p-failed-1.jpg", analysis_status="failed")
        self.create_photo("p-failed-2.jpg", analysis_status="failed")
        self.create_photo("p-ok.jpg", analysis_status="succeeded")
        self.create_photo("p-legacy.jpg", analysis_status="legacy")
        # 回收站里的失败照片不应该出现在首页待处理数字里
        self.create_photo("p-trashed.jpg", analysis_status="failed", is_deleted=1)

        data = self._dashboard()["analysis"]["data"]

        self.assertEqual(1, data["pending_count"])
        self.assertEqual(1, data["running_count"])
        self.assertEqual(2, data["failed_count"])
        self.assertEqual(1, data["succeeded_count"])
        self.assertEqual(1, data["legacy_count"])

    def test_job_summary_merges_both_queues(self) -> None:
        """照片队列与维护队列的同状态数量相加后展示。"""
        photo_id = self.create_photo("job-source.jpg")
        AdminJobRepository(self.database_path, max_attempts=3).enqueue(
            photo_id, "analyze_photo", self.admin_id, {}
        )
        with self.database() as connection:
            connection.execute(
                "INSERT INTO admin_maintenance_jobs (job_type,status,payload_json,"
                "created_by_username,created_at,updated_at) "
                "VALUES ('cleanup_expired_trash','pending','{}',?,?,?)",
                (self.ADMIN_USERNAME, TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO admin_maintenance_jobs (job_type,status,payload_json,"
                "created_by_username,created_at,updated_at) "
                "VALUES ('render_display','failed','{}',?,?,?)",
                (self.ADMIN_USERNAME, TEST_TIMESTAMP, TEST_TIMESTAMP),
            )

        data = self._dashboard()["jobs"]["data"]

        self.assertEqual(2, data["pending_count"])
        self.assertEqual(0, data["running_count"])
        self.assertEqual(1, data["failed_count"])

    def test_recent_count_only_includes_window(self) -> None:
        """近 7 天只统计窗口内入库的照片，空值与陈旧记录都排除。"""
        now = datetime.now(timezone.utc)
        fresh = self.create_photo("fresh.jpg")
        stale = self.create_photo("stale.jpg")
        empty = self.create_photo("no-created-at.jpg")
        self._set_created_at(fresh, (now - timedelta(days=2)).isoformat(timespec="seconds"))
        self._set_created_at(stale, (now - timedelta(days=30)).isoformat(timespec="seconds"))
        self._set_created_at(empty, None)

        statistics = self._dashboard()

        self.assertTrue(statistics["recent"]["available"])
        self.assertEqual(1, statistics["recent"]["data"])

    def test_dashboard_page_renders_without_stage_notice(self) -> None:
        """首页正常渲染，且不再出现已过期的阶段边界说明。"""
        self.create_photo("render.jpg", analysis_status="failed")

        response = self.client.get("/admin")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertNotIn("阶段边界", body)
        self.assertNotIn("后续阶段", body)
        self.assertIn("分析失败", body)
        self.assertIn("回收站", body)
        # 失败项应带筛选参数直达照片列表
        self.assertIn("analysis_status=failed", body)

    def test_failed_cards_link_to_actionable_pages(self) -> None:
        """失败卡片是入口而非纯数字，指向可直接处理的页面。"""
        self.create_photo("linked.jpg", analysis_status="failed")

        body = self.client.get("/admin").get_data(as_text=True)

        self.assertRegex(body, r'href="[^"]*analysis_status=failed[^"]*"[^>]*>')
        self.assertIn('href="/admin/jobs"', body)
        self.assertIn('href="/admin/trash"', body)
