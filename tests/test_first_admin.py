"""验证首个管理员只能在一个立即写事务中创建一次。"""

import threading
from concurrent.futures import ThreadPoolExecutor

from src.database import write_transaction
from src.server.repositories import (
    AdminUserRepository,
    FirstAdminAlreadyCreatedError,
)
from tests.support import TemporaryDatabaseTestCase


class AdminUserRepositoryFirstAdminTestCase(TemporaryDatabaseTestCase):
    """验证管理员表空状态检查与插入具有原子性。"""

    def repository(self) -> AdminUserRepository:
        """构造每次首次写入都打开独立立即写事务的仓储。"""
        return AdminUserRepository(
            lambda: None,
            lambda: write_transaction(self.database_path),
        )

    def test_concurrent_create_first_admin_allows_exactly_one(self) -> None:
        """两个不同用户名并发首次创建时最终只能插入一行。"""
        repository = self.repository()
        barrier = threading.Barrier(2)

        def attempt(username: str):
            barrier.wait(timeout=5)
            try:
                return ("created", repository.create_first_admin(username, "hash"))
            except FirstAdminAlreadyCreatedError:
                return ("rejected", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("first-admin", "second-admin")))

        self.assertEqual(1, sum(result[0] == "created" for result in results))
        self.assertEqual(1, sum(result[0] == "rejected" for result in results))
        with self.database() as connection:
            count = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        self.assertEqual(1, count)

    def test_create_first_admin_rejects_when_any_admin_exists(self) -> None:
        """已有任意管理员时，即使用户名不同也必须拒绝首次创建。"""
        self.create_admin_user("existing-admin")

        with self.assertRaises(FirstAdminAlreadyCreatedError):
            self.repository().create_first_admin("different-admin", "hash")

        with self.database() as connection:
            count = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        self.assertEqual(1, count)
