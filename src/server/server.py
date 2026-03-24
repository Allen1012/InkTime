from flask import Flask, render_template, request, send_file, abort, Response
import os
import html
import sqlite3
import time
from pathlib import Path
from io import BytesIO
import mimetypes

# 获取项目根目录
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入渲染模块
try:
    import sys
    sys.path.insert(0, os.path.join(ROOT_DIR, 'src', 'render'))
    import render_daily_photo as rdp
except Exception as e:
    print(f"渲染模块导入失败: {e}")
    rdp = None

app = Flask(__name__)

# 配置
DB_PATH = Path(os.path.join(ROOT_DIR, 'data', 'photos.db'))
IMAGE_DIR = Path(os.path.join(ROOT_DIR, 'data', 'photos'))
BIN_OUTPUT_DIR = Path(os.path.join(ROOT_DIR, 'data', 'output'))
DOWNLOAD_KEY = 'inktime'
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5005
DAILY_PHOTO_QUANTITY = 5
ENABLE_REVIEW_WEBUI = True

# 缓存配置
_MD_CACHE: dict = {}
_MD_CACHE_TTL_SEC = 3600  # 1小时

# 确保目录存在
os.makedirs(BIN_OUTPUT_DIR, exist_ok=True)

# 辅助函数
def extract_date_from_exif(exif_json: str) -> str:
    """从 EXIF JSON 中提取日期"""
    try:
        import json
        data = json.loads(exif_json)
        return data.get('DateTime', '')
    except Exception:
        return ''

def _load_all_md_list() -> list[str]:
    """从全库提取所有存在的 MM-DD（去重、排序）。用于前端"随机一天"。"""
    if not DB_PATH.exists():
        return []
    # 简单 TTL 缓存
    now = time.time()
    try:
        built_at = float(_MD_CACHE.get("built_at") or 0.0)
    except Exception:
        built_at = 0.0
    if (now - built_at) < _MD_CACHE_TTL_SEC:
        cached = _MD_CACHE.get("md_list")
        if isinstance(cached, list):
            return [str(x) for x in cached]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    rows = c.execute("SELECT exif_json FROM photo_scores").fetchall()
    conn.close()
    s: set[str] = set()
    for (exif_json,) in rows:
        d = extract_date_from_exif(exif_json)
        if d and len(d) >= 10:
            md = d[5:10]
            if len(md) == 5 and md[2] == "-":
                s.add(md)
    md_list = sorted(s)
    _MD_CACHE["md_list"] = md_list
    _MD_CACHE["built_at"] = now
    return md_list

def _require_webui_enabled() -> None:
    if not ENABLE_REVIEW_WEBUI:
        abort(404)

# 路由
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/photo/<int:photo_id>')
def photo(photo_id):
    return render_template('photo.html')

@app.route('/category')
def category():
    return render_template('category.html')

@app.route('/search')
def search():
    query = request.args.get('q', '')
    return render_template('search.html', query=query)

# 纯展示页面路由
@app.route('/display')
def display():
    return render_template('display.html')

@app.route('/display/<int:photo_id>')
def display_photo(photo_id):
    return render_template('display.html')

# API 路由
@app.route('/api/display/next')
def api_display_next():
    # 这里可以实现获取下一张照片的逻辑
    # 目前返回模拟数据
    return {
        'id': 2,
        'title': '照片 2',
        'caption': '这是下一张照片',
        'image_url': 'https://picsum.photos/800/600?random=2',
        'location': '上海',
        'date_taken': '2024-01-02T12:00:00'
    }

@app.route('/api/display/prev')
def api_display_prev():
    # 这里可以实现获取上一张照片的逻辑
    # 目前返回模拟数据
    return {
        'id': 1,
        'title': '照片 1',
        'caption': '这是上一张照片',
        'image_url': 'https://picsum.photos/800/600?random=1',
        'location': '北京',
        'date_taken': '2024-01-01T12:00:00'
    }

