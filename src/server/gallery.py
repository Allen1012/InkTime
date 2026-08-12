#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""展示页轮次制随机选片逻辑。"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.database import connect_database

CHANNEL_WEB = "web"
MIN_SCORE = 70.0
NEW_PHOTO_WEIGHT = 3.0


def configure(
    min_score: Optional[float] = None,
    new_photo_weight: Optional[float] = None,
) -> None:
    """更新旧直接调用使用的默认值；Web 请求会显式传入独立参数。"""
    global MIN_SCORE, NEW_PHOTO_WEIGHT
    if min_score is not None:
        MIN_SCORE = float(min_score)
    if new_photo_weight is not None:
        NEW_PHOTO_WEIGHT = max(1.0, float(new_photo_weight))


def _effective_parameters(
    min_score: Optional[float], new_photo_weight: Optional[float]
) -> tuple[float, float]:
    """解析单次调用参数，缺省时仅为旧直接调用读取兼容默认值。"""
    return (
        MIN_SCORE if min_score is None else float(min_score),
        NEW_PHOTO_WEIGHT
        if new_photo_weight is None
        else max(1.0, float(new_photo_weight)),
    )


def ensure_table(conn: sqlite3.Connection) -> None:
    """创建与照片分析结果分离的展示统计表及选片索引。"""
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_display_stats_channel_count "
        "ON display_stats (channel, show_count)"
    )


def _pool_where() -> str:
    """返回分数达标、未删除且分析可用的候选池条件。"""
    return (
        "COALESCE(p.memory_score, 0) >= ? "
        "AND p.is_deleted = 0 "
        "AND p.analysis_status IN ('legacy', 'succeeded')"
    )


def _sync_new_photos(
    conn: sqlite3.Connection,
    channel: str,
    min_score: float,
    new_photo_weight: float,
) -> int:
    """按本次阈值给新候选补齐轮次基线，并接收同链路展示权重。"""
    del new_photo_weight  # 权重只在同层随机选择时使用，此处显式接收以固定调用链。
    cursor = conn.cursor()
    row = cursor.execute(
        f"""
        SELECT MIN(d.show_count)
        FROM display_stats d
        JOIN photo_scores p ON p.id = d.photo_id
        WHERE d.channel = ? AND {_pool_where()}
        """,
        (channel, min_score),
    ).fetchone()
    baseline = row[0] if row and row[0] is not None else 0
    cursor.execute(
        f"""
        INSERT OR IGNORE INTO display_stats (photo_id, channel, show_count, last_shown_at)
        SELECT p.id, ?, ?, NULL
        FROM photo_scores p
        LEFT JOIN display_stats d ON d.photo_id = p.id AND d.channel = ?
        WHERE d.photo_id IS NULL AND {_pool_where()}
        """,
        (channel, baseline, channel, min_score),
    )
    return cursor.rowcount or 0


PHOTO_FIELDS = """
    p.id, p.path, p.caption, p.type, p.memory_score, p.beauty_score,
    p.side_caption, p.exif_datetime, p.exif_city, p.exif_gps_lat, p.exif_gps_lon,
    p.width, p.height, p.orientation, p.date_source
"""


