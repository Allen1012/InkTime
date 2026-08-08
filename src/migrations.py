"""InkTime 的版本化 SQLite 数据库迁移执行器。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from src.database import database_connection, write_transaction

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MIGRATIONS_DIR = ROOT_DIR / "sql" / "migrations"
MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    """描述一个按版本排序且具有内容校验值的 SQL 迁移。"""

    version: int
    name: str
    path: Path
    checksum: str
    sql: str


def _load_migrations(migrations_dir: Path) -> List[Migration]:
    """读取并校验迁移文件名，返回按版本排序的迁移列表。"""
    migrations: List[Migration] = []
    seen_versions = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_FILE_PATTERN.match(path.name)
        if not match:
            raise RuntimeError(f"迁移文件名不合法: {path.name}")
        version = int(match.group(1))
        if version in seen_versions:
            raise RuntimeError(f"迁移版本重复: {version}")
        seen_versions.add(version)
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                name=match.group(2),
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8").strip(),
            )
        )
    if not migrations:
        raise RuntimeError(f"没有找到迁移文件: {migrations_dir}")
    return migrations


def _ensure_migration_table(connection) -> None:
    """创建迁移台账；该操作由外层迁移事务统一提交。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _column_exists(connection, table: str, column: str) -> bool:
    """判断指定数据表是否已存在目标字段。"""
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _is_already_satisfied(connection, migration: Migration) -> bool:
    """识别迁移体系引入前已存在的结构，避免重复执行不可幂等 DDL。"""
    if migration.version == 2:
        return _column_exists(connection, "photo_scores", "date_source")
    return False


def migrate_database(database_path: Path, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> List[str]:
    """在一个立即写事务中按顺序应用全部待执行迁移。

    Args:
        database_path: 必须由调用方明确提供的目标数据库路径。
        migrations_dir: SQL 迁移文件目录。

    Returns:
        本次新增或采纳的迁移说明列表；无待执行迁移时返回空列表。
    """
    path = Path(database_path).expanduser().resolve()
    migrations = _load_migrations(Path(migrations_dir))
    applied_now: List[str] = []

    with write_transaction(path) as connection:
        _ensure_migration_table(connection)
        applied = {
            row["version"]: row
            for row in connection.execute(
                "SELECT version, name, checksum FROM schema_migrations"
            ).fetchall()
        }

        for migration in migrations:
            existing = applied.get(migration.version)
            if existing is not None:
                if existing["name"] != migration.name or existing["checksum"] != migration.checksum:
                    raise RuntimeError(
                        f"已执行迁移被修改: version={migration.version}, name={migration.name}"
                    )
                continue

            adopted = _is_already_satisfied(connection, migration)
            if not adopted:
                connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            action = "adopted" if adopted else "applied"
            applied_now.append(f"{migration.version:04d}_{migration.name}:{action}")

    assert_current_schema(path)
    return applied_now


def assert_current_schema(database_path: Path) -> None:
    """确认照片生命周期、审计、管理员和迁移台账满足当前代码要求。"""
    path = Path(database_path).expanduser().resolve()
    with database_connection(path, read_only=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "photo_scores" not in tables:
            raise RuntimeError("数据库缺少 photo_scores 表")
        photo_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(photo_scores)").fetchall()
        }
        required_photo_columns = {
            "id",
            "path",
            "date_source",
            "original_filename",
            "content_sha256",
            "analysis_status",
            "analysis_error",
            "is_deleted",
            "deleted_at",
            "created_at",
            "updated_at",
            "version",
        }
        missing_photo_columns = required_photo_columns - photo_columns
        if missing_photo_columns:
            raise RuntimeError(
                f"photo_scores 缺少必要字段: {sorted(missing_photo_columns)}"
            )

        if "admin_users" not in tables:
            raise RuntimeError("数据库缺少 admin_users 表")
        admin_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(admin_users)").fetchall()
        }
        required_admin_columns = {
            "id",
            "username",
            "password_hash",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
        }
        missing_admin_columns = required_admin_columns - admin_columns
        if missing_admin_columns:
            raise RuntimeError(
                f"admin_users 缺少必要字段: {sorted(missing_admin_columns)}"
            )
        admin_table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'admin_users'"
        ).fetchone()
        normalized_admin_sql = " ".join(str(admin_table["sql"]).upper().split())
        if "USERNAME TEXT NOT NULL COLLATE NOCASE UNIQUE" not in normalized_admin_sql:
            raise RuntimeError("admin_users.username 缺少大小写不敏感唯一约束")

        if "photo_audit_log" not in tables:
            raise RuntimeError("数据库缺少 photo_audit_log 表")
        audit_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(photo_audit_log)").fetchall()
        }
        required_audit_columns = {
            "id",
            "photo_id",
            "admin_user_id",
            "admin_username",
            "action",
            "old_values_json",
            "new_values_json",
            "batch_id",
            "created_at",
        }
        missing_audit_columns = required_audit_columns - audit_columns
        if missing_audit_columns:
            raise RuntimeError(
                f"photo_audit_log 缺少必要字段: {sorted(missing_audit_columns)}"
            )

        if "schema_migrations" not in tables:
            raise RuntimeError("数据库缺少 schema_migrations 表")
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        if migration_count < 19:
            raise RuntimeError("数据库迁移数量少于 19")
