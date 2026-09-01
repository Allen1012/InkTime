"""模型厂商档案的读写与审计仓储。

密钥刻意不走通用读取路径：`list_all`、`get`、`resolve_enabled` 一律不返回 `api_key`，
需要密钥的调用方必须显式调 `secret_for`。这样「把整行档案顺手塞进接口响应或日志」
就不会泄露密钥，而不是靠每个调用方自己记得脱敏。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from src.database import database_connection, write_transaction


# 对外可见列，刻意不含 api_key。api_key_hint 是末四位，仅用于界面辨认填的是哪个密钥。
PUBLIC_COLUMNS = (
    "id",
    "name",
    "base_url",
    # model_name 是可选模型池（分号分隔），active_model 是其中当前启用的那一个。
    # 分成两列而不是只存启用值：切换模型时不该要求重新抄一遍全部可选模型。
    "model_name",
    "active_model",
    # 厂商特有的额外请求体参数（JSON 对象文本）。放档案而不是全局配置，是因为它天生
    # 因厂商而异：`enable_thinking` 只有千问认，硬传给别家可能直接 400。
    "request_options",
    "api_key_hint",
    "timeout_seconds",
    "max_long_edge",
    "is_enabled",
    "version",
    "created_at",
    "updated_at",
)
_PUBLIC_SELECT = ",".join(PUBLIC_COLUMNS)
# 可写列白名单，与 PUBLIC_COLUMNS 不同：api_key 与 api_key_hint 可写但不可读。
_WRITABLE_COLUMNS = (
    "name",
    "base_url",
    "model_name",
    "active_model",
    "request_options",
    "api_key",
    "api_key_hint",
    "timeout_seconds",
    "max_long_edge",
    "is_enabled",
)
# 审计里替换密钥的固定占位文本，与配置审计的展示措辞保持一致。
REDACTED_TEXT = "已脱敏"
_SECRET_COLUMNS = frozenset({"api_key"})


def redact(values: Mapping[str, Any]) -> dict[str, Any]:
    """把待写审计的字段里的密钥换成占位文本。

    脱敏必须发生在写入前而不是展示时：数据库备份、`sqlite3` 直连与误导出都会绕过
    展示层，只在页面上显示「已脱敏」等于没脱敏。

    Args:
        values: 原始字段映射。

    Returns:
        密钥已替换为占位文本的新映射，其余字段原样保留。
    """
    return {
        key: (REDACTED_TEXT if key in _SECRET_COLUMNS and value else value)
        for key, value in values.items()
    }


class ModelProviderRepository:
    """在独立短写事务里维护厂商档案，并在同事务写入审计。"""

    def __init__(self, database_path: Any) -> None:
        """记录数据库位置。

        Args:
            database_path: 厂商档案与审计所在的 SQLite 文件。
        """
        self.database_path = database_path

    def list_all(self) -> list[dict[str, Any]]:
        """按名称排序返回全部档案，不含密钥。"""
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, provider_id: int) -> dict[str, Any] | None:
        """按编号读取一条档案，不含密钥。"""
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers WHERE id=?",
                (int(provider_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        """按名称读取一条档案（含停用的），不含密钥。"""
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers WHERE name=?",
                (str(name),),
            ).fetchone()
        return dict(row) if row else None

    def resolve_enabled(
        self, name: str, connection: Any | None = None
    ) -> dict[str, Any] | None:
        """按名称读取启用中的公开档案，可复用任务认领事务连接。

        Args:
            name: 厂商名称。
            connection: 可选现有 SQLite 连接；传入后不负责关闭。

        Returns:
            不含密钥的公开档案；停用或不存在时返回空值。
        """
        if connection is not None:
            row = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers "
                "WHERE name=? AND is_enabled=1",
                (str(name),),
            ).fetchone()
            return dict(row) if row else None
        with database_connection(self.database_path, read_only=True) as current:
            row = current.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers "
                "WHERE name=? AND is_enabled=1",
                (str(name),),
            ).fetchone()
        return dict(row) if row else None

    def secret_for(self, name: str) -> str:
        """按名称读取密钥原值，供执行时现读现传。

        这是唯一会返回密钥的方法。返回空串表示该档案不存在、已停用或未配密钥，
        三种情况调用方的处置一致：按无密钥发起请求，由上游决定是否拒绝。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT api_key FROM model_providers WHERE name=? AND is_enabled=1",
                (str(name),),
            ).fetchone()
        return str(row["api_key"]) if row else ""

    def create(
        self,
        values: Mapping[str, Any],
        actor_user_id: int | None,
        actor_username: str,
        timestamp: str,
    ) -> dict[str, Any]:
        """插入一条档案并在同事务写入审计。

        Args:
            values: 已由服务层校验与归一化的可写字段。
            actor_user_id: 操作管理员编号。
            actor_username: 操作管理员用户名快照。
            timestamp: 本次操作时间。

        Returns:
            新建档案的公开字段。

        Raises:
            sqlite3.IntegrityError: 名称重复时由唯一约束抛出，交服务层转换。
        """
        payload = self._writable(values)
        columns = ",".join(payload)
        placeholders = ",".join("?" for _ in payload)
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                f"INSERT INTO model_providers ({columns},version,created_at,updated_at) "
                f"VALUES ({placeholders},1,?,?)",
                (*payload.values(), timestamp, timestamp),
            )
            provider_id = int(cursor.lastrowid)
            self._insert_audit(
                connection, provider_id, str(payload["name"]), "provider_create",
                {}, payload, actor_user_id, actor_username, timestamp,
            )
            row = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers WHERE id=?",
                (provider_id,),
            ).fetchone()
        return dict(row)

    def update(
        self,
        provider_id: int,
        expected_version: int,
        values: Mapping[str, Any],
        actor_user_id: int | None,
        actor_username: str,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """按预期版本更新档案并递增版本，同事务写入审计。

        Returns:
            更新后的公开字段；档案不存在或版本不匹配时返回空值。
        """
        payload = self._writable(values)
        if not payload:
            raise ValueError("厂商更新必须至少包含一个可写字段")
        assignments = ",".join(f"{column}=?" for column in payload)
        with write_transaction(self.database_path) as connection:
            # 审计需要改动前的原值，且必须在同一事务内读，避免与并发更新交错。
            before = connection.execute(
                "SELECT * FROM model_providers WHERE id=?", (int(provider_id),)
            ).fetchone()
            if before is None:
                return None
            cursor = connection.execute(
                f"UPDATE model_providers SET {assignments},version=version+1,updated_at=? "
                "WHERE id=? AND version=?",
                (*payload.values(), timestamp, int(provider_id), int(expected_version)),
            )
            if cursor.rowcount != 1:
                return None
            old_values = {column: before[column] for column in payload}
            self._insert_audit(
                connection, int(provider_id), str(before["name"]), "provider_update",
                old_values, payload, actor_user_id, actor_username, timestamp,
            )
            row = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers WHERE id=?",
                (int(provider_id),),
            ).fetchone()
        return dict(row)

    def delete(
        self,
        provider_id: int,
        expected_version: int,
        actor_user_id: int | None,
        actor_username: str,
        timestamp: str,
    ) -> dict[str, Any] | None:
        """按预期版本删除档案并保留审计。

        审计行的 `provider_id` 允许悬空、且不加外键：档案删掉之后这条记录仍要能查到
        「谁在什么时候删了哪个厂商」，因此另存一份删除那一刻的名称快照。

        Returns:
            被删档案的公开字段；不存在或版本不匹配时返回空值。
        """
        with write_transaction(self.database_path) as connection:
            before = connection.execute(
                f"SELECT {_PUBLIC_SELECT} FROM model_providers WHERE id=?",
                (int(provider_id),),
            ).fetchone()
            if before is None:
                return None
            cursor = connection.execute(
                "DELETE FROM model_providers WHERE id=? AND version=?",
                (int(provider_id), int(expected_version)),
            )
            if cursor.rowcount != 1:
                return None
            removed = dict(before)
            self._insert_audit(
                connection, int(provider_id), str(before["name"]), "provider_delete",
                removed, {}, actor_user_id, actor_username, timestamp,
            )
        return removed

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """按时间倒序返回厂商审计记录。

        Args:
            limit: 返回条数，收敛到 1 至 100。

        Returns:
            审计记录列表；密钥在写入时已脱敏，这里无需再处理。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM model_provider_audit ORDER BY created_at DESC, id DESC "
                "LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _writable(values: Mapping[str, Any]) -> dict[str, Any]:
        """过滤出可写列，拒绝白名单外的字段。"""
        unknown = sorted(set(values) - set(_WRITABLE_COLUMNS))
        if unknown:
            raise ValueError(f"厂商档案包含非白名单字段: {unknown}")
        return {
            column: values[column]
            for column in _WRITABLE_COLUMNS
            if column in values
        }

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        provider_id: int | None,
        provider_name: str,
        action: str,
        old_values: Mapping[str, Any],
        new_values: Mapping[str, Any],
        actor_user_id: int | None,
        actor_username: str,
        timestamp: str,
    ) -> None:
        """写入已脱敏的厂商审计快照。"""
        connection.execute(
            "INSERT INTO model_provider_audit (provider_id,provider_name,action,"
            "old_values_json,new_values_json,modified_by_user_id,modified_by_username,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                provider_id,
                provider_name,
                action,
                json.dumps(redact(old_values), ensure_ascii=False, sort_keys=True),
                json.dumps(redact(new_values), ensure_ascii=False, sort_keys=True),
                actor_user_id,
                actor_username,
                timestamp,
            ),
        )