def _row_to_photo(row: sqlite3.Row, show_count: int) -> Dict[str, Any]:
    """把数据库行转换为展示前端兼容的照片结构。"""
    path = row["path"] or ""
    return {
        "id": row["id"],
        "path": path,
        "title": path.split("/")[-1],
        "description": row["caption"] or "",
        "side_caption": row["side_caption"] or "",
        "category": row["type"] or "",
        "memory_score": row["memory_score"],
        "beauty_score": row["beauty_score"],
        "date_taken": row["exif_datetime"] or "",
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


def pick_next(
    db_path: Path,
    channel: str = CHANNEL_WEB,
    exclude_id: Optional[int] = None,
    min_score: Optional[float] = None,
    new_photo_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """按本次显式阈值和权重选择下一张照片并在同一事务记账。"""
    effective_min_score, effective_weight = _effective_parameters(
        min_score, new_photo_weight
    )
    if not Path(db_path).exists():
        return {"photo": None, "error": f"数据库不存在: {db_path}"}
    connection = connect_database(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_table(connection)
        added = _sync_new_photos(
            connection, channel, effective_min_score, effective_weight
        )
        cursor = connection.cursor()
        pool_total = cursor.execute(
            f"SELECT COUNT(*) FROM photo_scores p WHERE {_pool_where()}",
            (effective_min_score,),
        ).fetchone()[0]
        if pool_total == 0:
            return {
                "photo": None,
                "error": f"候选池为空：没有 memory_score >= {effective_min_score} 的照片",
                "stats": {"pool_total": 0, "min_score": effective_min_score},
            }
        min_count = cursor.execute(
            f"""
            SELECT MIN(d.show_count)
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND {_pool_where()}
            """,
            (channel, effective_min_score),
        ).fetchone()[0] or 0
        sql = f"""
            SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
        """
        parameters: List[Any] = [channel, min_count, effective_min_score]
        if exclude_id is not None:
            sql += " AND p.id != ?"
            parameters.append(exclude_id)
        rows = cursor.execute(sql, parameters).fetchall()
        if not rows:
            parameters = [channel, min_count + 1, effective_min_score]
            sql = f"""
                SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
                FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
                WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
            """
            if exclude_id is not None:
                sql += " AND p.id != ?"
                parameters.append(exclude_id)
            rows = cursor.execute(sql, parameters).fetchall()
        if not rows:
            rows = cursor.execute(
                f"""
                SELECT {PHOTO_FIELDS}, d.show_count, d.last_shown_at
                FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
                WHERE d.channel = ? AND {_pool_where()}
                """,
                (channel, effective_min_score),
            ).fetchall()
            if not rows:
                return {
                    "photo": None,
                    "error": "候选池为空",
                    "stats": {
                        "pool_total": pool_total,
                        "min_score": effective_min_score,
                    },
                }
        weights = [
            effective_weight if row["last_shown_at"] in (None, "") else 1.0
            for row in rows
        ]
        row = random.choices(rows, weights=weights, k=1)[0]
        new_count = (row["show_count"] or 0) + 1
        cursor.execute(
            "UPDATE display_stats SET show_count = ?, last_shown_at = ? "
            "WHERE photo_id = ? AND channel = ?",
            (
                new_count,
                dt.datetime.now().isoformat(timespec="seconds"),
                row["id"],
                channel,
            ),
        )
        connection.commit()
        remaining = cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND d.show_count = ? AND {_pool_where()}
            """,
            (channel, min_count, effective_min_score),
        ).fetchone()[0]
        return {
            "photo": _row_to_photo(row, new_count),
            "stats": {
                "pool_total": pool_total,
                "min_score": effective_min_score,
                "new_photo_weight": effective_weight,
                "round": min_count + 1,
                "remaining_in_round": remaining,
                "newly_added": added,
            },
        }
    finally:
        connection.close()


def peek_photo(
    db_path: Path,
    channel: str = CHANNEL_WEB,
    photo_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """只读取出一张照片用于休息期展示，**不修改任何展示计数**。

    传入编号时取该照片，不要求达到回忆度阈值（休息期固定照片由管理员显式指定），
    但仍要求未删除且分析可用。不传编号时按 `display_stats.last_shown_at` 倒序取最近
    展示过的一张，用于「停在生效时间段最后一张」。

    Args:
        db_path: 数据库路径。
        channel: 展示频道。
        photo_id: 可选照片编号。

    Returns:
        与 `pick_next` 相同结构的照片字典；找不到可用照片时返回 None。
    """
    if not Path(db_path).exists():
        return None
    connection = connect_database(db_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_table(connection)
        visible = "p.is_deleted = 0 AND p.analysis_status IN ('legacy', 'succeeded')"
        if photo_id is not None:
            row = connection.execute(
                f"""
                SELECT {PHOTO_FIELDS}, COALESCE(d.show_count, 0) AS show_count
                FROM photo_scores p
                LEFT JOIN display_stats d ON d.photo_id = p.id AND d.channel = ?
                WHERE p.id = ? AND {visible}
                """,
                (channel, int(photo_id)),
            ).fetchone()
        else:
            row = connection.execute(
                f"""
                SELECT {PHOTO_FIELDS}, d.show_count AS show_count
                FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
                WHERE d.channel = ? AND d.last_shown_at IS NOT NULL
                  AND d.last_shown_at != '' AND {visible}
                ORDER BY d.last_shown_at DESC, d.photo_id DESC
                LIMIT 1
                """,
                (channel,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_photo(row, row["show_count"] or 0)
    finally:
        connection.close()


def get_stats(
    db_path: Path,
    channel: str = CHANNEL_WEB,
    min_score: Optional[float] = None,
    new_photo_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """按本次显式阈值和权重返回展示统计，不依赖请求间可变全局。"""
    effective_min_score, effective_weight = _effective_parameters(
        min_score, new_photo_weight
    )
    if not Path(db_path).exists():
        return {"error": f"数据库不存在: {db_path}"}
    connection = connect_database(db_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_table(connection)
        connection.commit()
        cursor = connection.cursor()
        pool_total = cursor.execute(
            f"SELECT COUNT(*) FROM photo_scores p WHERE {_pool_where()}",
            (effective_min_score,),
        ).fetchone()[0]
        tracked = cursor.execute(
            "SELECT COUNT(*) FROM display_stats WHERE channel = ?", (channel,)
        ).fetchone()[0]
        distribution = cursor.execute(
            f"""
            SELECT d.show_count AS c, COUNT(*) AS n
            FROM display_stats d JOIN photo_scores p ON p.id = d.photo_id
            WHERE d.channel = ? AND {_pool_where()}
            GROUP BY d.show_count ORDER BY d.show_count
            """,
            (channel, effective_min_score),
        ).fetchall()
        return {
            "channel": channel,
            "min_score": effective_min_score,
            "new_photo_weight": effective_weight,
            "pool_total": pool_total,
            "tracked": tracked,
            "count_distribution": {
                str(row["c"]): row["n"] for row in distribution
            },
        }
    finally:
        connection.close()


def reset(db_path: Path, channel: str = CHANNEL_WEB) -> int:
    """清空指定渠道展示历史，并返回删除行数。"""
    connection = connect_database(db_path)
    try:
        ensure_table(connection)
        cursor = connection.execute(
            "DELETE FROM display_stats WHERE channel = ?", (channel,)
        )
        connection.commit()
        return cursor.rowcount or 0
    finally:
        connection.close()
