#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填已入库照片的元数据（不调用 VLM，不花额度）。

适用场景：改了日期兜底规则或类型规范化规则之后，让存量数据跟上新逻辑。
分析脚本按 path 去重会跳过已分析照片，所以存量数据不会自动更新。

回填内容：
- exif_datetime + date_source：按 EXIF → XMP → 文件名 → mtime 四级兜底重算
- exif_json 里的 datetime / date_source：render 的候选池从这里取日期
- type：统一成 '/' 分隔

用法：
    ./venv/bin/python scripts/backfill_metadata.py            # 预览，不写库
    ./venv/bin/python scripts/backfill_metadata.py --apply    # 实际写入
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src" / "analysis"))

import analyze_photos_docker as a  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    db = a.DB_PATH
    if not db.exists():
        print(f"找不到数据库: {db}")
        return 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    a.ensure_table(conn)  # 确保 date_source 列存在
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT id, path, type, exif_datetime, exif_json, date_source FROM photo_scores ORDER BY id"
    ).fetchall()

    print(f"数据库: {db}")
    print(f"exiftool 可用: {a.EXIFTOOL_AVAILABLE}")
    print(f"记录数: {len(rows)}    模式: {'写入' if apply else '预览（加 --apply 才写库）'}\n")

    changed = 0
    missing_file = 0
    for r in rows:
        p = Path(r["path"])
        if not p.exists():
            missing_file += 1
            print(f"  id={r['id']:<4} [文件不存在] {p}")
            continue

        # 重算日期：以现存 EXIF 拍摄时间为起点，走四级兜底
        exif_now = a.read_exif(p)
        new_dt, new_src = a.resolve_datetime(p, exif_now.get("datetime"))
        new_type = a.normalize_type(r["type"])

        old_dt, old_src, old_type = r["exif_datetime"], r["date_source"], r["type"]
        diffs = []
        if (new_dt or "") != (old_dt or ""):
            diffs.append(f"date {old_dt!r} -> {new_dt!r}")
        if (new_src or "") != (old_src or ""):
            diffs.append(f"source {old_src!r} -> {new_src!r}")
        if new_type != (old_type or ""):
            diffs.append(f"type {old_type!r} -> {new_type!r}")

        if not diffs:
            continue

        changed += 1
        print(f"  id={r['id']:<4} {p.name}")
        for d in diffs:
            print(f"           {d}")

        if apply:
            # exif_json 同步更新：render 的候选池只看这里的 datetime
            try:
                ej = json.loads(r["exif_json"]) if r["exif_json"] else {}
            except Exception:
                ej = {}
            ej["datetime"] = new_dt
            ej["date_source"] = new_src
            cur.execute(
                "UPDATE photo_scores SET exif_datetime=?, date_source=?, exif_json=?, type=? WHERE id=?",
                (new_dt, new_src, json.dumps(ej, ensure_ascii=False, default=str), new_type, r["id"]),
            )

    if apply:
        conn.commit()

    print(f"\n需更新 {changed} 条，文件缺失 {missing_file} 条。{'已写入。' if apply else '未写入（预览模式）。'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
