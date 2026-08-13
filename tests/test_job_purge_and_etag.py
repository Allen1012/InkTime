"""终态任务清理与任务列表 ETag 条件请求的回归测试。"""

from __future__ import annotations

import re
from typing import Any

from src.server.admin_jobs import AdminJobRepository
from src.server.app import create_app
from src.server.photo_lifecycle import MaintenanceJobRepository, MaintenanceJobService
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class JobPurgeTestCase(TemporaryDatabaseTestCase):
    """验证 purge_completed 只删多余终态任务，且不碰活跃任务。"""

    def _service(self) -> MaintenanceJobService:
        """构造只依赖临时数据库的合并任务服务。"""
        return MaintenanceJobService(
            self.database_path,
            MaintenanceJobRepository(self.database_path),
            AdminJobRepository(self.database_path, max_attempts=3),
        )

    def _insert_terminal_job(
        self, admin_id: int, photo_id: int, status: str, *, with_event: bool = True
    ) -> int:
        """直接写入一条终态照片任务，绕过状态机以便构造清理场景。"""
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO admin_jobs (job_type,status,payload_json,created_by,"
                "photo_id,photo_version,created_at,updated_at,finished_at) "
                "VALUES ('analyze_photo',?,'{}',?,?,1,?,?,?)",
                (status, admin_id, photo_id, TEST_TIMESTAMP, TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            job_id = int(cursor.lastrowid)
            if with_event:
                connection.execute(
                    "INSERT INTO admin_job_events (job_id,event_type,created_at) "
                    "VALUES (?,'enqueued',?)",
                    (job_id, TEST_TIMESTAMP),
                )
        return job_id

    def _count(self, table: str) -> int:
        """统计指定表的当前行数。"""
        with self.database() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_purge_keeps_requested_number_of_terminal_jobs(self) -> None:
        """终态任务超出保留数时，只留下编号最大的 keep 条。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("purge-many.jpg")
        job_ids = [
            self._insert_terminal_job(admin_id, photo_id, "succeeded") for _ in range(25)
        ]

        result = self._service().purge_completed(keep=10)

        self.assertEqual(15, result["photo_purged"])
        self.assertEqual(10, self._count("admin_jobs"))
        with self.database() as connection:
            remaining = [
                int(row["id"])
                for row in connection.execute("SELECT id FROM admin_jobs ORDER BY id")
            ]
        self.assertEqual(sorted(job_ids)[-10:], remaining)

    def test_purge_is_noop_when_terminal_count_within_keep(self) -> None:
        """终态任务不超过保留数时一条都不删。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("purge-few.jpg")
        for _ in range(10):
            self._insert_terminal_job(admin_id, photo_id, "failed")

        result = self._service().purge_completed(keep=10)

        self.assertEqual(0, result["photo_purged"])
        self.assertEqual(10, self._count("admin_jobs"))

    def test_purge_preserves_pending_and_running_jobs(self) -> None:
        """活跃任务不属于终态，无论保留数多小都不能被删。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("purge-active.jpg")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        active = repository.enqueue(photo_id, "analyze_photo", admin_id, {})
        for _ in range(30):
            self._insert_terminal_job(admin_id, photo_id, "canceled")

        self._service().purge_completed(keep=10)

        surviving = self.read_job(active["id"])
        self.assertEqual("pending", surviving["status"])

    def test_purge_removes_events_without_foreign_key_violation(self) -> None:
        """事件日志随任务一起删除；tearDown 的外键检查兜底。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("purge-events.jpg")
        for _ in range(20):
            self._insert_terminal_job(admin_id, photo_id, "succeeded")
        self.assertEqual(20, self._count("admin_job_events"))

        self._service().purge_completed(keep=10)

        self.assertEqual(10, self._count("admin_jobs"))
        self.assertEqual(10, self._count("admin_job_events"))
        self.assert_foreign_keys_valid()

    def test_purge_clamps_keep_to_minimum_ten(self) -> None:
        """keep 小于 10 时按 10 处理，避免误清空任务历史。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("purge-clamp.jpg")
        for _ in range(20):
            self._insert_terminal_job(admin_id, photo_id, "succeeded")

        self._service().purge_completed(keep=0)

        self.assertEqual(10, self._count("admin_jobs"))


class JobListEtagTestCase(TemporaryDatabaseTestCase):
    """验证任务列表接口的 ETag 覆盖了前端会渲染的全部字段。"""

    ADMIN_USERNAME = "etag-admin"
    ADMIN_PASSWORD = "inktime-etag-test-password"

    def setUp(self) -> None:
        """构造绑定临时数据库的应用，并用真实登录流程建立会话。"""
        super().setUp()
        self.photo_id = self.create_photo("etag.jpg")
        self.application = create_app(self.application_config())
        with self.application.app_context():
            self.application.extensions["inktime_services"]["auth"].create_admin(
                self.ADMIN_USERNAME, self.ADMIN_PASSWORD
            )
        with self.database() as connection:
            self.admin_id = int(
                connection.execute(
                    "SELECT id FROM admin_users WHERE username=?", (self.ADMIN_USERNAME,)
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

    def _enqueue(self) -> int:
        """排队一项真实分析任务并返回编号。"""
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        return int(repository.enqueue(self.photo_id, "analyze_photo", self.admin_id, {})["id"])

    def _fetch(self, etag: str | None = None) -> Any:
        """按需带上条件请求头访问任务列表接口。"""
        headers = {"If-None-Match": etag} if etag else {}
        return self.client.get("/api/admin/jobs", headers=headers)

    def test_unchanged_list_returns_not_modified(self) -> None:
        """列表未变化时第二次请求返回 304 且不带响应体。"""
        self._enqueue()
        first = self._fetch()
        self.assertEqual(200, first.status_code)
        etag = first.headers["ETag"]

        second = self._fetch(etag)

        self.assertEqual(304, second.status_code)
        self.assertEqual(etag, second.headers["ETag"])
        self.assertEqual(b"", second.data)

    def test_status_change_invalidates_etag(self) -> None:
        """状态变化后旧 ETag 失效，客户端能拿到新数据。"""
        job_id = self._enqueue()
        etag = self._fetch().headers["ETag"]
        with self.database() as connection:
            connection.execute(
                "UPDATE admin_jobs SET status='running' WHERE id=?", (job_id,)
            )

        response = self._fetch(etag)

        self.assertEqual(200, response.status_code)
        self.assertNotEqual(etag, response.headers["ETag"])

    def test_error_summary_change_invalidates_etag(self) -> None:
        """只有错误信息变化时也必须失效——摘要漏字段会让页面显示过期内容。"""
        job_id = self._enqueue()
        etag = self._fetch().headers["ETag"]
        with self.database() as connection:
            connection.execute(
                "UPDATE admin_jobs SET error_code=?,error_summary=? WHERE id=?",
                ("model_timeout", "视觉模型响应超时", job_id),
            )

        response = self._fetch(etag)

        self.assertEqual(200, response.status_code)
        payload = response.get_json()["data"][0]
        self.assertEqual("model_timeout", payload["error_code"])
