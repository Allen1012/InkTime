"""InkTime 的版本化 SQLite 数据库迁移执行器。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from src.database import database_connection, write_transaction

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MIGRATIONS_DIR = ROOT_DIR / "sql" / "migrations"
MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
SCHEMA_TARGET_VERSION = 55
EXPECTED_SCHEMA_VERSIONS = tuple(range(1, SCHEMA_TARGET_VERSION + 1))


@dataclass(frozen=True)
class Migration:
    """描述一个按版本排序且具有内容校验值的 SQL 迁移。"""

    version: int
    name: str
    path: Path
    checksum: str
    sql: str


def _split_sql_statements(sql: str) -> List[str]:
    """按 SQLite 完整语句边界拆分文本，忽略纯注释尾部。"""
    statements: List[str] = []
    buffer = ""
    candidate = sql.strip()
    if candidate and not candidate.endswith(";"):
        candidate += ";"
    for character in candidate:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            meaningful = "\n".join(
                line for line in buffer.splitlines() if not line.lstrip().startswith("--")
            ).strip()
            if meaningful and meaningful != ";":
                statements.append(buffer.strip())
            buffer = ""
    meaningful_tail = "\n".join(
        line for line in buffer.splitlines() if not line.lstrip().startswith("--")
    ).strip()
    if meaningful_tail:
        statements.append(buffer.strip())
    return statements


def _load_migrations(migrations_dir: Path) -> List[Migration]:
    """读取迁移文件，并要求命名、单语句及版本 1 至 50 连续完整。"""
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
        sql = raw.decode("utf-8").strip()
        statements = _split_sql_statements(sql)
        if len(statements) != 1:
            raise RuntimeError(f"迁移文件必须且只能包含一条 SQL 语句: {path.name}")
        migrations.append(
            Migration(
                version=version,
                name=match.group(2),
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=sql,
            )
        )
    versions = tuple(migration.version for migration in migrations)
    if versions != EXPECTED_SCHEMA_VERSIONS:
        raise RuntimeError(
            "本地迁移文件版本必须恰好连续为 "
            f"1..{SCHEMA_TARGET_VERSION}: actual={list(versions)}"
        )
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


def _validate_migration_history(
    connection, migrations: List[Migration], require_complete: bool
) -> dict[int, sqlite3.Row]:
    """校验数据库迁移台账属于当前程序唯一认可的线性历史。

    迁移执行前允许缺少已知版本，以便按顺序补齐；结构门禁则要求版本集合完整。
    任一未知版本或已知版本的名称、校验值不匹配都表示未来或分叉历史，必须拒绝。
    """
    expected = {migration.version: migration for migration in migrations}
    applied = {
        int(row["version"]): row
        for row in connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    }
    unknown_versions = sorted(set(applied) - set(expected))
    if unknown_versions:
        raise RuntimeError(f"数据库包含当前程序未知的迁移版本: {unknown_versions}")

    for version, row in applied.items():
        migration = expected[version]
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise RuntimeError(
                "数据库迁移身份不匹配: "
                f"version={version}, expected_name={migration.name}, actual_name={row['name']}"
            )

    if require_complete:
        actual_versions = tuple(sorted(applied))
        if actual_versions != EXPECTED_SCHEMA_VERSIONS:
            missing_versions = sorted(set(EXPECTED_SCHEMA_VERSIONS) - set(applied))
            raise RuntimeError(
                "数据库迁移版本必须恰好完整为 "
                f"1..{SCHEMA_TARGET_VERSION}: missing={missing_versions}"
            )
    return applied


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
    """校验迁移历史后，在一个立即写事务中按顺序应用全部已知缺失迁移。

    执行任何待迁移 SQL 前都会拒绝未知版本及已知版本的名称或校验值分叉。

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
        applied = _validate_migration_history(
            connection, migrations, require_complete=False
        )

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


