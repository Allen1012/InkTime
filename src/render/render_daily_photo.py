#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
每日相册渲染脚本：
- 从 photos.db / photo_scores 中选出一张“历史上的今天”照片
- 按 InkTime 模拟器的布局渲染到 480x800
- 用 LXGWHeartSerifMN.ttf 把文案 / 日期 / 地点都画到图上
- 转成四色墨水屏（黑/白/红/黄）图像，并保存为 BIN（1 字节 1 像素，行优先）
- 同时导出 latest.h 头文件数组，给 ESP32 直接 include
"""

from __future__ import annotations

from pathlib import Path
import json
import datetime as dt
import os
import threading
from typing import List, Dict, Any, Tuple, Optional, Mapping
from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.database import database_connection

# 配置来源：.env 文件 + 环境变量（.env 为唯一配置源，不再依赖 config.py）
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


TODAY = dt.date.today()

# === 路径配置（来自 .env / 环境变量） ===
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

if load_dotenv:
    _env_file = ROOT_DIR / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)

DB_PATH = Path(str(os.environ.get("DB_PATH", "photos.db") or "photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

BIN_OUTPUT_DIR = Path(str(os.environ.get("BIN_OUTPUT_DIR", "output/inktime") or "output/inktime")).expanduser()
if not BIN_OUTPUT_DIR.is_absolute():
    BIN_OUTPUT_DIR = (ROOT_DIR / BIN_OUTPUT_DIR).resolve()
BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_PATH = Path(str(os.environ.get("FONT_PATH", "") or "")).expanduser()
if str(os.environ.get("FONT_PATH", "") or "") and not FONT_PATH.is_absolute():
    FONT_PATH = (ROOT_DIR / FONT_PATH).resolve()

MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", 70.0) or 70.0)
DAILY_PHOTO_QUANTITY = int(os.environ.get("DAILY_PHOTO_QUANTITY", 5) or 5)
# 当「历史上的今天」照片数不足 DAILY_PHOTO_QUANTITY 时，是否从全局高分照片补足。
# 相册照片较少时开启，可保证每天稳定产出 DAILY_PHOTO_QUANTITY 张。
FILL_FROM_GLOBAL = str(os.environ.get("FILL_FROM_GLOBAL", "True")).strip().lower() in ("1", "true", "yes", "on")
_RENDER_CONFIGURATION_LOCK = threading.RLock()

# 墨水屏尺寸
CANVAS_WIDTH = 480
CANVAS_HEIGHT = 800

# 底部文字区域高度
TEXT_AREA_HEIGHT = 100


# ========== DB 与 EXIF 处理 ==========

def extract_date_from_exif(exif_json: Optional[str]) -> str:
    """
    从 EXIF JSON 中提取拍摄日期，返回 YYYY-MM-DD 格式，失败则返回空字符串。
    逻辑与 review_web.py 中保持一致。
    """
    if not exif_json:
        return ""
    try:
        data = json.loads(exif_json)
    except Exception:
        return ""
    dt_str = data.get("datetime")
    if not dt_str:
        return ""
    try:
        date_part = str(dt_str).split()[0]
        parts = date_part.replace(":", "-").split("-")
        if len(parts) >= 3:
            return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except Exception:
        return ""
    return ""


def load_sim_rows() -> List[Dict[str, Any]]:
    """
    加载 InkTime 用的核心字段：
    - path: 照片路径
    - exif_json: 用于解析日期 / GPS
    - side_caption: 文案
    - memory_score: 回忆度
    - exif_gps_lat / exif_gps_lon / exif_city: 地点信息（纯本地，不上网）
    """
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库文件: {DB_PATH}")

    with database_connection(DB_PATH, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT id,
                   path,
                   exif_datetime,
                   side_caption,
                   memory_score,
                   exif_gps_lat,
                   exif_gps_lon,
                   exif_city
            FROM photo_scores
            WHERE is_included = 1
              AND is_deleted = 0
              AND analysis_status IN ('legacy', 'succeeded')
            """
        ).fetchall()

    items: List[Dict[str, Any]] = []
    for photo_id, path, exif_datetime, side_caption, memory_score, gps_lat, gps_lon, exif_city in rows:
        date_str = extract_date_from_exif(
            json.dumps({"datetime": exif_datetime}, ensure_ascii=False)
        )
        # 再次兜底过滤 Screenshot 等
        if "screenshot" in str(path).lower():
            continue

        # 没有拍摄时间的照片同样进候选池：既然被放进相册就是想展示，
        # 缺日期只应该让它不参与「历史上的今天」的月日匹配，不该让它永不露面。
        # md 留空 -> 不进月日分组，但仍可被补足与全局兜底选中；画布上日期渲染为空串。
        md = ""
        if date_str:
            try:
                _y, m, d = map(int, date_str.split("-"))
                md = f"{m:02d}-{d:02d}"
            except Exception:
                date_str, md = "", ""

        item = {
            "photo_id": int(photo_id),
            "path": str(path),
            "date": date_str or "",  # YYYY-MM-DD，无拍摄时间时为空串
            "md": md,                # MM-DD，空串表示不参与月日匹配
            "side": side_caption or "",
            "memory": float(memory_score) if memory_score is not None else -1.0,
            "lat": gps_lat,
            "lon": gps_lon,
            "city": exif_city or "",
        }
        items.append(item)

    return items


