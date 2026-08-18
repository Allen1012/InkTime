"""在容器目标进程启动前串行准备 InkTime 数据库。"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from src.migrations import initialize_database_if_new

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger(__name__)


def _absolute_path(value: str) -> Path:
    """把容器配置路径按项目根目录解析为绝对路径。"""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _prepare_database(database_path: Path) -> list[str]:
    """持有数据库同目录排他锁，并在锁内初始化新库或校验已有库。

    锁文件只提供跨进程串行化，不承载状态；每个等待者获得锁后都会重新检查
    数据库，因此 Web 服务和后台工作进程并发启动时也只有首个进程执行迁移。

    Args:
        database_path: 已解析的数据库绝对路径。

    Returns:
        当前进程为全新数据库应用的迁移说明；已有数据库返回空列表。
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f"{database_path.name}.initialize.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+") as lock_file:
        LOGGER.info(
            "Waiting for database initialization lock, database_path=[%s], lock_path=[%s]",
            database_path,
            lock_path,
        )
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            applied = initialize_database_if_new(database_path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    LOGGER.info(
        "Database preparation completed, database_path=[%s], migration_count=[%s]",
        database_path,
        len(applied),
    )
    return applied


def _prepare_runtime_directories(database_path: Path) -> None:
    """创建零配置容器运行所需的持久化目录。

    高级部署仍可通过环境变量改写照片和输出目录；分号分隔的所有照片根目录
    都会在服务启动前创建，挂载缺失或权限不足时立即失败而不是延迟到请求阶段。

    Args:
        database_path: 已解析的数据库绝对路径。
    """
    image_directories = [
        _absolute_path(item.strip())
        for item in os.environ.get("IMAGE_DIR", "./data/photos").split(";")
        if item.strip()
    ]
    output_directory = _absolute_path(
        os.environ.get("BIN_OUTPUT_DIR", "./data/output")
    )
    for directory in (database_path.parent, output_directory, *image_directories):
        directory.mkdir(parents=True, exist_ok=True)


def _execute(command: Sequence[str]) -> None:
    """用目标命令替换当前进程，使容器信号直接交给实际服务。

    Args:
        command: Docker 传入的目标命令及参数。

    Raises:
        OSError: 目标程序不存在或无法执行。
    """
    LOGGER.info("Starting container command, executable=[%s]", command[0])
    os.execvp(command[0], list(command))


def main(arguments: Sequence[str] | None = None) -> None:
    """准备数据库后转交容器目标命令，失败时以非零状态退出。

    Args:
        arguments: 可选目标命令；未提供时使用当前进程命令行参数。

    Raises:
        SystemExit: 目标命令为空或数据库准备、命令执行失败时退出。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    command = list(sys.argv[1:] if arguments is None else arguments)
    if not command:
        raise SystemExit("容器启动命令不能为空")
    database_path = _absolute_path(os.environ.get("DB_PATH", "./data/photos.db"))
    try:
        _prepare_runtime_directories(database_path)
        _prepare_database(database_path)
        _execute(command)
    except SystemExit:
        raise
    except Exception as error:
        LOGGER.exception(
            "Container startup failed, database_path=[%s], error_type=[%s]",
            database_path,
            type(error).__name__,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
