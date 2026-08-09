"""为 InkTime 自动测试提供临时数据库和真实迁移夹具。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.migrations import migrate_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DATABASE_PATHS = {
    (PROJECT_ROOT / "data" / "photos.db").resolve(),
    (PROJECT_ROOT / "photos.db").resolve(),
}
TEST_TIMESTAMP = "2026-01-01T00:00:00+00:00"


class TemporaryDatabaseTestCase(unittest.TestCase):
    """为每个测试创建独立临时目录，并迁移一份全新的数据库。"""

    def setUp(self) -> None:
        """创建隔离路径，明确排除正式数据库后应用版本 1 至 47 迁移。"""
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="inktime-tests-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name).resolve()
        self.database_path = (self.temporary_path / "database" / "test.db").resolve()
        self.image_directory = (self.temporary_path / "images").resolve()
        self.output_directory = (self.temporary_path / "output").resolve()
        self.image_directory.mkdir(parents=True)
        self.output_directory.mkdir(parents=True)

        self.assertNotIn(self.database_path, FORBIDDEN_DATABASE_PATHS)
        self.assertTrue(self.database_path.is_relative_to(self.temporary_path))
        applied = migrate_database(self.database_path)
        versions = [int(item.split("_", 1)[0]) for item in applied]
        self.assertEqual(list(range(1, 48)), versions)
        self.assertEqual(47, len(applied))
        self.assertTrue(applied[-1].startswith("0047_"))

    def tearDown(self) -> None:
        """在临时目录释放前确认每个测试都没有留下外键违规。"""
        self.assert_foreign_keys_valid()

    @contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        """打开仅指向当前临时数据库的连接，并在退出时提交和关闭。"""
        self.assertNotIn(self.database_path, FORBIDDEN_DATABASE_PATHS)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def assert_foreign_keys_valid(self) -> None:
        """断言当前临时数据库不存在任何外键引用违规。"""
        with self.database() as connection:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual([], violations)

    def create_admin_user(self, username: str = "test-admin") -> int:
        """创建满足后台任务外键约束但不用于真实认证的管理员夹具。"""
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO admin_users (username,password_hash,created_at,updated_at) "
                "VALUES (?,?,?,?)",
                (username, "not-a-real-password-hash", TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            return int(cursor.lastrowid)

    def create_photo(
        self,
        filename: str,
        *,
        analysis_status: str = "legacy",
        is_deleted: int = 0,
        version: int = 1,
        caption: str | None = None,
        date_taken: str = "2024:01:01 12:00:00",
    ) -> int:
        """创建位于临时图片目录且包含公开接口与任务所需字段的照片夹具。"""
        path = (self.image_directory / filename).resolve()
        self.assertTrue(path.is_relative_to(self.image_directory))
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,caption,type,memory_score,beauty_score,"
                "exif_datetime,side_caption,analysis_status,analysis_error,is_deleted,"
                "created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(path),
                    caption or f"caption-{filename}",
                    "family",
                    88.0,
                    77.0,
                    date_taken,
                    f"side-{filename}",
                    analysis_status,
                    None,
                    is_deleted,
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                    version,
                ),
            )
            return int(cursor.lastrowid)

    def read_job(self, job_id: int) -> dict[str, Any]:
        """按任务编号读取当前完整任务状态。"""
        with self.database() as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def read_photo(self, photo_id: int) -> dict[str, Any]:
        """按照片编号读取当前完整照片状态。"""
        with self.database() as connection:
            row = connection.execute("SELECT * FROM photo_scores WHERE id=?", (photo_id,)).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def read_events(self, job_id: int) -> list[dict[str, Any]]:
        """按写入顺序读取指定任务的全部审计事件。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_job_events WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def expire_job_lease(self, job_id: int) -> None:
        """把运行任务的租约改为确定的过去时间，以触发真实恢复逻辑。"""
        with self.database() as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET lease_expires_at=? WHERE id=? AND status='running'",
                ("2000-01-01T00:00:00+00:00", job_id),
            )
        self.assertEqual(1, cursor.rowcount)

    def application_config(self) -> dict[str, Any]:
        """返回完全绑定临时路径且禁用生产安全要求的 Flask 测试配置。"""
        return {
            "APP_ENV": "testing",
            "TESTING": True,
            "DB_PATH": self.database_path,
            "IMAGE_DIR": self.image_directory,
            "BIN_OUTPUT_DIR": self.output_directory,
            "SECRET_KEY": "inktime-tests-only-secret-key-not-for-production",
            "SESSION_COOKIE_SECURE": False,
            "WTF_CSRF_CHECK_DEFAULT": False,
            "JOB_MAX_ATTEMPTS": 3,
            "JOB_LEASE_SECONDS": 30,
            "JOB_RENEW_SECONDS": 10,
            "JOB_POLL_SECONDS": 1,
            "ENABLE_REVIEW_WEBUI": False,
            "ENABLE_FILE_BROWSER": False,
        }
