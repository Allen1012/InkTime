"""通过 SQLite backup API 生成一致性备份与照片身份基线。

命令行运维工具与容器入口的自动升级共用本模块，确保两条路径产生的备份
格式、完整性校验和基线口径完全一致，不会出现两份互相分叉的备份实现。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src.database import database_connection

BACKUP_FILE_PREFIX = "photos"


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免大数据库一次性读入内存。

    Args:
        path: 待计算摘要的文件路径。

    Returns:
        小写十六进制 SHA-256 摘要。
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_baseline(database_path: Path) -> Dict[str, Any]:
    """收集可证明迁移前后照片身份不变的数据库基线。

    Args:
        database_path: 目标数据库路径。

    Returns:
        含完整性检查、结构清单与照片身份摘要的基线字典。

    Raises:
        FileNotFoundError: 数据库不存在或不是普通文件。
        RuntimeError: photo_scores 缺少建立身份基线所需字段。
    """
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在或不是普通文件: {path}")

    with database_connection(path, read_only=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        photo_table_exists = "photo_scores" in tables
        columns = []
        count = min_id = max_id = distinct_ids = distinct_paths = 0
        logical_digest = hashlib.sha256()
        if photo_table_exists:
            columns = [
                row["name"]
                for row in connection.execute("PRAGMA table_info(photo_scores)").fetchall()
            ]
            if "id" not in columns or "path" not in columns:
                raise RuntimeError("photo_scores 缺少 id 或 path，不能建立身份基线")
            summary = connection.execute(
                "SELECT COUNT(*) AS count, MIN(id) AS min_id, MAX(id) AS max_id, "
                "COUNT(DISTINCT id) AS distinct_ids, COUNT(DISTINCT path) AS distinct_paths "
                "FROM photo_scores"
            ).fetchone()
            count = summary["count"]
            min_id = summary["min_id"]
            max_id = summary["max_id"]
            distinct_ids = summary["distinct_ids"]
            distinct_paths = summary["distinct_paths"]
            for row in connection.execute("SELECT id, path FROM photo_scores ORDER BY id, path"):
                logical_digest.update(f"{row['id']}\0{row['path']}\n".encode("utf-8"))

    return {
        "database": str(path),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_size": path.stat().st_size,
        "file_sha256": sha256_file(path),
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_check": integrity,
        "quick_check": quick_check,
        "journal_mode": journal_mode,
        "foreign_keys": foreign_keys,
        "busy_timeout": busy_timeout,
        "tables": tables,
        "photo_scores_columns": columns,
        "photo_count": count,
        "min_photo_id": min_id,
        "max_photo_id": max_id,
        "distinct_photo_ids": distinct_ids,
        "distinct_photo_paths": distinct_paths,
        "photo_identity_sha256": logical_digest.hexdigest(),
    }


def create_backup(database_path: Path, output_dir: Path) -> Dict[str, Any]:
    """创建一致性备份与同名基线文件，并校验备份自身完整性。

    备份使用 SQLite backup API 而非文件复制，因此允许源库处于 WAL 模式；
    备份副本随后切换为独立 DELETE 日志模式，保证单文件即可独立恢复。
    备份完整性检查失败时删除残件并抛出异常，绝不返回不可信的回滚点。

    Args:
        database_path: 源数据库路径。
        output_dir: 备份输出目录，不存在时创建。

    Returns:
        源库基线，并附加 backup_database、backup_baseline 与 backup_file_sha256。

    Raises:
        FileNotFoundError: 源数据库不存在或不是普通文件。
        RuntimeError: 备份无法切换日志模式或完整性检查失败。
    """
    source_path = Path(database_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}.db"
    baseline_path = target_dir / f"{BACKUP_FILE_PREFIX}-{timestamp}.baseline.json"

    baseline = collect_baseline(source_path)
    with database_connection(source_path, read_only=True) as source:
        target = sqlite3.connect(str(backup_path))
        try:
            source.backup(target)
            target.commit()
            journal_mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise RuntimeError(
                    f"备份数据库无法切换为独立 DELETE 日志模式: {journal_mode}"
                )
        finally:
            target.close()

    backup_integrity = collect_baseline(backup_path)
    if backup_integrity["integrity_check"] != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("备份数据库完整性检查失败")

    baseline["backup_database"] = str(backup_path)
    baseline["backup_baseline"] = str(baseline_path)
    baseline["backup_file_sha256"] = backup_integrity["file_sha256"]
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return baseline
