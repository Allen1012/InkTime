"""照片管理写入与审计仓储。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping


MANAGED_PHOTO_FIELDS = """
    id, path, caption, type, reason, side_caption, memory_score, beauty_score, exif_city,
    exif_datetime, date_source, exif_json, analysis_status,
    analysis_error, is_deleted, deleted_at, created_at, updated_at, version
"""
_EDITABLE_COLUMNS = {
    "caption",
    "type",
    "reason",
    "side_caption",
    "memory_score",
    "beauty_score",
    "exif_city",
    "exif_datetime",
    "date_source",
    "exif_json",
    "analysis_status",
}


class PhotoManagementRepository:
    """在请求级连接上执行乐观锁更新和同事务审计。"""

    def __init__(self, connection_provider: Callable[[], sqlite3.Connection]) -> None:
        """保存请求级数据库连接提供器。

        Args:
            connection_provider: 返回当前请求复用连接的函数。
        """
        self._connection_provider = connection_provider

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启立即写事务，数据库异常或业务异常时统一回滚。

        Yields:
            当前请求连接；调用方在该连接上完成照片更新和审计写入。
        """
        connection = self._connection_provider()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def get_for_update(
        self, connection: sqlite3.Connection, photo_id: int
    ) -> sqlite3.Row | None:
        """在当前事务中读取照片管理字段。

        Args:
            connection: 已开启立即写事务的连接。
            photo_id: photo_scores 表的稳定自增编号。

        Returns:
            匹配行；不存在时返回 None。
        """
        return connection.execute(
            f"SELECT {MANAGED_PHOTO_FIELDS} FROM photo_scores WHERE id = ?",
            (photo_id,),
        ).fetchone()

    def optimistic_update(
        self,
        connection: sqlite3.Connection,
        photo_id: int,
        expected_version: int,
        updates: Mapping[str, Any],
        updated_at: str,
    ) -> sqlite3.Row | None:
        """按预期版本更新白名单字段并把版本递增一。

        Args:
            connection: 已开启立即写事务的连接。
            photo_id: photo_scores 表的稳定自增编号。
            expected_version: 客户端读取到的版本号。
            updates: 已校验的数据库字段和值。
            updated_at: 本次修改的协调世界时字符串。

        Returns:
            更新后的行；版本不匹配时返回 None。
        """
        unknown_columns = set(updates) - _EDITABLE_COLUMNS
        if unknown_columns:
            raise ValueError(f"照片更新包含非白名单字段: {sorted(unknown_columns)}")
        assignments = [f"{column} = ?" for column in updates]
        values = [updates[column] for column in updates]
        assignments.extend(("version = version + 1", "updated_at = ?"))
        values.extend((updated_at, photo_id, expected_version))
        cursor = connection.execute(
            f"UPDATE photo_scores SET {', '.join(assignments)} "
            "WHERE id = ? AND version = ?",
            values,
        )
        if cursor.rowcount != 1:
            return None
        return self.get_for_update(connection, photo_id)

    def insert_audit(
        self,
        connection: sqlite3.Connection,
        photo_id: int,
        admin_user_id: int,
        admin_username: str,
        action: str,
        old_values: Mapping[str, Any],
        new_values: Mapping[str, Any],
        batch_id: str | None,
        created_at: str,
    ) -> None:
        """写入不级联删除的照片修改审计快照。

        Args:
            connection: 与照片更新相同的事务连接。
            photo_id: 被修改照片编号。
            admin_user_id: 执行操作的管理员编号。
            admin_username: 操作时的管理员用户名快照。
            action: 稳定的审计行为名称。
            old_values: 修改前业务字段。
            new_values: 修改后业务字段。
            batch_id: 批量操作关联编号；单条操作为 None。
            created_at: 审计发生时间。
        """
        connection.execute(
            "INSERT INTO photo_audit_log "
            "(photo_id, admin_user_id, admin_username, action, old_values_json, "
            "new_values_json, batch_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                photo_id,
                admin_user_id,
                admin_username,
                action,
                json.dumps(dict(old_values), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(new_values), ensure_ascii=False, sort_keys=True),
                batch_id,
                created_at,
            ),
        )
