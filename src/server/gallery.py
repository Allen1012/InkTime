#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""展示页选片逻辑（轮次制随机）。

目标是同时满足三件互相牵制的事：
- 覆盖性：每张照片都要被看到
- 新鲜感：不要很快重复
- 公平性：新导入的照片不能霸屏

## 算法

给每张照片记展示次数 show_count，**只从「当前最小展示次数」的那批里随机选**：

    min_count = 池子里 show_count 的最小值
    从 show_count == min_count 的照片中随机选一张，选中后 +1
    这批全部 +1 后 min_count 自然上升，新一轮开始

一轮内每张照片恰好出现一次，轮内顺序随机。不需要维护「已展示列表」，
状态全在库里，进程重启不丢。

## 新照片为什么不会霸屏

关键在初始化：新照片入场时 show_count 设为 **当前 min_count 减去一个小提前量**，
而不是 0。

举例：1000 张已各展示 15 次（min_count=15），此时导入 10 张新照片。
若按 0 初始化，这 10 张会连续霸屏 10 次；设为 14（提前量 1）则它们只在
接下来一轮里优先出现，之后就与老照片同轮竞争。

初始化是**懒惰**的：新照片在本表中没有记录，第一次选片时才补上基线值。
因此分析脚本完全不需要感知展示逻辑。

## 为什么独立建表

photo_scores 是分析产物，展示计数是运行时状态，混在一张表里会让分析脚本的
UPSERT 每次都要小心别覆盖掉计数。拆开后「清空展示历史」也只是清一张表。
channel 字段为墨水屏预留（它的选片是「历史上的今天」逻辑，与此完全不同，
两边的计数必须分开，否则会互相打乱轮次）。
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

CHANNEL_WEB = "web"

# 由 server.py 注入
MIN_SCORE = 70.0
# 从未展示过的照片在同一轮内的选中权重倍数。
# 注意不能改用「层级偏移」的方式给新照片提前量（即把 show_count 设得比 min_count 更小）：
# 那样新照片会独占一个更低的层级，必然被连续选完才轮到老照片，也就是霸屏。
# 同层加权才能做到「更早出现但与老照片混排」。设为 1.0 即完全公平。
NEW_PHOTO_WEIGHT = 3.0


def configure(min_score: Optional[float] = None,
              new_photo_weight: Optional[float] = None) -> None:
    """注入配置。不在本模块直接读环境变量，便于脱离 Flask 单独测试。"""
    global MIN_SCORE, NEW_PHOTO_WEIGHT
    if min_score is not None:
        MIN_SCORE = float(min_score)
    if new_photo_weight is not None:
        NEW_PHOTO_WEIGHT = max(1.0, float(new_photo_weight))


def ensure_table(conn: sqlite3.Connection) -> None:
    """建展示统计表。与 photo_scores 分离，不设外键约束。

    不用外键的原因：SQLite 默认不启用外键检查，且分析脚本会清理磁盘上已删除的
    照片记录，那时本表会留下孤儿行。选片时用 JOIN photo_scores，孤儿行自然被忽略，
    不影响正确性。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS display_stats (
            photo_id      INTEGER NOT NULL,
            channel       TEXT    NOT NULL DEFAULT 'web',
            show_count    INTEGER NOT NULL DEFAULT 0,
            last_shown_at TEXT,
            PRIMARY KEY (photo_id, channel)
        )
        """
    )
    # 选片每次都要按 show_count 找最小值，加索引避免全表扫
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_display_stats_channel_count "
        "ON display_stats (channel, show_count)"
    )
    conn.commit()


def _pool_where() -> str:
    """候选池条件：分数达标。

    与墨水屏不同，这里不要求有 EXIF 拍摄时间 —— web 展示不依赖日期，
    没有日期的照片同样可以展示。
    """
    return "COALESCE(p.memory_score, 0) >= ?"


def _sync_new_photos(conn: sqlite3.Connection, channel: str) -> int:
    """给候选池中尚无统计记录的照片补上基线值（懒惰初始化）。

    返回新纳入的照片数。
    """
    cur = conn.cursor()

    # 当前轮次基线
    row = cur.execute(
        f"""
        SELECT MIN(d.show_count)
        FROM display_stats d
        JOIN photo_scores p ON p.id = d.photo_id
        WHERE d.channel = ? AND {_pool_where()}
        """,
        (channel, MIN_SCORE),
    ).fetchone()
    min_count = row[0] if row and row[0] is not None else 0

    # 与当前轮次对齐，不做层级偏移。偏移会让新照片独占更低层级从而连续霸屏，
    # 「更早出现」这件事交给同层内的 NEW_PHOTO_WEIGHT 权重实现。
    baseline = min_count

    cur.execute(
        f"""
        INSERT OR IGNORE INTO display_stats (photo_id, channel, show_count, last_shown_at)
        SELECT p.id, ?, ?, NULL
        FROM photo_scores p
        LEFT JOIN display_stats d ON d.photo_id = p.id AND d.channel = ?
        WHERE d.photo_id IS NULL AND {_pool_where()}
        """,
        (channel, baseline, channel, MIN_SCORE),
    )
    added = cur.rowcount or 0
    if added:
        conn.commit()
    return added


PHOTO_FIELDS = """
    p.id, p.path, p.caption, p.type, p.memory_score, p.beauty_score,
    p.side_caption, p.exif_datetime, p.exif_city, p.exif_gps_lat, p.exif_gps_lon,
    p.width, p.height, p.orientation, p.date_source
"""


