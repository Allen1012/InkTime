"""InkTime 照片目录扫描入口，供定时任务或命令行手动调用。

只负责发现并登记新照片、创建分析任务，真正的分析由独立工作进程承接。
不要用 analyze_photos_docker 做定时分析：它把 pending 状态的照片也视为待
处理对象，会与工作进程重复分析同一张照片、重复消耗模型额度。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.configuration import ConfigurationService, parse_image_dirs
from src.database import database_connection
from src.migrations import assert_current_schema
from src.server.admin_jobs import LibraryScanService

ROOT_DIR = Path(__file__).resolve().parents[2]


def _absolute_path(value: str) -> Path:
    """按项目根目录解析统一配置中的文件系统路径。"""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT_DIR / path).resolve()


def _resolve_operator(database_path: Path) -> int:
    """取编号最小的管理员作为定时扫描的操作者。

    Args:
        database_path: 数据库文件路径。

    Returns:
        用于任务归属记录的管理员编号。

    Raises:
        SystemExit: 尚未创建任何管理员账号。
    """
    with database_connection(database_path, read_only=True) as connection:
        row = connection.execute("SELECT id FROM admin_users ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise SystemExit(
            "尚未创建管理员账号，无法记录任务归属；"
            "请先执行 flask --app src.server.app create-admin"
        )
    return int(row["id"])


def main() -> None:
    """扫描照片目录并登记新照片，输出本次统计供日志留痕。"""
    load_dotenv(ROOT_DIR / ".env", override=False)
    database_path = _absolute_path(os.environ.get("DB_PATH", "./data/photos.db"))
    assert_current_schema(database_path)

    configuration = ConfigurationService(database_path, environment=os.environ)
    settings = configuration.get_many(("IMAGE_DIR", "JOB_MAX_ATTEMPTS"))
    raw_image_dirs = str(settings["IMAGE_DIR"])
    try:
        image_dirs = parse_image_dirs(
            raw_image_dirs, base_dir=ROOT_DIR, require_existing=True
        )
    except ValueError as error:
        raise SystemExit(f"照片目录配置无效: {error}") from error
    print("扫描目录：" + "、".join(str(item) for item in image_dirs))

    service = LibraryScanService(
        raw_image_dirs,
        database_path,
        int(settings["JOB_MAX_ATTEMPTS"]),
        configuration_service=configuration,
    )
    try:
        result = service.scan(_resolve_operator(database_path))
    except sqlite3.Error as error:
        raise SystemExit(f"扫描失败: {error}") from error

    print(
        "扫描完成："
        f"发现 {result['discovered']} 张，"
        f"新登记 {result['registered']} 张，"
        f"已在库 {result['already_indexed']} 张，"
        f"剩余待登记 {result['remaining']} 张"
    )
    if result["remaining"]:
        print(
            f"提示：单次上限 {result['batch_limit']} 张，"
            "剩余照片会在下一次扫描时继续登记"
        )
    sys.stdout.flush()


if __name__ == "__main__":
    main()
