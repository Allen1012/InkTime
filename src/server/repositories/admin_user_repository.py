"""管理员账号仓储，集中管理参数化 SQL 和事务边界。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, ContextManager


class DuplicateAdminUsernameError(Exception):
    """表示管理员用户名违反大小写不敏感唯一约束。"""


class FirstAdminAlreadyCreatedError(Exception):
    """表示系统已经存在管理员，禁止再次执行首次管理员创建。"""


class AdminUserRepository:
    """通过请求级连接查询管理员，并用独立短事务执行原子写入。"""

    def __init__(
        self,
        connection_provider: Callable[[], sqlite3.Connection],
        write_transaction_provider: Callable[[], ContextManager[sqlite3.Connection]],
    ) -> None:
        """初始化查询连接与独立写事务提供器。

        Args:
            connection_provider: 返回当前 Flask 上下文复用连接的函数。
            write_transaction_provider: 返回已开启立即写事务的上下文管理器。
        """
        self._connection_provider = connection_provider
        self._write_transaction_provider = write_transaction_provider

    def find_by_id(self, admin_user_id: int) -> sqlite3.Row | None:
        """按主键读取 Flask-Login 恢复会话所需的管理员字段。

        Args:
            admin_user_id: admin_users 表主键。

        Returns:
            管理员行对象；不存在时返回 None。
        """
        return self._connection_provider().execute(
            "SELECT id, username, password_hash, is_active, last_login_at "
            "FROM admin_users WHERE id = ?",
            (admin_user_id,),
        ).fetchone()

    def find_by_username(self, username: str) -> sqlite3.Row | None:
        """按大小写不敏感唯一用户名读取认证字段。

        Args:
            username: 已去除首尾空白的登录用户名。

        Returns:
            管理员行对象；不存在时返回 None。
        """
        return self._connection_provider().execute(
            "SELECT id, username, password_hash, is_active, last_login_at "
            "FROM admin_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()

    def has_admins(self) -> bool:
        """判断系统是否已经存在任意管理员，供首次设置页面决定展示状态。

        Returns:
            至少存在一个管理员时返回 True，否则返回 False。
        """
        row = self._connection_provider().execute(
            "SELECT EXISTS(SELECT 1 FROM admin_users LIMIT 1)"
        ).fetchone()
        return bool(row[0])

    def create(self, username: str, password_hash: str) -> int:
        """创建启用的管理员账号并显式提交，不保存或返回明文密码。

        Args:
            username: 已校验的唯一用户名。
            password_hash: Werkzeug 生成的安全密码哈希。

        Returns:
            新管理员的数据库主键。

        Raises:
            DuplicateAdminUsernameError: 用户名已存在。
        """
        connection = self._connection_provider()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            cursor = connection.execute(
                "INSERT INTO admin_users "
                "(username, password_hash, is_active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (username, password_hash, timestamp, timestamp),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise DuplicateAdminUsernameError(username) from error
        return int(cursor.lastrowid)

    def create_first_admin(self, username: str, password_hash: str) -> int:
        """在独立立即写事务中仅当管理员表为空时创建首个管理员。

        检查与插入必须处于同一事务，避免不同用户名的并发初始化请求
        同时观察到空表后各自创建一条管理员记录。

        Args:
            username: 已校验的首个管理员用户名。
            password_hash: Werkzeug 生成的安全密码哈希。

        Returns:
            首个管理员的数据库主键。

        Raises:
            FirstAdminAlreadyCreatedError: 系统已经存在任意管理员。
            DuplicateAdminUsernameError: 用户名违反数据库唯一约束。
        """
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._write_transaction_provider() as connection:
                exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM admin_users LIMIT 1)"
                ).fetchone()
                if bool(exists[0]):
                    raise FirstAdminAlreadyCreatedError()
                cursor = connection.execute(
                    "INSERT INTO admin_users "
                    "(username, password_hash, is_active, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (username, password_hash, timestamp, timestamp),
                )
                admin_user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise DuplicateAdminUsernameError(username) from error
        return admin_user_id