def _row_to_photo(row: sqlite3.Row, show_count: int) -> Dict[str, Any]:
    """转成前端需要的结构，字段名与 /api/photos 保持一致以便前端复用渲染逻辑。"""
    path = row["path"] or ""
    date_taken = row["exif_datetime"] or ""
    return {
        "id": row["id"],
        "path": path,
        "title": path.split("/")[-1],
        "description": row["caption"] or "",
        "side_caption": row["side_caption"] or "",
        "category": row["type"] or "",
        "memory_score": row["memory_score"],
        "beauty_score": row["beauty_score"],
        "date_taken": date_taken,
        "date_source": row["date_source"] or "",
        "location": row["exif_city"] or "",
        "gps_lat": row["exif_gps_lat"],
        "gps_lon": row["exif_gps_lon"],
        "width": row["width"],
        "height": row["height"],
        "orientation": row["orientation"] or "",
        "thumbnail_url": f"/api/photo/thumbnail?path={path}",
        "full_url": f"/api/photo/full?path={path}",
        "show_count": show_count,
    }


def pick_next(db_path: Path, channel: str = CHANNEL_WEB,
              exclude_id: Optional[int] = None) -> Dict[str, Any]:
    """选出下一张要展示的照片并记账。

    返回 {"photo": {...}, "stats": {...}}；池子为空时 photo 为 None。
    """
    if not Path(db_path).exists():
        return {"photo": None, "error": f"数据库不存在: {db_path}"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_table(conn)
        added = _sync_new_photos(conn, channel)
        cur = conn.cursor()

        pool_total = cur.execute(
            f"SELECT COUNT(*) FROM photo_scores p WHERE {_pool_where()}",
            (MIN_SCORE,),
        ).fetchone()[0]

        if pool_total == 0:
            return {
                "photo": None,
                "error": f"候选池为空：没有 memory_score >= {MIN_SCORE} 的照片",
                "stats": {"pool_total": 0, "min_score": MIN_SCORE},
            }

        # 本轮基线
        min_count = cur.execute(
            f"""
            SELECT MIN(d.show_count)
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND {_pool_where()}
            """,
            (channel, MIN_SCORE),
        ).fetchone()[0] or 0

        # 本轮尚未展示的候选。排除当前那张，避免池子小时连续重复
        sql = f"""
            SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
        """
        params: List[Any] = [channel, min_count, MIN_SCORE]
        if exclude_id is not None:
            sql += " AND p.id != ?"
            params.append(exclude_id)

        rows = cur.execute(sql, params).fetchall()

        # 本轮只剩当前这一张（或已选完）时，直接进入下一轮
        if not rows:
            params = [channel, min_count + 1, MIN_SCORE]
            sql2 = f"""
                SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
                FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
                WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
            """
            if exclude_id is not None:
                sql2 += " AND p.id != ?"
                params.append(exclude_id)
            rows = cur.execute(sql2, params).fetchall()

        # 仍然没有说明池子里只有被排除的那一张，退化为重复展示它
        if not rows:
            rows = cur.execute(
                f"""
                SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
                FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
                WHERE d.channel = ? AND {_pool_where()}
                """,
                (channel, MIN_SCORE),
            ).fetchall()
            if not rows:
                return {"photo": None, "error": "候选池为空",
                        "stats": {"pool_total": pool_total, "min_score": MIN_SCORE}}

        # 同层内加权随机：从未展示过的照片（last_shown_at 为空）权重更高，
        # 因此会更早出现，但仍与老照片混排，不会连续独占。
        weights = [
            NEW_PHOTO_WEIGHT if r["last_shown_at"] in (None, "") else 1.0
            for r in rows
        ]
        row = random.choices(rows, weights=weights, k=1)[0]
        new_count = (row["show_count"] or 0) + 1

        cur.execute(
            "UPDATE display_stats SET show_count = ?, last_shown_at = ? "
            "WHERE photo_id = ? AND channel = ?",
            (new_count, dt.datetime.now().isoformat(timespec="seconds"),
             row["id"], channel),
        )
        conn.commit()

        remaining = cur.execute(
            f"""
            SELECT COUNT(*)
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
            """,
            (channel, min_count, MIN_SCORE),
        ).fetchone()[0]

        return {
            "photo": _row_to_photo(row, new_count),
            "stats": {
                "pool_total": pool_total,
                "min_score": MIN_SCORE,
                "round": min_count + 1,        # 人类可读的轮次，从 1 开始
                "remaining_in_round": remaining,
                "newly_added": added,
            },
        }
    finally:
        conn.close()


def get_stats(db_path: Path, channel: str = CHANNEL_WEB) -> Dict[str, Any]:
    """展示统计概览，用于排查轮次是否正常推进。"""
    if not Path(db_path).exists():
        return {"error": f"数据库不存在: {db_path}"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_table(conn)
        cur = conn.cursor()
        pool_total = cur.execute(
            f"SELECT COUNT(*) FROM photo_scores p WHERE {_pool_where()}",
            (MIN_SCORE,),
        ).fetchone()[0]
        tracked = cur.execute(
            "SELECT COUNT(*) FROM display_stats WHERE channel = ?", (channel,)
        ).fetchone()[0]
        dist = cur.execute(
            f"""
            SELECT d.show_count AS c, COUNT(*) AS n
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND {_pool_where()}
            GROUP BY d.show_count ORDER BY d.show_count
            """,
            (channel, MIN_SCORE),
        ).fetchall()
        return {
            "channel": channel,
            "min_score": MIN_SCORE,
            "pool_total": pool_total,
            "tracked": tracked,
            "count_distribution": {str(r["c"]): r["n"] for r in dist},
        }
    finally:
        conn.close()


def reset(db_path: Path, channel: str = CHANNEL_WEB) -> int:
    """清空某渠道的展示历史。返回删除行数。"""
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_table(conn)
        cur = conn.execute("DELETE FROM display_stats WHERE channel = ?", (channel,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
