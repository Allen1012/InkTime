"""InkTime 的统一 SQLite 连接与短事务工具。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union
from urllib.parse import quote

DatabasePath = Union[str, Path]
SQLITE_TIMEOUT_SECONDS = 5.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5000


def connect_database(database_path: DatabasePath, *, read_only: bool = False) -> sqlite3.Connection:
    """创建配置一致的 SQLite 连接。

    每次调用都返回独立连接，禁止跨线程共享。可写连接启用 WAL；只读连接使用
    SQLite URI 的只读模式，避免健康检查或渲染流程意外修改数据库。

    Args:
        database_path: SQLite 数据库文件路径。
        read_only: 是否以只读模式打开。

    Returns:
        已启用行对象、外键检查和 5 秒忙等待的 SQLite 连接。
    """
    path = Path(database_path).expanduser().resolve()
    if read_only:
        uri = f"file:{quote(str(path))}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=SQLITE_TIMEOUT_SECONDS,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SECONDS)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def database_connection(
    database_path: DatabasePath, *, read_only: bool = False
) -> Iterator[sqlite3.Connection]:
    """在上下文结束时始终关闭 SQLite 连接。

    Args:
        database_path: SQLite 数据库文件路径。
        read_only: 是否禁止数据库写入。

    Yields:
        统一配置的 SQLite 连接。
    """
    connection = connect_database(database_path, read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def write_transaction(database_path: DatabasePath) -> Iterator[sqlite3.Connection]:
    """执行一个使用 ``BEGIN IMMEDIATE`` 的短写事务。

    网络请求、照片扫描和图片处理不得放在该上下文中，以免长时间占用写锁。

    Args:
        database_path: SQLite 数据库文件路径。

    Yields:
        已开始事务的 SQLite 连接；正常退出提交，异常退出回滚并关闭。
    """
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