def pending_migration_versions(database_path: Path) -> List[int]:
    """只读返回已有数据库缺失的迁移版本，并拒绝未知或分叉历史。

    该函数不写入数据库，供启动期在决定是否备份与升级前做无副作用判断。
    缺少迁移台账的数据库视为尚未纳入迁移体系，需要补齐全部已知版本。

    Args:
        database_path: 已存在的数据库路径。

    Returns:
        升序排列的缺失迁移版本；返回空列表表示结构版本已是最新。

    Raises:
        RuntimeError: 数据库包含当前程序未知的版本，或已知版本身份不匹配。
    """
    path = Path(database_path).expanduser().resolve()
    migrations = _load_migrations(DEFAULT_MIGRATIONS_DIR)
    with database_connection(path, read_only=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "schema_migrations" not in tables:
            return list(EXPECTED_SCHEMA_VERSIONS)
        applied = _validate_migration_history(
            connection, migrations, require_complete=False
        )
    return sorted(set(EXPECTED_SCHEMA_VERSIONS) - set(applied))


def initialize_database_if_new(database_path: Path) -> List[str]:
    """仅为不存在的数据库执行首次迁移，已有数据库只校验当前结构。

    该函数不升级、删除或覆盖任何已存在的数据库；零字节文件和非普通文件
    均视为需要人工处理的异常，避免把挂载或中断问题误判为全新数据库。
    调用方负责在并发启动场景下持有跨进程初始化锁，并在锁内调用本函数。

    Args:
        database_path: 需要初始化或校验的数据库路径。

    Returns:
        全新数据库本次应用的迁移说明列表；已有且结构正确时返回空列表。

    Raises:
        RuntimeError: 路径已存在但不是普通文件、文件为空或数据库结构不符合要求。
        OSError: 数据库路径无法访问或迁移时发生文件系统错误。
    """
    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        return migrate_database(path)
    if not path.is_file():
        raise RuntimeError(f"数据库路径已存在但不是普通文件: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"数据库文件为空，拒绝自动覆盖: {path}")
    assert_current_schema(path)
    return []


def upgrade_database_if_outdated(
    database_path: Path,
    before_migrate: Optional[Callable[[List[int]], None]] = None,
) -> List[str]:
    """把已有数据库补齐到当前版本，并允许调用方在写入前插入前置动作。

    与 initialize_database_if_new 的区别只有一点：确认数据库仅仅是落后于
    当前代码时，本函数会继续补齐缺失迁移，而不是直接拒绝启动。未知版本、
    分叉历史和文件层面的异常仍然一律拒绝，不做任何猜测性修复。

    before_migrate 在任何写入前调用，用于创建可回滚的备份；该回调抛出异常
    时升级立即中止且数据库保持原样，因此备份失败绝不会带来无备份的迁移。

    Args:
        database_path: 需要初始化或升级的数据库路径。
        before_migrate: 可选前置回调，入参为升序的缺失迁移版本列表。

    Returns:
        本次应用的迁移说明列表；已是最新时返回空列表。

    Raises:
        RuntimeError: 路径不是普通文件、文件为空、迁移历史分叉或结构校验失败。
        OSError: 数据库路径无法访问或迁移时发生文件系统错误。
    """
    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        return migrate_database(path)
    if not path.is_file():
        raise RuntimeError(f"数据库路径已存在但不是普通文件: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"数据库文件为空，拒绝自动覆盖: {path}")

    pending = pending_migration_versions(path)
    if not pending:
        assert_current_schema(path)
        return []
    if before_migrate is not None:
        before_migrate(pending)
    return migrate_database(path)


def assert_current_schema(database_path: Path) -> None:
    """拒绝未来或分叉迁移历史，并确认数据库结构满足当前代码要求。"""
    path = Path(database_path).expanduser().resolve()
    migrations = _load_migrations(DEFAULT_MIGRATIONS_DIR)
    with database_connection(path, read_only=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "schema_migrations" not in tables:
            raise RuntimeError("数据库缺少 schema_migrations 表，无法验证迁移历史")
        _validate_migration_history(connection, migrations, require_complete=True)
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
            "is_included",
            "is_deleted",
            "deleted_at",
            "original_path",
            "trash_path",
            "deleted_by_user_id",
            "deleted_by_username",
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

        if "admin_jobs" not in tables:
            raise RuntimeError("数据库缺少 admin_jobs 表")
        job_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(admin_jobs)").fetchall()
        }
        required_job_columns = {
            "id", "job_type", "status", "payload_json", "priority", "progress", "created_by",
            "photo_id", "photo_version", "lease_owner", "lease_expires_at", "attempts",
            "max_attempts", "cancel_requested", "error_code", "error_summary", "created_at",
            "updated_at", "started_at", "finished_at", "config_version", "config_snapshot_json",
            "result_json",
        }
        missing_job_columns = required_job_columns - job_columns
        if missing_job_columns:
            raise RuntimeError(f"admin_jobs 缺少必要字段: {sorted(missing_job_columns)}")
        indexes = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='admin_jobs'"
            ).fetchall()
        }
        required_indexes = {"idx_admin_jobs_queue", "uq_admin_jobs_active_photo_type"}
        if not required_indexes.issubset(indexes):
            raise RuntimeError(f"admin_jobs 缺少必要索引: {sorted(required_indexes - indexes)}")

        required_stage6_tables = {
            "photo_lifecycle_audit",
            "admin_maintenance_jobs",
            "admin_maintenance_job_events",
            "display_artifact_state",
            "photo_lifecycle_operations",
            "photo_purge_operations",
        }
        missing_stage6_tables = required_stage6_tables - tables
        if missing_stage6_tables:
            raise RuntimeError(f"阶段六缺少必要表: {sorted(missing_stage6_tables)}")
        operation_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(photo_lifecycle_operations)"
            ).fetchall()
        }
        required_operation_columns = {
            "operation_id",
            "action",
            "photo_id",
            "expected_version",
            "source_path",
            "destination_path",
            "admin_user_id",
            "admin_username",
            "lease_owner",
            "lease_expires_at",
            "created_at",
            "updated_at",
        }
        if operation_columns != required_operation_columns:
            raise RuntimeError(
                "photo_lifecycle_operations 字段不合法: "
                f"actual={sorted(operation_columns)}"
            )
        operation_indexes = connection.execute(
            "PRAGMA index_list(photo_lifecycle_operations)"
        ).fetchall()
        has_unique_photo_id_index = False
        for index in operation_indexes:
            if not bool(index["unique"]):
                continue
            escaped_index_name = str(index["name"]).replace('"', '""')
            index_columns = [
                row["name"]
                for row in connection.execute(
                    f'PRAGMA index_info("{escaped_index_name}")'
                ).fetchall()
            ]
            if index_columns == ["photo_id"]:
                has_unique_photo_id_index = True
                break
        if not has_unique_photo_id_index:
            raise RuntimeError(
                "photo_lifecycle_operations.photo_id 缺少单列唯一索引"
            )
        purge_operation_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(photo_purge_operations)"
            ).fetchall()
        }
        required_purge_operation_columns = {
            "operation_id",
            "photo_id",
            "expected_version",
            "trash_path",
            "admin_user_id",
            "admin_username",
            "internal",
            "lease_owner",
            "lease_expires_at",
            "created_at",
            "updated_at",
        }
        if purge_operation_columns != required_purge_operation_columns:
            raise RuntimeError(
                "photo_purge_operations 字段不合法: "
                f"actual={sorted(purge_operation_columns)}"
            )
        purge_operation_indexes = connection.execute(
            "PRAGMA index_list(photo_purge_operations)"
        ).fetchall()
        has_unique_purge_photo_id_index = False
        for index in purge_operation_indexes:
            if not bool(index["unique"]):
                continue
            escaped_index_name = str(index["name"]).replace('"', '""')
            index_columns = [
                row["name"]
                for row in connection.execute(
                    f'PRAGMA index_info("{escaped_index_name}")'
                ).fetchall()
            ]
            if index_columns == ["photo_id"]:
                has_unique_purge_photo_id_index = True
                break
        if not has_unique_purge_photo_id_index:
            raise RuntimeError(
                "photo_purge_operations.photo_id 缺少单列唯一索引"
            )
        purge_operation_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(photo_purge_operations)"
        ).fetchall()
        if purge_operation_foreign_keys:
            raise RuntimeError("photo_purge_operations 不应包含外键")
        maintenance_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(admin_maintenance_jobs)").fetchall()
        }
        required_maintenance_columns = {
            "id", "job_type", "status", "payload_json", "result_json", "priority",
            "progress", "created_by_user_id", "created_by_username", "lease_owner",
            "lease_expires_at", "attempts", "max_attempts", "cancel_requested",
            "error_code", "error_summary", "created_at", "updated_at", "started_at",
            "finished_at", "config_version", "config_snapshot_json",
        }
        if not required_maintenance_columns.issubset(maintenance_columns):
            raise RuntimeError(
                "admin_maintenance_jobs 缺少必要字段: "
                f"{sorted(required_maintenance_columns - maintenance_columns)}"
            )
        maintenance_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='admin_maintenance_jobs'"
            ).fetchall()
        }
        required_maintenance_indexes = {
            "idx_admin_maintenance_jobs_queue",
            "uq_admin_maintenance_jobs_active_type",
        }
        if not required_maintenance_indexes.issubset(maintenance_indexes):
            raise RuntimeError(
                "admin_maintenance_jobs 缺少必要索引: "
                f"{sorted(required_maintenance_indexes - maintenance_indexes)}"
            )
        display_state = connection.execute(
            "SELECT blocked,generation,desired_generation,manifest_json,updated_at,maintenance_job_id "
            "FROM display_artifact_state WHERE id=1"
        ).fetchone()
        if display_state is None:
            raise RuntimeError("display_artifact_state 缺少单例状态")
        generation = int(display_state["generation"])
        desired_generation = int(display_state["desired_generation"])
        if generation < 0 or desired_generation < generation:
            raise RuntimeError("display_artifact_state 渲染代次不合法")
        if bool(display_state["blocked"]) != (generation < desired_generation):
            raise RuntimeError("display_artifact_state 屏蔽状态与渲染代次不一致")

        if "admin_job_events" not in tables:
            raise RuntimeError("数据库缺少 admin_job_events 表")
        event_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(admin_job_events)").fetchall()
        }
        required_event_columns = {
            "id", "job_id", "event_type", "old_status", "new_status", "admin_user_id",
            "worker_id", "reason_code", "created_at",
        }
        if not required_event_columns.issubset(event_columns):
            raise RuntimeError(
                f"admin_job_events 缺少必要字段: {sorted(required_event_columns - event_columns)}"
            )

        if "app_settings" not in tables:
            raise RuntimeError("数据库缺少 app_settings 表")
        settings_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(app_settings)").fetchall()
        }
        required_settings_columns = {
            "id", "settings_json", "version", "modified_by_user_id",
            "modified_by_username", "created_at", "updated_at",
        }
        if not required_settings_columns.issubset(settings_columns):
            raise RuntimeError(
                "app_settings 缺少必要字段: "
                f"{sorted(required_settings_columns - settings_columns)}"
            )
        settings_state = connection.execute(
            "SELECT settings_json,version FROM app_settings WHERE id=1"
        ).fetchone()
        if settings_state is None:
            raise RuntimeError("app_settings 缺少单例配置")
        try:
            settings_value = __import__("json").loads(settings_state["settings_json"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("app_settings.settings_json 不是合法 JSON") from error
        if not isinstance(settings_value, dict) or int(settings_state["version"]) < 0:
            raise RuntimeError("app_settings 单例内容不合法")

        if "app_settings_audit" not in tables:
            raise RuntimeError("数据库缺少 app_settings_audit 表")
        settings_audit_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(app_settings_audit)").fetchall()
        }
        required_settings_audit_columns = {
            "id", "batch_id", "old_version", "new_version", "changed_keys_json",
            "old_values_json", "new_values_json", "modified_by_user_id",
            "modified_by_username", "created_at",
        }
        if not required_settings_audit_columns.issubset(settings_audit_columns):
            raise RuntimeError(
                "app_settings_audit 缺少必要字段: "
                f"{sorted(required_settings_audit_columns - settings_audit_columns)}"
            )
        settings_audit_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='app_settings_audit'"
            ).fetchall()
        }
        if "idx_app_settings_audit_created_at" not in settings_audit_indexes:
            raise RuntimeError("app_settings_audit 缺少时间索引")

        # 模型厂商档案：多套接口地址、模型与密钥。断言粒度与 app_settings 一致——
        # 只查表与关键列是否存在，不查内容，因为空表是完全合法的初始状态（未建档时
        # 分析链路回退到注册表里的 API_URL 等兜底键）。
        if "model_providers" not in tables:
            raise RuntimeError("数据库缺少 model_providers 表")
        provider_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(model_providers)").fetchall()
        }
        required_provider_columns = {
            "id", "name", "base_url", "model_name", "api_key", "api_key_hint",
            "timeout_seconds", "max_long_edge", "is_enabled", "version",
            "created_at", "updated_at",
        }
        if not required_provider_columns.issubset(provider_columns):
            raise RuntimeError(
                "model_providers 缺少必要字段: "
                f"{sorted(required_provider_columns - provider_columns)}"
            )

        if "model_provider_audit" not in tables:
            raise RuntimeError("数据库缺少 model_provider_audit 表")
        provider_audit_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(model_provider_audit)"
            ).fetchall()
        }
        required_provider_audit_columns = {
            "id", "provider_id", "provider_name", "action", "old_values_json",
            "new_values_json", "modified_by_user_id", "modified_by_username",
            "created_at",
        }
        if not required_provider_audit_columns.issubset(provider_audit_columns):
            raise RuntimeError(
                "model_provider_audit 缺少必要字段: "
                f"{sorted(required_provider_audit_columns - provider_audit_columns)}"
            )
        provider_audit_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='model_provider_audit'"
            ).fetchall()
        }
        if "idx_model_provider_audit_created_at" not in provider_audit_indexes:
            raise RuntimeError("model_provider_audit 缺少时间索引")

        if "admin_login_failures" not in tables:
            raise RuntimeError("数据库缺少 admin_login_failures 表")
        login_failure_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(admin_login_failures)"
            ).fetchall()
        }
        required_login_failure_columns = {
            "attempt_key", "failed_at_epoch", "attempt_nonce"
        }
        if login_failure_columns != required_login_failure_columns:
            raise RuntimeError(
                "admin_login_failures 字段不合法: "
                f"actual={sorted(login_failure_columns)}"
            )
        primary_key_columns = [
            row["name"]
            for row in sorted(
                connection.execute(
                    "PRAGMA table_info(admin_login_failures)"
                ).fetchall(),
                key=lambda item: int(item["pk"]),
            )
            if int(row["pk"]) > 0
        ]
        if primary_key_columns != [
            "attempt_key", "failed_at_epoch", "attempt_nonce"
        ]:
            raise RuntimeError("admin_login_failures 缺少预期复合主键")
        expiry_index_valid = False
        for index in connection.execute(
            "PRAGMA index_list(admin_login_failures)"
        ).fetchall():
            index_columns = [
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            ]
            if index_columns and index_columns[0] == "failed_at_epoch":
                expiry_index_valid = True
                break
        if not expiry_index_valid:
            raise RuntimeError("admin_login_failures 缺少 failed_at_epoch 首列过期索引")