# 电子墨水屏服务端路由
@app.post("/api/render")
def api_render():
    """渲染照片"""
    if not rdp:
        return {"status": "error", "message": "渲染模块未加载"}
    
    try:
        # 这里可以实现渲染照片的逻辑
        # 目前返回模拟数据
        return {
            "status": "ok",
            "message": "渲染成功"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/settings")
def api_settings_get():
    """获取设置"""
    return {
        "daily_photo_quantity": DAILY_PHOTO_QUANTITY,
        "image_dir": str(IMAGE_DIR),
        "enable_review_webui": ENABLE_REVIEW_WEBUI
    }

@app.post("/api/settings")
def api_settings_post():
    """更新设置"""
    try:
        # 这里可以实现更新设置的逻辑
        # 目前返回模拟数据
        return {
            "status": "ok",
            "message": "设置更新成功"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/md_list")
def api_md_list():
    """获取所有存在的 MM-DD 列表"""
    try:
        md_list = _load_all_md_list()
        return {"status": "ok", "data": md_list}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/random_day")
def api_random_day():
    """获取随机一天"""
    try:
        md_list = _load_all_md_list()
        if not md_list:
            return {"status": "error", "message": "没有找到照片"}
        import random
        random_md = random.choice(md_list)
        return {"status": "ok", "data": random_md}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/photos")
def api_photos():
    """获取照片列表"""
    try:
        # 获取查询参数
        page = int(request.args.get('page', 1))
        filter = request.args.get('filter', 'all')
        sort = request.args.get('sort', 'latest')
        limit = int(request.args.get('limit', 12))
        offset = (page - 1) * limit
        
        # 连接数据库
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # 构建查询
        query = "SELECT id, path, caption, type, memory_score, beauty_score, reason, width, height, orientation, used_at, exif_datetime, exif_make, exif_model, exif_iso, exif_exposure_time, exif_f_number, exif_focal_length, exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city FROM photo_scores"
        
        # 添加筛选条件
        if filter != 'all':
            query += f" WHERE type LIKE '%{filter}%'"
        
        # 添加排序条件
        if sort == 'latest':
            query += " ORDER BY exif_datetime DESC"
        elif sort == 'oldest':
            query += " ORDER BY exif_datetime ASC"
        elif sort == 'memory':
            query += " ORDER BY memory_score DESC"
        elif sort == 'beauty':
            query += " ORDER BY beauty_score DESC"
        
        # 添加分页
        query += f" LIMIT {limit} OFFSET {offset}"
        
        # 执行查询
        rows = c.execute(query).fetchall()
        
        # 获取总记录数
        count_query = "SELECT COUNT(*) FROM photo_scores"
        if filter != 'all':
            count_query += f" WHERE type LIKE '%{filter}%'"
        total = c.execute(count_query).fetchone()[0]
        
        # 关闭数据库连接
        conn.close()
        
        # 转换结果
        photos = []
        for row in rows:
            photo = {
                'id': row['id'],  # 使用自增 ID 作为照片标识符
                'path': row['path'],
                'title': row['path'].split('/')[-1],
                'description': row['caption'],
                'date_taken': row['exif_datetime'],
                'location': row['exif_city'],
                'thumbnail_url': f"/api/photo/thumbnail?path={row['path']}",
                'full_url': f"/api/photo/full?path={row['path']}",
                'category': row['type'],
                'memory_score': row['memory_score'],
                'beauty_score': row['beauty_score'],
                'side_caption': row['side_caption']
            }
            photos.append(photo)
        
        return {
            "status": "ok",
            "data": {
                "items": photos,
                "total": total,
                "page": page,
                "limit": limit
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/photo/thumbnail")
def api_photo_thumbnail():
    """获取照片缩略图"""
    try:
        path = request.args.get('path')
        if not path:
            return {"status": "error", "message": "缺少路径参数"}
        
        # 检查文件是否存在
        photo_path = Path(path)
        if not photo_path.exists():
            return {"status": "error", "message": "文件不存在"}
        
        # 生成缩略图
        from PIL import Image
        import io
        
        img = Image.open(photo_path)
        img.thumbnail((300, 200))
        
        # 转换为字节流
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG')
        buffer.seek(0)
        
        return Response(buffer, mimetype='image/jpeg')
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/photo/full")
def api_photo_full():
    """获取完整照片"""
    try:
        path = request.args.get('path')
        if not path:
            return {"status": "error", "message": "缺少路径参数"}
        
        # 检查文件是否存在
        photo_path = Path(path)
        if not photo_path.exists():
            return {"status": "error", "message": "文件不存在"}
        
        return send_file(photo_path)
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/photo/<int:photo_id>")
def api_photo_detail(photo_id):
    """获取照片详情"""
    try:
        # 连接数据库
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # 查询照片数据
        # 使用 ID 来查找照片
        row = c.execute("SELECT id, path, caption, type, memory_score, beauty_score, reason, width, height, orientation, used_at, exif_datetime, exif_make, exif_model, exif_iso, exif_exposure_time, exif_f_number, exif_focal_length, exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city FROM photo_scores WHERE id = ?", (photo_id,)).fetchone()
        
        # 关闭数据库连接
        conn.close()
        
        # 查找匹配的照片
        matched_photo = row
        
        if not matched_photo:
            return {"status": "error", "message": "照片不存在"}
        
        # 构建响应数据
        photo = {
            'id': photo_id,
            'path': matched_photo['path'],
            'title': matched_photo['path'].split('/')[-1],
            'description': matched_photo['caption'],
            'date_taken': matched_photo['exif_datetime'],
            'location': matched_photo['exif_city'],
            'camera': f"{matched_photo['exif_make']} {matched_photo['exif_model']}" if matched_photo['exif_make'] and matched_photo['exif_model'] else '未知',
            'resolution': f"{matched_photo['width']} x {matched_photo['height']}" if matched_photo['width'] and matched_photo['height'] else '未知',
            'image_url': f"/api/photo/full?path={matched_photo['path']}",
            'memory_score': matched_photo['memory_score'],
            'beauty_score': matched_photo['beauty_score'],
            'score_reason': matched_photo['reason'],
            'exif_data': {
                '相机厂商': matched_photo['exif_make'] or '未知',
                '相机型号': matched_photo['exif_model'] or '未知',
                '焦距': f"{matched_photo['exif_focal_length']}mm" if matched_photo['exif_focal_length'] else '未知',
                '光圈': f"f/{matched_photo['exif_f_number']}" if matched_photo['exif_f_number'] else '未知',
                '快门速度': f"{matched_photo['exif_exposure_time']}s" if matched_photo['exif_exposure_time'] else '未知',
                'ISO': matched_photo['exif_iso'] or '未知',
                '拍摄时间': matched_photo['exif_datetime'] or '未知',
                'GPS 纬度': matched_photo['exif_gps_lat'] or '未知',
                'GPS 经度': matched_photo['exif_gps_lon'] or '未知',
                'GPS 海拔': f"{matched_photo['exif_gps_alt']}m" if matched_photo['exif_gps_alt'] else '未知'
            },
            'side_caption': matched_photo['side_caption']
        }
        
        return {
            "status": "ok",
            "data": photo
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/static/inktime/<key>/photo_<int:idx>.bin")
def esp_photo(key: str, idx: int):
    if key != DOWNLOAD_KEY:
        abort(404)
    if idx < 0 or idx >= DAILY_PHOTO_QUANTITY:
        abort(404)
    p = BIN_OUTPUT_DIR / f"photo_{idx}.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/latest.bin")
def esp_latest(key: str):
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "latest.bin"
    return _send_static_file(p)


@app.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    if key != DOWNLOAD_KEY:
        abort(404)
    p = BIN_OUTPUT_DIR / "preview.png"
    return _send_static_file(p)

@app.get("/files/")
@app.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    _require_webui_enabled()
    try:
        p = _safe_join(BIN_OUTPUT_DIR, subpath)
    except Exception:
        abort(400)

    if p.is_file():
        return _send_static_file(p)

    if not p.exists() or not p.is_dir():
        abort(404)

    items = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        name = child.name + ("/" if child.is_dir() else "")
        rel = child.relative_to(BIN_OUTPUT_DIR)
        href = "/files/" + str(rel).replace("\\", "/")
        items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')

    up = ""
    if p != BIN_OUTPUT_DIR:
        parent_rel = p.parent.relative_to(BIN_OUTPUT_DIR)
        up_href = "/files/" + str(parent_rel).replace("\\", "/")
        up = f'<a href="{html.escape(up_href)}">⬅ 返回上级</a><br><br>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>InkTime Files</title>
<style>
body {{ font-family: -apple-system,BlinkMacSystemFont,system-ui,sans-serif; padding: 24px; }}
ul {{ line-height: 1.8; }}
code {{ background:#f2f2f2; padding:2px 6px; border-radius:4px; }}
</style>
</head>
<body>
<h3>输出目录浏览</h3>
<p>当前：<code>{html.escape(str(p.relative_to(BIN_OUTPUT_DIR) if p != BIN_OUTPUT_DIR else "."))}</code></p>
{up}
<ul>
{''.join(items)}
</ul>
</body>
</html>
"""

# 辅助函数
def _safe_join(base: Path, rel: str) -> Path:
    """防目录穿越：只允许 base 下的相对路径"""
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve())):
        raise ValueError("path traversal blocked")
    return p

def _send_static_file(p: Path) -> Response:
    """发送静态文件"""
    if not p.exists() or not p.is_file():
        abort(404)

    if p.suffix.lower() == ".bin":
        return send_file(p, mimetype="application/octet-stream", as_attachment=False)

    mt, _ = mimetypes.guess_type(str(p))
    if mt:
        return send_file(p, mimetype=mt, as_attachment=False)
    return send_file(p, as_attachment=False)

if __name__ == '__main__':
    mimetypes.add_type("application/octet-stream", ".bin")
    print(f"[InkTime] DB: {DB_PATH}")
    print(f"[InkTime] IMAGE_DIR: {IMAGE_DIR}")
    print(f"[InkTime] OUT: {BIN_OUTPUT_DIR}")
    print(f"[InkTime] key: {DOWNLOAD_KEY}")
    print(f"[InkTime] listen: {FLASK_HOST}:{FLASK_PORT}")
    print(f"[InkTime] open: http://127.0.0.1:{FLASK_PORT}/  (本机)")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)