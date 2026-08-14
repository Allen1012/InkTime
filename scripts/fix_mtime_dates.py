#!/usr/bin/env python3
"""修正把文件修改时间当拍摄时间的历史照片。

背景：取日期的兜底链原本是 EXIF → XMP → 文件名 → 文件 mtime，其中文件名一级读的
是磁盘路径。上传照片的磁盘名是随机十六进制串，永远解析不出日期，于是直接掉到
mtime——而上传落盘会把 mtime 刷成上传时刻，结果雪景照的拍摄时间显示成八月。

原始文件名一直存在 `original_filename` 里没被用上。本脚本按新口径重算
`date_source='mtime'` 的照片：

- 原始名或磁盘名里能解析出日期 → 写入该日期，来源记为 filename
- 解析不出 → 拍摄时间留空，来源记为 none

留空不影响展示：这些照片照常进候选池、照常轮播，只是画面上不显示拍摄时间，也不
参与「历史上的今天」的月日匹配（没有日期无从匹配），而是通过补足档进入当天画面。
想补日期可在后台照片详情页手工填写，来源会记为 manual。

默认只打印将要做的改动，加 --apply 才真正写库。写库前会自动备份。

用法：
    python scripts/fix_mtime_dates.py --database data/photos.db            # 预览
    python scripts/fix_mtime_dates.py --database data/photos.db --apply    # 执行
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analyze_photos_docker import datetime_from_filename  # noqa: E402


def resolve_from_names(
    path_value: str, original_filename: str | None, original_path: str | None = None
) -> str | None:
    """按新口径从原始名与磁盘名解析日期，原始名优先。

    回收站照片的 `path` 已指向 .trash 下的位置，磁盘名回退要用删除前的
    `original_path`，否则拿到的是回收站里的名字。
    """
    disk_source = original_path or path_value or ""
    candidates = [original_filename, Path(disk_source).name]
    for candidate in candidates:
        if not candidate:
            continue
        parsed = datetime_from_filename(Path(candidate))
        if parsed:
            return parsed
    return None


def build_plan(connection: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """把 mtime 照片分成「可修正」与「需留空」两组。

    回收站照片一并处理：日期是照片自身的属性，不该因为它在回收站里就留着错值，
    否则恢复之后错误日期跟着回来。
    """
    rows = connection.execute(
        "SELECT id,path,original_filename,original_path,exif_datetime,is_deleted "
        "FROM photo_scores WHERE date_source='mtime' ORDER BY id"
    ).fetchall()
    fixable: list[dict] = []
    clearing: list[dict] = []
    for row in rows:
        resolved = resolve_from_names(
            row["path"], row["original_filename"], row["original_path"]
        )
        display_name = (
            row["original_filename"]
            or Path(row["original_path"] or row["path"] or "").name
        )
        item = {
            "id": int(row["id"]),
            "name": display_name,
            "current": row["exif_datetime"],
            "resolved": resolved,
            "trashed": bool(row["is_deleted"]),
        }
        (fixable if resolved else clearing).append(item)
    return fixable, clearing


def print_plan(fixable: list[dict], clearing: list[dict]) -> None:
    """打印将要执行的改动，供人工确认。"""
    total = len(fixable) + len(clearing)
    trashed = sum(1 for item in fixable + clearing if item["trashed"])
    print(f"待处理照片（date_source='mtime'）共 {total} 张，其中回收站内 {trashed} 张\n")
    print(f"【可修正】{len(fixable)} 张 —— 从文件名解析出真实拍摄时间")
    for item in fixable:
        mark = "（回收站）" if item["trashed"] else ""
        print(f"  #{item['id']:<5} {item['name']}{mark}")
        print(f"         {item['current']}  ->  {item['resolved']}")
    print(f"\n【需留空】{len(clearing)} 张 —— 无任何日期线索，拍摄时间将清空")
    print("         这些照片照常展示，只是画面上不显示拍摄时间；")
    print("         想补日期可在后台照片详情页填写，来源会记为 manual")
    for item in clearing:
        mark = "（回收站）" if item["trashed"] else ""
        print(f"  #{item['id']:<5} {item['name']}{mark}  （当前 {item['current']}）")


def backup_database(database: Path) -> Path:
    """写库前做一份带时间戳的副本。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = database.with_name(f"{database.stem}-before-datefix-{stamp}{database.suffix}")
    shutil.copy2(database, target)
    return target


def apply_plan(
    connection: sqlite3.Connection, fixable: list[dict], clearing: list[dict]
) -> None:
    """在单个事务里写入修正结果，同时同步 exif_json 里的 datetime。

    render 与展示页从 `exif_json` 的 datetime 字段取日期，只改列不改 JSON 会让
    两处不一致。
    """
    with connection:
        for item in fixable:
            connection.execute(
                "UPDATE photo_scores SET exif_datetime=?,date_source='filename',"
                "exif_json=json_set(COALESCE(NULLIF(exif_json,''),'{}'),"
                "'$.datetime',?,'$.date_source','filename') "
                "WHERE id=? AND date_source='mtime'",
                (item["resolved"], item["resolved"], item["id"]),
            )
        for item in clearing:
            connection.execute(
                "UPDATE photo_scores SET exif_datetime=NULL,date_source='none',"
                "exif_json=json_set(COALESCE(NULLIF(exif_json,''),'{}'),"
                "'$.datetime',json('null'),'$.date_source','none') "
                "WHERE id=? AND date_source='mtime'",
                (item["id"],),
            )


def main() -> int:
    """解析参数，默认预览，显式 --apply 才写库。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="SQLite 数据库路径")
    parser.add_argument(
        "--apply", action="store_true", help="真正写入；省略时只打印计划"
    )
    arguments = parser.parse_args()

    database = Path(arguments.database).expanduser().resolve()
    if not database.is_file():
        print(f"数据库不存在: {database}", file=sys.stderr)
        return 1

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        fixable, clearing = build_plan(connection)
        if not fixable and not clearing:
            print("没有 date_source='mtime' 的照片，无需处理")
            return 0
        print_plan(fixable, clearing)
        if not arguments.apply:
            print("\n以上仅为预览，未改动数据库。确认无误后加 --apply 执行。")
            return 0
        backup = backup_database(database)
        print(f"\n已备份到 {backup}")
        apply_plan(connection, fixable, clearing)
        remaining = connection.execute(
            "SELECT COUNT(*) FROM photo_scores WHERE date_source='mtime'"
        ).fetchone()[0]
        print(f"已修正 {len(fixable)} 张，清空 {len(clearing)} 张")
        print(f"剩余 date_source='mtime' 记录: {remaining}（应为 0）")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
