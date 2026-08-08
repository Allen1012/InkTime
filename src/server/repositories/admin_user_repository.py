"""管理员账号仓储，集中管理参数化 SQL 和显式提交。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable


class DuplicateAdminUsernameError(Exception):
    """表示管理员用户名违反大小写不敏感唯一约束。"""


class AdminUserRepository:
    """通过请求级连接读取和写入管理员账号。"""

    def __init__(self, connection_provider: Callable[[], sqlite3.Connection]) -> None:
        """初始化仓储。

        Args:
            connection_provider: 返回当前 Flask 上下文复用连接的函数。
        """
        self._connection_provider = connection_provider

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

    def update_last_login(self, admin_user_id: int) -> None:
        """记录成功登录时间并显式提交。

        Args:
            admin_user_id: 成功认证的管理员主键。
        """
        connection = self._connection_provider()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE admin_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, admin_user_id),
        )
        connection.commit()