# ========== “历史上的今天”选片 ==========

def md_to_day_of_year(md: str) -> Optional[int]:
    """把 'MM-DD' 转成非闰年的第几天（1~365）。"""
    try:
        m, d = map(int, md.split("-"))
        days_before = [0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        if m < 1 or m > 12:
            return None
        return days_before[m] + d
    except Exception:
        return None


def day_of_year_to_md(day: int) -> str:
    # 选一个非闰年（2001/2005 随便），只依赖 day-of-year。
    base = dt.date(2001, 1, 1) + dt.timedelta(days=day - 1)
    return f"{base.month:02d}-{base.day:02d}"


def choose_photo_for_today(items: List[Dict[str, Any]], today: dt.date) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    选片规则（按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，随机选一张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选 memory 最大的一张作为兜底
    """

    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        # 空 md 表示没有拍摄时间：不参与月日匹配，但仍留在 items 里供补足与兜底选中
        if not md:
            continue
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        chosen = random.choice(candidates)
        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "total_count_md": len(arr),
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
        }
        return chosen, info

    global_best = max(items, key=lambda x: x.get("memory", -1.0))
    info = {
        "target_md": target_md,
        "used_md": global_best["md"],
        "day_offset": None,
        "candidate_count": 1,
        "total_count_md": len(by_md.get(global_best["md"], [])),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
    }
    return global_best, info

def choose_photos_for_today(items: List[Dict[str, Any]], today: dt.date, count: int = 5) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    选片规则（多张版，按月日）：
    - 以 today 的月日为目标，例如 12 月 2 日 -> "12-02"
    - 在所有年份该月日的照片中，找 memory > MEMORY_THRESHOLD 的候选，尽量随机选 count 张
    - 如果该月日没有任何 > 阈值的，则往前一天（月日）继续找（12-01, 11-30, ...），最多回溯 365 天
    - 如果整个 365 天都没有任何 > 阈值的照片，则在全局中选回忆度最高的若干张作为兜底
    """
    if not items:
        raise RuntimeError("没有任何可用照片")

    # 按 md 分组
    by_md: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        md = it["md"]
        # 空 md 表示没有拍摄时间：不参与月日匹配，但仍留在 items 里供补足与兜底选中
        if not md:
            continue
        by_md.setdefault(md, []).append(it)

    # 每组内按 memory 从高到低排序
    for arr in by_md.values():
        arr.sort(key=lambda x: x.get("memory", -1.0), reverse=True)

    target_md = f"{today.month:02d}-{today.day:02d}"
    target_doy = md_to_day_of_year(target_md)
    if target_doy is None:
        raise RuntimeError(f"无法解析今天的月日: {target_md}")

    import random

    for offset in range(0, 365):
        doy = target_doy - offset
        if doy <= 0:
            doy += 365
        md = day_of_year_to_md(doy)

        arr = by_md.get(md, [])
        if not arr:
            continue
        candidates = [p for p in arr if p.get("memory", -1.0) > MEMORY_THRESHOLD]
        if not candidates:
            continue

        # 随机选不重复的多张
        if len(candidates) >= count:
            chosen_list = random.sample(candidates, count)
        else:
            # 候选不足 count 张，先用该日剩余的照片补齐
            chosen_list = list(candidates)
            chosen_paths = {p["path"] for p in chosen_list}
            for extra in arr:
                if extra["path"] in chosen_paths:
                    continue
                chosen_list.append(extra)
                chosen_paths.add(extra["path"])
                if len(chosen_list) >= count:
                    break

        # 该日照片总数不足 count 时，按开关从全局高分照片补足，
        # 保证每天稳定产出 count 张供 ESP32 随机取用
        filled_from_global = 0
        if FILL_FROM_GLOBAL and len(chosen_list) < count:
            chosen_paths = {p["path"] for p in chosen_list}
            for extra in sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True):
                if extra["path"] in chosen_paths:
                    continue
                chosen_list.append(extra)
                chosen_paths.add(extra["path"])
                filled_from_global += 1
                if len(chosen_list) >= count:
                    break

        info = {
            "target_md": target_md,
            "used_md": md,
            "day_offset": -offset,
            "candidate_count": len(candidates),
            "total_count_md": len(arr),
            "filled_from_global": filled_from_global,
            "threshold": MEMORY_THRESHOLD,
            "fallback_global_max": False,
        }
        return chosen_list, info

    # 兜底：全局回忆度最高的若干张
    sorted_all = sorted(items, key=lambda x: x.get("memory", -1.0), reverse=True)
    chosen_list = sorted_all[:count]
    info = {
        "target_md": target_md,
        "used_md": chosen_list[0]["md"] if chosen_list else "",
        "day_offset": None,
        "candidate_count": len(chosen_list),
        "total_count_md": len(items),
        "threshold": MEMORY_THRESHOLD,
        "fallback_global_max": True,
    }
    return chosen_list, info
