"""照片公开查询与后台读取仓储，集中管理参数化 SQL。"""

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
ADMIN_PHOTO_FIELDS = f"""{PHOTO_FIELDS}, date_source, exif_json,
    original_filename, content_sha256, analysis_status, analysis_error,
    is_included, is_deleted, deleted_at, original_path, trash_path,
    deleted_by_user_id, deleted_by_username, created_at, updated_at, version
"""
# 公开侧可见条件同样要求已收录：排除态表达的是「这张照片不进相框」，公开相册、
# 分类浏览与照片详情都属于展示面，未收录照片不该从任何一处露出。后台列表用的是
# ACTIVE_ADMIN_CONDITION，不带这个条件——管理员必须能看到并改动被排除的照片。
VISIBLE_PHOTO_CONDITION = (
    "is_included = 1 AND is_deleted = 0 "
    "AND analysis_status IN ('legacy', 'succeeded')"
)
ACTIVE_ADMIN_CONDITION = "is_deleted = 0"

# 展示统计不在 photo_scores 里，而在 display_stats，主键是 (photo_id, channel)。
# 这里刻意不 import gallery.CHANNEL_WEB 而是复制字面量：app 工厂把 gallery 当可降级
# 模块加载（加载失败只丢展示功能、服务照起），仓储层静态依赖它会把这条降级路径变成
# 启动失败。代价是两处字面量必须一起改，改错的表现是后台把所有照片显示成「未入池」。
# join 必须限定渠道，否则将来新增渠道时一张照片会展开成多行，分页与总数全错。
DISPLAY_STATS_CHANNEL = "web"
DISPLAY_STATS_JOIN = (
    "LEFT JOIN display_stats ON display_stats.photo_id = photo_scores.id "
    "AND display_stats.channel = ?"
)
# 刻意不做 COALESCE：show_count 为 NULL 表示这张照片还没进过候选池，
# 与「进了池但一次没展示」是两回事，序列化层要靠这个区别区分三态。
DISPLAY_STATS_FIELDS = "display_stats.show_count, display_stats.last_shown_at"

