#!/usr/bin/env python3
"""SQLite 预检、备份、迁移与迁移后不变量验证工具。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.database import database_connection  # noqa: E402
from src.database_backup import collect_baseline, create_backup  # noqa: E402
from src.migrations import (  # noqa: E402
    EXPECTED_SCHEMA_VERSIONS,
    SCHEMA_TARGET_VERSION,
    assert_current_schema,
    migrate_database,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _resolve_path(raw: str, base: Path = ROOT_DIR) -> Path:
    """将配置路径解析为绝对路径，相对路径以项目根目录为基准。"""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _mount_point(path: Path) -> Path:
    """返回路径所在文件系统的挂载点。"""
    current = path.resolve()
    if not current.exists():
        current = current.parent
    while current.parent != current and not os.path.ismount(current):
        current = current.parent
    return current


def command_preflight(args: argparse.Namespace) -> int:
    """检查活动路径、权限、挂载点和数据库照片路径异常。"""
    if load_dotenv and args.env_file:
        load_dotenv(args.env_file, override=False)
    database = _resolve_path(os.environ.get("DB_PATH", "./data/photos.db"))
    image_dir = _resolve_path(os.environ.get("IMAGE_DIR", "./data/photos"))
    output_dir = _resolve_path(os.environ.get("BIN_OUTPUT_DIR", "./data/output"))

    checks = {
        "database": {
            "path": str(database),
            "exists": database.is_file(),
            "readable": os.access(database, os.R_OK),
            "writable": os.access(database, os.W_OK),
            "mount_point": str(_mount_point(database)),
        },
        "image_dir": {
            "path": str(image_dir),
            "exists": image_dir.is_dir(),
            "readable": os.access(image_dir, os.R_OK),
            "writable": os.access(image_dir, os.W_OK),
            "mount_point": str(_mount_point(image_dir)),
        },
        "output_dir": {
            "path": str(output_dir),
            "exists": output_dir.is_dir(),
            "readable": os.access(output_dir, os.R_OK),
            "writable": os.access(output_dir, os.W_OK),
            "mount_point": str(_mount_point(output_dir)),
        },
    }
    if not database.is_file() or not image_dir.is_dir() or not output_dir.is_dir():
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 1

    outside_root = 0
    missing_files = 0
    with database_connection(database, read_only=True) as connection:
        for row in connection.execute("SELECT path FROM photo_scores"):
            photo = Path(row["path"]).expanduser().resolve()
            if not photo.is_relative_to(image_dir):
                outside_root += 1
            if not photo.is_file():
                missing_files += 1
    checks["photo_paths"] = {
        "outside_image_dir": outside_root,
        "missing_files": missing_files,
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    required = (database.is_file() and os.access(database, os.R_OK | os.W_OK)
                and image_dir.is_dir() and os.access(image_dir, os.R_OK)
                and output_dir.is_dir() and os.access(output_dir, os.R_OK | os.W_OK))
    return 0 if required else 1


def command_baseline(args: argparse.Namespace) -> int:
    """将数据库基线写入显式指定的 JSON 文件。"""
    baseline = collect_baseline(Path(args.database))
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"baseline": str(output), **baseline}, ensure_ascii=False, indent=2))
    return 0


def command_backup(args: argparse.Namespace) -> int:
    """使用 SQLite backup API 创建一致性备份，并保存源库身份基线。"""
    baseline = create_backup(Path(args.database), Path(args.output_dir))
    print(
        json.dumps(
            {
                "backup": baseline["backup_database"],
                "baseline": baseline["backup_baseline"],
                **baseline,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    """对调用方明确指定的数据库执行版本化迁移。"""
    database = Path(args.database).expanduser().resolve()
    applied = migrate_database(database)
    print(json.dumps({"database": str(database), "migrations": applied}, ensure_ascii=False, indent=2))
    return 0


def command_check_schema(args: argparse.Namespace) -> int:
    """只读校验当前数据库具有完整且身份一致的迁移历史与结构。"""
    database = Path(args.database).expanduser().resolve()
    assert_current_schema(database)
    with database_connection(database, read_only=True) as connection:
        versions = tuple(
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
    schema_current = versions == EXPECTED_SCHEMA_VERSIONS
    print(
        json.dumps(
            {
                "database": str(database),
                "schema_target": SCHEMA_TARGET_VERSION,
                "migration_count": len(versions),
                "max_migration": max(versions, default=0),
                "schema_current": schema_current,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if schema_current else 1


def command_verify(args: argparse.Namespace) -> int:
    """核对当前数据库结构、完整性和照片身份不变量。"""
    database = Path(args.database).expanduser().resolve()
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = collect_baseline(database)
    invariant_keys = (
        "photo_count",
        "min_photo_id",
        "max_photo_id",
        "distinct_photo_ids",
        "distinct_photo_paths",
        "photo_identity_sha256",
    )
    mismatches = {
        key: {"before": baseline.get(key), "after": current.get(key)}
        for key in invariant_keys
        if baseline.get(key) != current.get(key)
    }
    has_date_source = "date_source" in current["photo_scores_columns"]
    required_admin_columns = {
        "id",
        "username",
        "password_hash",
        "is_active",
        "last_login_at",
        "created_at",
        "updated_at",
    }
    schema_error = None
    try:
        assert_current_schema(database)
    except RuntimeError as error:
        schema_error = str(error)
    with database_connection(database, read_only=True) as connection:
        migration_versions = tuple(
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_key_violations = [
            dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        admin_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(admin_users)").fetchall()
        }
        admin_table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'admin_users'"
        ).fetchone()
    admin_sql = " ".join(str(admin_table["sql"]).upper().split()) if admin_table else ""
    admin_structure_exists = (
        required_admin_columns.issubset(admin_columns)
        and "USERNAME TEXT NOT NULL COLLATE NOCASE UNIQUE" in admin_sql
    )
    schema_current = schema_error is None and migration_versions == EXPECTED_SCHEMA_VERSIONS
    result = {
        "database": str(database),
        "integrity_check": current["integrity_check"],
        "quick_check": current["quick_check"],
        "date_source_exists": has_date_source,
        "admin_users_structure_exists": admin_structure_exists,
        "schema_target": SCHEMA_TARGET_VERSION,
        "migration_count": len(migration_versions),
        "max_migration": max(migration_versions, default=0),
        "schema_current": schema_current,
        "schema_error": schema_error,
        "foreign_keys": foreign_keys,
        "foreign_key_violations": foreign_key_violations,
        "busy_timeout": busy_timeout,
        "identity_mismatches": mismatches,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    valid = (
        current["integrity_check"] == "ok"
        and current["quick_check"] == "ok"
        and has_date_source
        and admin_structure_exists
        and schema_current
        and foreign_keys == 1
        and not foreign_key_violations
        and busy_timeout == 5000
        and not mismatches
    )
    return 0 if valid else 1


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="检查路径、权限和异常照片路径")
    preflight.add_argument("--env-file", default=str(ROOT_DIR / ".env"))
    preflight.set_defaults(handler=command_preflight)

    baseline = subparsers.add_parser("baseline", help="记录数据库基线")
    baseline.add_argument("--database", required=True)
    baseline.add_argument("--output", required=True)
    baseline.set_defaults(handler=command_baseline)

    backup = subparsers.add_parser("backup", help="创建一致性备份和基线")
    backup.add_argument("--database", required=True)
    backup.add_argument("--output-dir", required=True)
    backup.set_defaults(handler=command_backup)

    migrate = subparsers.add_parser("migrate", help="执行版本化迁移")
    migrate.add_argument("--database", required=True)
    migrate.set_defaults(handler=command_migrate)

    check_schema = subparsers.add_parser(
        "check-schema", help="只读确认数据库已达到当前代码要求"
    )
    check_schema.add_argument("--database", required=True)
    check_schema.set_defaults(handler=command_check_schema)

    verify = subparsers.add_parser("verify", help="核对迁移不变量")
    verify.add_argument("--database", required=True)
    verify.add_argument("--baseline", required=True)
    verify.set_defaults(handler=command_verify)
    return parser


def main() -> int:
    """解析命令并返回适合 shell 的退出码。"""
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