# ========== 绘制 + 抖动 ==========

# 四色墨水屏调色板（RGB）
PALETTE = [
    (0, 0, 0),         # 0 = 黑
    (255, 255, 255),   # 1 = 白
    (200, 0, 0),       # 2 = 红
    (220, 180, 0),     # 3 = 黄
]


def nearest_palette_color(r: float, g: float, b: float) -> Tuple[int, int, int, int]:
    """
    返回 (idx, pr, pg, pb)，idx 为 PALETTE 中最近颜色的索引。
    """
    best_idx = 0
    best_dist = float("inf")
    for i, (pr, pg, pb) in enumerate(PALETTE):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr * dr + dg * dg + db * db
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    pr, pg, pb = PALETTE[best_idx]
    return best_idx, pr, pg, pb


def wrap_text_chinese(draw: ImageDraw.ImageDraw,
                      text: str,
                      font: ImageFont.FreeTypeFont,
                      max_width: int,
                      max_lines: int) -> List[str]:
    """
    简单中文按字符宽度折行。
    """
    if not text:
        return []
    lines: List[str] = []
    line = ""
    for ch in text:
        test = line + ch
        w = draw.textlength(test, font=font)
        if w <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = ch
            if len(lines) >= max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    return lines


def format_date_display(date_str: str) -> str:
    """
    "YYYY-MM-DD" -> "YYYY.M.D"
    """
    if not date_str:
        return ""
    parts = date_str.split("-")
    if len(parts) < 3:
        return date_str
    y = parts[0]
    try:
        m = str(int(parts[1]))
        d = str(int(parts[2]))
    except Exception:
        return date_str
    return f"{y}.{m}.{d}"