SORT_EXPRESSIONS = {
    "latest": "exif_datetime DESC",
    "oldest": "exif_datetime ASC",
    "memory": "memory_score DESC",
    "beauty": "beauty_score DESC",
}
ADMIN_SORT_EXPRESSIONS = {
    "latest": "(exif_datetime IS NULL OR exif_datetime = '') ASC, exif_datetime DESC, id DESC",
    "oldest": "(exif_datetime IS NULL OR exif_datetime = '') ASC, exif_datetime ASC, id ASC",
    "added_newest": "created_at DESC, id DESC",
    "added_oldest": "created_at ASC, id ASC",
    "memory": "memory_score IS NULL ASC, memory_score DESC, id DESC",
    "beauty": "beauty_score IS NULL ASC, beauty_score DESC, id DESC",
    # 展示次数排序只在 list_admin_photos 用，那里 join 了 display_stats。
    # 未入池（NULL）一律排在最后：它们不是「展示得少」，而是压根没资格被选中。
    "shown_most": (
        "display_stats.show_count IS NULL ASC, display_stats.show_count DESC, id DESC"
    ),
    "shown_least": (
        "display_stats.show_count IS NULL ASC, display_stats.show_count ASC, id DESC"
    ),
}


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 通配符，让后台搜索按用户输入的字面量匹配。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        """分页查询可公开展示的照片列表。

        Args:
            page: 从 1 开始的页码。
            photo_filter: 分类筛选值，all 表示不过滤。
            sort: 排序白名单键。
            limit: 每页数量。

        Returns:
            当前页行对象和总记录数。
        """
        conditions = [VISIBLE_PHOTO_CONDITION]
        values: list[object] = []
        if photo_filter != "all":
            conditions.append("type LIKE ?")
            values.append(f"%{photo_filter}%")
        where_sql = f" WHERE {' AND '.join(conditions)}"
        order_sql = SORT_EXPRESSIONS[sort]
        offset = (page - 1) * limit
        connection = self._connection_provider()
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
        """分页搜索可公开照片的描述、旁白和路径。

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
        where_sql = (
            f" WHERE {VISIBLE_PHOTO_CONDITION} "
            "AND (caption LIKE ? OR side_caption LIKE ? OR path LIKE ?)"
        )
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
        """查询可公开照片的原始分类字符串及数量。

        Returns:
            按原始分类聚合的行对象和照片总数。
        """
        connection = self._connection_provider()
        rows = connection.execute(
            "SELECT type, COUNT(*) AS count FROM photo_scores "
            f"WHERE {VISIBLE_PHOTO_CONDITION} GROUP BY type ORDER BY count DESC"
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores WHERE {VISIBLE_PHOTO_CONDITION}"
        ).fetchone()[0]
        return rows, total

    def list_admin_category_counts(self) -> Tuple[Sequence[sqlite3.Row], int]:
        """查询全部活动照片的原始分类字符串及数量，不按分析状态过滤。

        Returns:
            按原始分类聚合的活动照片行对象和活动照片总数。
        """
        connection = self._connection_provider()
        rows = connection.execute(
            "SELECT type, COUNT(*) AS count FROM photo_scores "
            f"WHERE {ACTIVE_ADMIN_CONDITION} GROUP BY type ORDER BY count DESC"
        ).fetchall()
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores WHERE {ACTIVE_ADMIN_CONDITION}"
        ).fetchone()[0]
        return rows, int(total)

    def list_category_photos(
        self, category: str, page: int, limit: int
    ) -> Tuple[Sequence[sqlite3.Row], int]:
        """分页查询指定分类下可公开展示的照片。

        Args:
            category: 分类名称，all 表示全部。
            page: 从 1 开始的页码。
            limit: 每页数量。

        Returns:
            当前页行对象和总记录数。
        """
        conditions = [VISIBLE_PHOTO_CONDITION]
        values: tuple[object, ...] = ()
        if category != "all":
            conditions.append("type LIKE ?")
            values = (f"%{category}%",)
        where_sql = f" WHERE {' AND '.join(conditions)}"
        offset = (page - 1) * limit
        connection = self._connection_provider()
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
        """按稳定自增编号查询可公开照片详情。

        Args:
            photo_id: photo_scores 表的自增编号。

        Returns:
            匹配的照片行，不存在或不可公开时返回 None。
        """
        return self._connection_provider().execute(
            f"SELECT {PHOTO_FIELDS} FROM photo_scores "
            f"WHERE id = ? AND {VISIBLE_PHOTO_CONDITION}",
            (photo_id,),
        ).fetchone()

    def count_admin_photos(self) -> int:
        """返回后台当前活动照片记录总数。"""
        return int(
            self._connection_provider()
            .execute(
                f"SELECT COUNT(*) FROM photo_scores WHERE {ACTIVE_ADMIN_CONDITION}"
            )
            .fetchone()[0]
        )

    def admin_score_summary(self) -> sqlite3.Row:
        """返回活动照片评分统计；空评分不参与平均值。"""
        return self._connection_provider().execute(
            "SELECT AVG(memory_score) AS average_memory, "
            "AVG(beauty_score) AS average_beauty, "
            "SUM(CASE WHEN memory_score IS NULL OR beauty_score IS NULL THEN 1 ELSE 0 END) AS missing_scores "
            f"FROM photo_scores WHERE {ACTIVE_ADMIN_CONDITION}"
        ).fetchone()

    def admin_metadata_summary(self) -> sqlite3.Row:
        """返回活动照片拍摄时间与城市元数据覆盖统计。"""
        return self._connection_provider().execute(
            "SELECT "
            "SUM(CASE WHEN exif_datetime IS NOT NULL AND exif_datetime != '' THEN 1 ELSE 0 END) AS dated_count, "
            "SUM(CASE WHEN exif_city IS NOT NULL AND exif_city != '' THEN 1 ELSE 0 END) AS located_count "
            f"FROM photo_scores WHERE {ACTIVE_ADMIN_CONDITION}"
        ).fetchone()

    def count_trashed_photos(self) -> int:
        """返回回收站中等待恢复或永久删除的照片数。"""
        return int(
            self._connection_provider()
            .execute("SELECT COUNT(*) FROM photo_scores WHERE is_deleted = 1")
            .fetchone()[0]
        )

    def admin_analysis_status_summary(self) -> sqlite3.Row:
        """返回活动照片按分析状态的分布。

        逐状态单独计数而不是 GROUP BY：首页要固定展示这几项，用一行结果
        免去调用方为缺失状态补零。
        """
        return self._connection_provider().execute(
            "SELECT "
            "SUM(CASE WHEN analysis_status = 'pending' THEN 1 ELSE 0 END) AS pending_count, "
            "SUM(CASE WHEN analysis_status = 'running' THEN 1 ELSE 0 END) AS running_count, "
            "SUM(CASE WHEN analysis_status = 'failed' THEN 1 ELSE 0 END) AS failed_count, "
            "SUM(CASE WHEN analysis_status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_count, "
            "SUM(CASE WHEN analysis_status = 'legacy' THEN 1 ELSE 0 END) AS legacy_count "
            f"FROM photo_scores WHERE {ACTIVE_ADMIN_CONDITION}"
        ).fetchone()

    def count_photos_created_since(self, since: str) -> int:
        """返回入库时间不早于给定时刻的活动照片数。

        `created_at` 存的是 UTC ISO 8601 字符串，同格式下可直接按字典序比较。
        历史迁移补列时留空的记录不参与统计——它们本就不是近期新增。

        Args:
            since: UTC ISO 8601 起点，例如 2026-08-06T00:00:00+00:00。
        """
        return int(
            self._connection_provider()
            .execute(
                "SELECT COUNT(*) FROM photo_scores "
                f"WHERE {ACTIVE_ADMIN_CONDITION} "
                "AND created_at IS NOT NULL AND created_at >= ?",
                (since,),
            )
            .fetchone()[0]
        )

    def count_photos_missing_date(self) -> int:
        """返回缺拍摄时间的活动照片数。

        这些照片照常展示，只是画面上不显示日期、也无法参与「历史上的今天」的
        月日匹配。首页据此提示补录。
        """
        return int(
            self._connection_provider()
            .execute(
                "SELECT COUNT(*) FROM photo_scores "
                f"WHERE {ACTIVE_ADMIN_CONDITION} "
                "AND (exif_datetime IS NULL OR TRIM(exif_datetime) = '')"
            )
            .fetchone()[0]
        )

    def admin_job_status_summary(self) -> sqlite3.Row:
        """返回两条任务队列合并后的待处理、执行中与失败数量。

        首页只关心「有没有事要我处理」，因此把照片分析与维护两条队列合并成
        一组数字；要看具体是哪条队列的哪个任务，点进任务页即可。
        """
        return self._connection_provider().execute(
            "SELECT "
            "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count, "
            "SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count "
            "FROM (SELECT status FROM admin_jobs "
            "      UNION ALL SELECT status FROM admin_maintenance_jobs)"
        ).fetchone()

    def list_admin_photos(
        self,
        page: int,
        limit: int,
        query: str,
        category: str,
        analysis_status: str,
        date_from: str | None,
        date_to: str | None,
        sort: str,
        missing_date: bool = False,
        curation: str = "",
    ) -> Tuple[Sequence[sqlite3.Row], int]:
        """按后台筛选条件分页查询活动照片。

        Args:
            page: 从 1 开始的页码。
            limit: 每页数量。
            query: 文件路径、描述、旁白或城市搜索词。
            category: 分类筛选值，空字符串表示全部。
            analysis_status: 分析状态精确筛选值，空字符串表示全部。
            date_from: 规范化后的拍摄时间起点。
            date_to: 规范化后的拍摄时间终点。
            sort: 后台排序白名单键。
            missing_date: 只看缺拍摄时间的照片，用于引导补录。
            curation: included 只看已收录，excluded 只看未收录，空字符串表示全部。

        Returns:
            当前页行对象和同条件总记录数。
        """
        where_sql, values = self._admin_filter_clause(
            query, category, analysis_status, date_from, date_to, missing_date, curation
        )
        order_sql = ADMIN_SORT_EXPRESSIONS[sort]
        offset = (page - 1) * limit
        connection = self._connection_provider()
        rows = connection.execute(
            f"SELECT {ADMIN_PHOTO_FIELDS}, {DISPLAY_STATS_FIELDS} "
            f"FROM photo_scores {DISPLAY_STATS_JOIN}{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            (DISPLAY_STATS_CHANNEL, *values, limit, offset),
        ).fetchall()
        # 总数不 join：渠道限定后 join 不会改变行数，但省一次多表扫描
        total = connection.execute(
            f"SELECT COUNT(*) FROM photo_scores{where_sql}", tuple(values)
        ).fetchone()[0]
        return rows, int(total)

    @staticmethod
    def _admin_filter_clause(
        query: str,
        category: str,
        analysis_status: str,
        date_from: str | None,
        date_to: str | None,
        missing_date: bool,
        curation: str,
    ) -> Tuple[str, list[object]]:
        """构造后台列表的 WHERE 子句与参数。

        分页列表与相邻照片查询必须共用同一份条件：详情页「上一张/下一张」的含义
        就是「在你刚才那个列表里的前后一张」，两处筛选一旦有出入，翻页就会跳到
        当前筛选之外的照片上。

        Args:
            query: 路径、描述、旁白或城市搜索词。
            category: 分类筛选值，空字符串表示全部。
            analysis_status: 分析状态精确值，空字符串表示全部。
            date_from: 规范化后的拍摄时间起点。
            date_to: 规范化后的拍摄时间终点。
            missing_date: 只看缺拍摄时间的照片。
            curation: included、excluded 或空字符串。

        Returns:
            以 WHERE 开头的子句与按序排列的绑定参数。
        """
        conditions: list[str] = [ACTIVE_ADMIN_CONDITION]
        values: list[object] = []
        if analysis_status:
            conditions.append("analysis_status = ?")
            values.append(analysis_status)
        if query:
            term = f"%{_escape_like(query)}%"
            conditions.append(
                "(path LIKE ? ESCAPE '\\' OR caption LIKE ? ESCAPE '\\' "
                "OR side_caption LIKE ? ESCAPE '\\' OR exif_city LIKE ? ESCAPE '\\')"
            )
            values.extend((term, term, term, term))
        if category:
            conditions.append("type LIKE ? ESCAPE '\\'")
            values.append(f"%{_escape_like(category)}%")
        if date_from:
            conditions.append("exif_datetime >= ?")
            values.append(date_from)
        if date_to:
            conditions.append("exif_datetime <= ?")
            values.append(date_to)
        if missing_date:
            # 缺拍摄时间的照片照常展示，只是画面上不显示日期、也无法参与
            # 「历史上的今天」的月日匹配；这个筛选用于引导补录。
            conditions.append("(exif_datetime IS NULL OR TRIM(exif_datetime) = '')")
        if curation == "included":
            conditions.append("is_included = 1")
        elif curation == "excluded":
            conditions.append("is_included = 0")
        return f" WHERE {' AND '.join(conditions)}", values

    def find_adjacent_admin_photos(
        self,
        photo_id: int,
        query: str,
        category: str,
        analysis_status: str,
        date_from: str | None,
        date_to: str | None,
        sort: str,
        missing_date: bool = False,
        curation: str = "",
    ) -> Tuple[int | None, int | None]:
        """在同一筛选与排序下返回目标照片的前一张与后一张编号。

        用窗口函数一次算出前后邻居，而不是把整个结果集取回内存再找位置：筛选命中
        上千张时后者会把一整页列表的查询成本放大到全表。也不逐条比较排序键——
        后台排序表达式含多列与 NULL 优先级，手写等价比较极易与列表页产生偏差。

        Args:
            photo_id: 当前照片编号。
            query: 与列表页一致的搜索词。
            category: 分类筛选值。
            analysis_status: 分析状态精确值。
            date_from: 规范化后的拍摄时间起点。
            date_to: 规范化后的拍摄时间终点。
            sort: 后台排序白名单键。
            missing_date: 只看缺拍摄时间的照片。
            curation: 收录状态筛选值。

        Returns:
            前一张与后一张的照片编号；处于两端或不在结果集内时对应项为 None。
        """
        where_sql, values = self._admin_filter_clause(
            query, category, analysis_status, date_from, date_to, missing_date, curation
        )
        order_sql = ADMIN_SORT_EXPRESSIONS[sort]
        # JOIN 必须保留：shown_most / shown_least 的排序表达式引用 display_stats
        row = self._connection_provider().execute(
            "WITH ordered AS ("
            "  SELECT photo_scores.id AS id,"
            f"         LAG(photo_scores.id) OVER (ORDER BY {order_sql}) AS previous_id,"
            f"         LEAD(photo_scores.id) OVER (ORDER BY {order_sql}) AS next_id"
            f"  FROM photo_scores {DISPLAY_STATS_JOIN}{where_sql}"
            ") SELECT previous_id, next_id FROM ordered WHERE id = ?",
            (DISPLAY_STATS_CHANNEL, *values, photo_id),
        ).fetchone()
        if row is None:
            return None, None
        previous_id = row["previous_id"]
        next_id = row["next_id"]
        return (
            int(previous_id) if previous_id is not None else None,
            int(next_id) if next_id is not None else None,
        )

    def get_admin_photo(self, photo_id: int) -> sqlite3.Row | None:
        """返回后台活动照片详情与生命周期字段。

        同样 join 展示统计：列表与详情共用 `AdminPhotoService._list_item` 序列化，
        两边的列集必须一致，少一列会让详情页取字段时直接抛错。

        Args:
            photo_id: photo_scores 表的自增编号。

        Returns:
            匹配的活动照片行，不存在时返回 None。
        """
        return self._connection_provider().execute(
            f"SELECT {ADMIN_PHOTO_FIELDS}, {DISPLAY_STATS_FIELDS} "
            f"FROM photo_scores {DISPLAY_STATS_JOIN} "
            f"WHERE id = ? AND {ACTIVE_ADMIN_CONDITION}",
            (DISPLAY_STATS_CHANNEL, photo_id),
        ).fetchone()

    def list_photo_dates(self) -> Sequence[sqlite3.Row]:
        """查询公开月日清单使用的规范拍摄时间。

        Returns:
            仅包含 exif_datetime 字段的可公开照片行。
        """
        return self._connection_provider().execute(
            "SELECT exif_datetime FROM photo_scores "
            f"WHERE {VISIBLE_PHOTO_CONDITION}"
        ).fetchall()

    def is_visible_path(self, candidates: Sequence[str]) -> bool:
        """判断任一规范路径是否对应当前可公开照片。

        Args:
            candidates: 请求原值、绝对路径和相对路径等规范候选。

        Returns:
            存在活动且分析可展示的匹配记录时返回 True。
        """
        values = tuple(dict.fromkeys(str(value) for value in candidates if str(value)))
        if not values:
            return False
        placeholders = ",".join("?" for _ in values)
        row = self._connection_provider().execute(
            f"SELECT 1 FROM photo_scores WHERE path IN ({placeholders}) "
            f"AND {VISIBLE_PHOTO_CONDITION} LIMIT 1",
            values,
        ).fetchone()
        return row is not None
