"""照片只读查询仓储，集中管理公开接口使用的 SQL。"""

from __future__ import annotations

import sqlite3
from typing import Callable, Sequence, Tuple


PHOTO_FIELDS = """
    id, path, caption, type, memory_score, beauty_score, reason,
    width, height, orientation, used_at, exif_datetime, exif_make,
    exif_model, exif_iso, exif_exposure_time, exif_f_number,
    exif_focal_length, exif_gps_lat, exif_gps_lon, exif_gps_alt,
    side_caption, exif_city
"""

SORT_EXPRESSIONS = {
    "latest": "exif_datetime DESC",
    "oldest": "exif_datetime ASC",
    "memory": "memory_score DESC",
    "beauty": "beauty_score DESC",
}


class PhotoRepository:
    """通过请求级连接执行照片查询，避免路由层直接包含 SQL。"""

    def __init__(self, connection_provider: Callable[[], sqlite3.Connection]) -> None:
        """初始化仓储。

        Args:
            connection_provider: 返回当前 Flask 请求复用连接的函数。
        """
        self._connection_provider = connection_provider

    def list_photos(
        self, page: int, photo_filter: str, sort: str, limit: int
    ) -> Tuple[Sequence[sqlite3.Row], int]:
        """分页查询照片列表，排序表达式仅取服务端白名单。

        Args:
            page: 从 1 开始的页码。
            photo_filter: 分类筛选值，all 表示不过滤。
            sort: 排序白名单键。
            limit: 每页数量。

        Returns:
            当前页行对象和总记录数。
        """
        connection = self._connection_provider()
        where_sql = ""
        values: list[object] = []
        if photo_filter != "all":
            where_sql = " WHERE type LIKE ?"
            values.append(f"%{photo_filter}%")
        order_sql = SORT_EXPRESSIONS[sort]
        offset = (page - 1) * limit
        rows = connection.execute(
            f"SELECT {PHOTO_FIELDS} FROM photo_scores{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores{where_sql}", tuple(values)
        ).fetchone()[0]
        return rows, total

    def search_photos(
        self, query: str, page: int, limit: int
    ) -> Tuple[Sequence[sqlite3.Row], int]:
        """分页搜索照片描述、旁白和路径。

        Args:
            query: 用户输入的搜索词。
            page: 从 1 开始的页码。
            limit: 每页数量。

        Returns:
            当前页行对象和总记录数。
        """
        connection = self._connection_provider()
        term = f"%{query}%"
        offset = (page - 1) * limit
        where_sql = " WHERE caption LIKE ? OR side_caption LIKE ? OR path LIKE ?"
        rows = connection.execute(
            f"SELECT {PHOTO_FIELDS} FROM photo_scores{where_sql} "
            "ORDER BY exif_datetime DESC LIMIT ? OFFSET ?",
            (term, term, term, limit, offset),
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores{where_sql}", (term, term, term)
        ).fetchone()[0]
        return rows, total

    def list_category_counts(self) -> Tuple[Sequence[sqlite3.Row], int]:
        """查询原始分类字符串及数量，标签拆分由 Service 层完成。

        Returns:
            按原始分类聚合的行对象和照片总数。
        """
        connection = self._connection_provider()
        rows = connection.execute(
            "SELECT type, COUNT(*) AS count FROM photo_scores "
            "GROUP BY type ORDER BY count DESC"
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM photo_scores").fetchone()[0]
        return rows, total

    def list_category_photos(
        self, category: str, page: int, limit: int
    ) -> Tuple[Sequence[sqlite3.Row], int]:
        """分页查询指定分类的照片。

        Args:
            category: 分类名称，all 表示全部。
            page: 从 1 开始的页码。
            limit: 每页数量。

        Returns:
            当前页行对象和总记录数。
        """
        connection = self._connection_provider()
        offset = (page - 1) * limit
        if category == "all":
            where_sql = ""
            values: tuple[object, ...] = ()
        else:
            where_sql = " WHERE type LIKE ?"
            values = (f"%{category}%",)
        rows = connection.execute(
            f"SELECT {PHOTO_FIELDS} FROM photo_scores{where_sql} "
            "ORDER BY exif_datetime DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores{where_sql}", values
        ).fetchone()[0]
        return rows, total

    def get_photo(self, photo_id: int) -> sqlite3.Row | None:
        """按稳定自增编号查询照片详情。

        Args:
            photo_id: photo_scores 表的自增编号。

        Returns:
            匹配的照片行，不存在时返回 None。
        """
        return self._connection_provider().execute(
            f"SELECT {PHOTO_FIELDS} FROM photo_scores WHERE id = ?", (photo_id,)
        ).fetchone()

    def list_exif_json(self) -> Sequence[sqlite3.Row]:
        """查询日期清单所需的 EXIF JSON 原始值。

        Returns:
            仅包含 exif_json 字段的行对象序列。
        """
        return self._connection_provider().execute(
            "SELECT exif_json FROM photo_scores"
        ).fetchall()