def format_location(lat, lon, city: str) -> str:
    """
    地点字符串：
    - 有 city 用 city
    - 否则如果有 lat/lon，用 "lat, lon"（5 位小数）
    - 否则空字符串（不写“未知地点”）
    """
    if city and str(city).strip():
        return str(city).strip()
    if lat is None or lon is None:
        return ""
    try:
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except Exception:
        return ""


def render_image(item: Dict[str, Any]) -> Image.Image:
    """
    根据选中的 item 渲染一张 480x800 的 RGB 图像（竖屏）：
    - 上方图片：占 [0, CANVAS_HEIGHT - TEXT_AREA_HEIGHT)
    - 底部 TEXT_AREA_HEIGHT 像素为文字区：第一行 side 文案（最多两行），第二行日期 + 地点
    """
    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # ---------- 加载原图并按 EXIF 方向纠正 ----------
    img_path = Path(item["path"])
    if not img_path.exists():
        raise RuntimeError(f"图片不存在: {img_path}")
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img).convert("RGB")

    img_w, img_h = img.size
    if img_w == 0 or img_h == 0:
        raise RuntimeError(f"图片尺寸非法: {img.size}")

    # ---------- 照片区域 ----------
    img_area_w = CANVAS_WIDTH
    img_area_h = CANVAS_HEIGHT - TEXT_AREA_HEIGHT  # 底部留给文字

    # “铺满裁剪”：缩放到至少覆盖区域，再从中间裁一块
    scale = max(img_area_w / img_w, img_area_h / img_h)
    draw_w = int(img_w * scale)
    draw_h = int(img_h * scale)

    img_resized = img.resize((draw_w, draw_h), Image.LANCZOS)

    left = max(0, (draw_w - img_area_w) // 2)
    top = max(0, (draw_h - img_area_h) // 2)
    right = left + img_area_w
    bottom = top + img_area_h
    img_cropped = img_resized.crop((left, top, right, bottom))

    # 贴到上方
    canvas.paste(img_cropped, (0, 0))

    # ---------- 底部文字区域 ----------
    padding_x = 24
    text_area_top = CANVAS_HEIGHT - TEXT_AREA_HEIGHT + 10
    text_width = CANVAS_WIDTH - 2 * padding_x

    try:
        font_big = ImageFont.truetype(str(FONT_PATH), 22)  # 文案
        font_small = ImageFont.truetype(str(FONT_PATH), 20)  # 日期/地点
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    side_text = item.get("side") or ""

    # 文案：最多两行，从 text_area_top 开始
    y = text_area_top
    if side_text:
        lines = wrap_text_chinese(draw, side_text, font_big, text_width, max_lines=2)
        for line in lines:
            draw.text((padding_x, y), line, font=font_big, fill=(0, 0, 0))
            y += 24  # 行高略大于字号

    # 日期 + 地点：固定在底部区域内的第二行
    date_display = format_date_display(item["date"])
    loc_display = format_location(item.get("lat"), item.get("lon"), item.get("city") or "")

    second_line_y = text_area_top + 54
    draw.text((padding_x, second_line_y), date_display, font=font_small, fill=(0, 0, 0))

    loc_w = draw.textlength(loc_display, font=font_small)
    loc_x = padding_x + text_width - loc_w
    if loc_x < padding_x:
        loc_x = padding_x
    draw.text((loc_x, second_line_y), loc_display, font=font_small, fill=(0, 0, 0))

    return canvas

def apply_four_color_dither(img: Image.Image) -> Image.Image:
    """
    对图像做 Floyd–Steinberg 抖动，量化到四种颜色（黑/白/红/黄）。
    """
    img = img.convert("RGB")
    w, h = img.size
    pixels = img.load()

    err_r = [0.0] * w
    err_g = [0.0] * w
    err_b = [0.0] * w
    next_err_r = [0.0] * w
    next_err_g = [0.0] * w
    next_err_b = [0.0] * w

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            r = max(0.0, min(255.0, r + err_r[x]))
            g = max(0.0, min(255.0, g + err_g[x]))
            b = max(0.0, min(255.0, b + err_b[x]))

            idx, pr, pg, pb = nearest_palette_color(r, g, b)

            # 写回量化后的颜色
            pixels[x, y] = (pr, pg, pb)

            # 误差
            er = r - pr
            eg = g - pg
            eb = b - pb

            # Floyd–Steinberg:
            #        *   7/16
            #   3/16 5/16 1/16
            if x + 1 < w:
                err_r[x + 1] += er * (7.0 / 16.0)
                err_g[x + 1] += eg * (7.0 / 16.0)
                err_b[x + 1] += eb * (7.0 / 16.0)
            if y + 1 < h:
                if x > 0:
                    next_err_r[x - 1] += er * (3.0 / 16.0)
                    next_err_g[x - 1] += eg * (3.0 / 16.0)
                    next_err_b[x - 1] += eb * (3.0 / 16.0)
                next_err_r[x] += er * (5.0 / 16.0)
                next_err_g[x] += eg * (5.0 / 16.0)
                next_err_b[x] += eb * (5.0 / 16.0)
                if x + 1 < w:
                    next_err_r[x + 1] += er * (1.0 / 16.0)
                    next_err_g[x + 1] += eg * (1.0 / 16.0)
                    next_err_b[x + 1] += eb * (1.0 / 16.0)

        if y + 1 < h:
            # 把 next_err_* 移到当前行，并清零 next_err_*
            for i in range(w):
                err_r[i] = next_err_r[i]
                err_g[i] = next_err_g[i]
                err_b[i] = next_err_b[i]
                next_err_r[i] = 0.0
                next_err_g[i] = 0.0
                next_err_b[i] = 0.0

    return img


def image_to_palette_bin(img: Image.Image) -> bytes:
    """
    把已经量化到 PALETTE 的图像转换成 BIN：
    - 行优先，从上到下，从左到右
    - 每像素 1 字节：0=黑,1=白,2=红,3=黄
    """
    img = img.convert("RGB")
    if img.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RuntimeError(f"图像尺寸错误：{img.size}，应为 {(CANVAS_WIDTH, CANVAS_HEIGHT)}")

    data = bytearray(CANVAS_WIDTH * CANVAS_HEIGHT)
    idx_map = {c: i for i, c in enumerate(PALETTE)}  # (r,g,b) -> index

    for y in range(CANVAS_HEIGHT):
        for x in range(CANVAS_WIDTH):
            r, g, b = img.getpixel((x, y))
            key = (int(r), int(g), int(b))
            idx = idx_map.get(key)
            if idx is None:
                idx, _, _, _ = nearest_palette_color(r, g, b)
            data[y * CANVAS_WIDTH + x] = idx

    return bytes(data)


def write_h_array(bin_path: Path, h_path: Path, array_name: str = "daily_bin"):
    """
    把 BIN 转成 C 数组头文件 latest.h：
    const unsigned int daily_bin_size = ...;
    const uint8_t daily_bin[] = { 0x00, 0x01, ... };
    """
    data = bin_path.read_bytes()
    with open(h_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated from render_daily_photo.py\n")
        f.write(f"// Size = {len(data)} bytes (480x800, 1 byte/pixel)\n\n")
        f.write(f"const unsigned int {array_name}_size = {len(data)};\n")
        f.write(f"const uint8_t {array_name}[] = {{\n    ")

        for i, b in enumerate(data):
            f.write(f"0x{b:02X}, ")
            if (i + 1) % 16 == 0:
                f.write("\n    ")

        f.write("\n};\n")


# ========== 主流程 ==========

def main(
    output_directory: Path | None = None,
    database_path: Path | None = None,
    today: dt.date | None = None,
    *,
    settings: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """在锁内应用可选任务配置，渲染四色正式兼容产物并返回清单。

    Args:
        output_directory: 显式输出目录；为空时保持命令行历史目录。
        database_path: 显式只读数据库；为空时保持环境配置。
        today: 可注入的选片日期；为空时使用当前日期。
        settings: 可选 render 作用域任务配置；路径仍由前两个安全参数决定。

    Returns:
        产物文件名列表和仅含照片编号的清单。
    """
    global BIN_OUTPUT_DIR, DB_PATH, FONT_PATH
    global MEMORY_THRESHOLD, DAILY_PHOTO_QUANTITY, FILL_FROM_GLOBAL
    _RENDER_CONFIGURATION_LOCK.acquire()
    original_output = BIN_OUTPUT_DIR
    original_database = DB_PATH
    original_font = FONT_PATH
    original_threshold = MEMORY_THRESHOLD
    original_quantity = DAILY_PHOTO_QUANTITY
    original_fill = FILL_FROM_GLOBAL
    try:
        if output_directory is not None:
            BIN_OUTPUT_DIR = Path(output_directory).expanduser().resolve()
        if database_path is not None:
            DB_PATH = Path(database_path).expanduser().resolve()
        if settings is not None:
            font_path = Path(str(settings["FONT_PATH"])).expanduser()
            FONT_PATH = (
                font_path.resolve()
                if font_path.is_absolute()
                else (ROOT_DIR / font_path).resolve()
            )
            MEMORY_THRESHOLD = float(settings["MEMORY_THRESHOLD"])
            DAILY_PHOTO_QUANTITY = int(settings["DAILY_PHOTO_QUANTITY"])
            FILL_FROM_GLOBAL = bool(settings["FILL_FROM_GLOBAL"])
        BIN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        items = load_sim_rows()
        if not items:
            raise RuntimeError("没有可用照片")
        photos, _info = choose_photos_for_today(
            items,
            today or TODAY,
            count=DAILY_PHOTO_QUANTITY,
        )
        if not photos:
            raise RuntimeError("选片结果为空")

        import shutil

        artifacts: List[str] = []
        artifact_photo_ids: Dict[str, List[int]] = {}
        for idx, chosen in enumerate(photos):
            photo_id = int(chosen["photo_id"])
            img_dithered = apply_four_color_dither(render_image(chosen))
            preview_name = f"preview_{idx}.png"
            bin_name = f"photo_{idx}.bin"
            header_name = f"photo_{idx}.h"
            img_dithered.save(BIN_OUTPUT_DIR / preview_name)
            (BIN_OUTPUT_DIR / bin_name).write_bytes(image_to_palette_bin(img_dithered))
            write_h_array(
                BIN_OUTPUT_DIR / bin_name,
                BIN_OUTPUT_DIR / header_name,
                array_name=f"daily_bin_{idx}",
            )
            for name in (preview_name, bin_name, header_name):
                artifacts.append(name)
                artifact_photo_ids[name] = [photo_id]

        compatibility = {
            "latest.bin": "photo_0.bin",
            "latest.h": "photo_0.h",
            "preview.png": "preview_0.png",
        }
        first_photo_id = int(photos[0]["photo_id"])
        for destination_name, source_name in compatibility.items():
            shutil.copyfile(BIN_OUTPUT_DIR / source_name, BIN_OUTPUT_DIR / destination_name)
            artifacts.append(destination_name)
            artifact_photo_ids[destination_name] = [first_photo_id]
        return {
            "artifacts": artifacts,
            "manifest": {
                "photo_ids": [int(item["photo_id"]) for item in photos],
                "artifact_photo_ids": artifact_photo_ids,
            },
        }
    finally:
        BIN_OUTPUT_DIR = original_output
        DB_PATH = original_database
        FONT_PATH = original_font
        MEMORY_THRESHOLD = original_threshold
        DAILY_PHOTO_QUANTITY = original_quantity
        FILL_FROM_GLOBAL = original_fill
        _RENDER_CONFIGURATION_LOCK.release()


if __name__ == "__main__":
    main()