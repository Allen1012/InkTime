"""保持既有 URL 和响应契约的公开 Blueprint。"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, render_template, request, send_file

from ..errors import ParameterError, ResourceNotFoundError
from ..services import FileContent


public_blueprint = Blueprint("public", __name__)


def _service(name: str) -> Any:
    """从当前应用扩展中取得实例级 Service。"""
    return current_app.extensions["inktime_services"][name]


def _project_name() -> str:
    """按当前生效配置读取站点名称，使后台修改无需重启即可反映到页面。"""
    return str(_service("configuration").get("PROJECT_NAME"))


def _weather_flags() -> dict[str, bool]:
    """按当前生效配置返回两套展示模板各自的天气显示开关。

    在服务端决定是否渲染天气元素，而不是交给前端判断：元素不存在时前端连一次
    请求都不会发，沉浸式模板默认关闭天气也就真的零开销。

    Returns:
        含 weather_show（仪表盘天气块）与 weather_corner（沉浸式角标）的字典。
    """
    configuration = _service("configuration")
    values = configuration.get_many(
        ("DISPLAY_WEATHER_SHOW", "DISPLAY_WEATHER_CORNER")
    )
    return {
        "weather_show": bool(values["DISPLAY_WEATHER_SHOW"]),
        "weather_corner": bool(values["DISPLAY_WEATHER_CORNER"]),
    }


def _integer_argument(name: str, default: int) -> int:
    """解析整数查询参数，失败时交给统一参数错误处理。"""
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ParameterError(f"{name} 必须是整数") from error


def _send(content: FileContent):
    """把 Service 文件描述转换为 Flask 文件响应。"""
    return send_file(content.path, mimetype=content.mimetype, as_attachment=False)


@public_blueprint.get("/")
def index():
    """渲染公开相册首页。"""
    return render_template("index.html", project_name=_project_name())


@public_blueprint.get("/photo/<int:photo_id>")
def photo(photo_id: int):
    """渲染照片详情页面，编号仍由前端脚本读取。"""
    return render_template("photo.html", project_name=_project_name())


@public_blueprint.get("/category")
def category():
    """渲染公开分类页面。"""
    return render_template("category.html", project_name=_project_name())


@public_blueprint.get("/search")
def search():
    """渲染公开搜索页面。"""
    return render_template("search.html", query=request.args.get("q", ""), project_name=_project_name())


@public_blueprint.get("/display")
def display():
    """渲染配置或查询参数指定的展示模板。"""
    template = _service("display").template_name(request.args.get("template"))
    return render_template(template, project_name=_project_name(), **_weather_flags())


@public_blueprint.get("/display/<int:photo_id>")
def display_photo(photo_id: int):
    """保持指定照片展示页面的旧行为，不在服务端使用编号。"""
    template = _service("display").template_name(request.args.get("template"))
    return render_template(template, project_name=_project_name(), **_weather_flags())


@public_blueprint.get("/api/display/next")
def api_display_next():
    """返回下一张展示照片并由原 gallery 算法记账。"""
    exclude = request.args.get("exclude", type=int)
    return _service("display").next_photo(exclude)


@public_blueprint.get("/api/display/stats")
def api_display_stats():
    """返回展示轮次统计。"""
    return _service("display").stats()


@public_blueprint.get("/api/display/prev")
def api_display_prev():
    """返回由前端历史栈处理上一张的兼容响应。"""
    return _service("display").previous()


@public_blueprint.post("/api/render")
def api_render():
    """保持电子墨水屏阶段一模拟渲染接口。"""
    return _service("render").render()


@public_blueprint.get("/api/panel")
def api_panel():
    """返回日期、农历节气和历史事件面板数据。"""
    force = request.args.get("force") in ("1", "true", "yes")
    return _service("panel").get_data(force)


@public_blueprint.get("/api/settings")
def api_settings_get():
    """返回保持裸对象契约的公开设置。"""
    return _service("config").public_settings()


@public_blueprint.post("/api/settings")
def api_settings_post():
    """保持与后台接口隔离的公开模拟设置更新。"""
    return _service("config").simulate_update()


@public_blueprint.get("/api/md_list")
def api_md_list():
    """返回数据库中存在的月日清单。"""
    return {"status": "ok", "data": _service("photo").date_list()}


@public_blueprint.get("/api/random_day")
def api_random_day():
    """返回数据库日期清单中的随机一天，并保持旧业务错误状态码。"""
    try:
        return {"status": "ok", "data": _service("photo").random_day()}
    except ResourceNotFoundError as error:
        return {"status": "error", "message": error.public_message}


@public_blueprint.get("/api/photos")
def api_photos():
    """返回公开分页照片列表。"""
    page = _integer_argument("page", 1)
    limit = _integer_argument("limit", 12)
    data = _service("photo").list_photos(page, request.args.get("filter", "all"), request.args.get("sort", "latest"), limit)
    return {"status": "ok", "data": data}


@public_blueprint.get("/api/search")
def api_search():
    """返回公开分页搜索结果。"""
    page = _integer_argument("page", 1)
    limit = _integer_argument("limit", 12)
    return {"status": "ok", "data": _service("photo").search(request.args.get("q", ""), page, limit)}


@public_blueprint.get("/api/category/stats")
def api_category_stats():
    """返回拆分复合标签后的分类统计。"""
    return {"status": "ok", "data": _service("photo").category_stats()}


@public_blueprint.get("/api/category/photos")
def api_category_photos():
    """返回指定分类的分页照片。"""
    page = _integer_argument("page", 1)
    limit = _integer_argument("limit", 12)
    data = _service("photo").category_photos(request.args.get("category", "all"), page, limit)
    return {"status": "ok", "data": data}


# 缩略图缓存时长：源文件变化或缩略图配置调整都会改变校验值，因此可以放长一些
_THUMBNAIL_CACHE_SECONDS = 7 * 24 * 3600


def _conditional_thumbnail(media: Any, path: Any):
    """先比对校验值，命中则直接返回 304，**不生成图片**。

    校验值只依赖 `stat()` 与两个配置值，成本几乎为零；生成一张缩略图要解码四千像素级
    原图，约一百六十毫秒。先前的实现顺序相反，导致 304 只省带宽不省 CPU。

    Args:
        media: 媒体服务。
        path: 已通过边界与可见性校验的照片路径。

    Returns:
        200 图片响应或 304 空响应，均带缓存头。
    """
    etag = media.thumbnail_etag(path)
    if request.if_none_match.contains_weak(etag.strip('W/"')):
        response = Response(status=304)
    else:
        content = media.render_thumbnail(path)
        response = Response(content.data, mimetype=content.mimetype)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"private, max-age={_THUMBNAIL_CACHE_SECONDS}"
    return response


@public_blueprint.get("/api/photo/thumbnail")
def api_photo_thumbnail():
    """返回经过照片目录边界校验的 JPEG 缩略图，并支持条件请求。"""
    media = _service("media")
    try:
        path = media.resolve_photo(request.args.get("path", ""), require_visible=True)
    except (ParameterError, ResourceNotFoundError) as error:
        return {"status": "error", "message": error.public_message}
    return _conditional_thumbnail(media, path)


@public_blueprint.get("/api/photo/full")
def api_photo_full():
    """返回经过 IMAGE_DIR 边界校验的完整照片。"""
    try:
        return _send(_service("media").full_photo(request.args.get("path", "")))
    except (ParameterError, ResourceNotFoundError) as error:
        return {"status": "error", "message": error.public_message}


@public_blueprint.get("/api/photo/<int:photo_id>")
def api_photo_detail(photo_id: int):
    """按编号返回兼容现有字段的照片详情。"""
    try:
        return {"status": "ok", "data": _service("photo").detail(photo_id)}
    except ResourceNotFoundError as error:
        return {"status": "error", "message": error.public_message}


@public_blueprint.get("/static/inktime/<key>/photo_<int:idx>.bin")
def esp_photo(key: str, idx: int):
    """返回指定编号的电子相框二进制文件。"""
    return _send(_service("device").photo(key, idx))


@public_blueprint.get("/static/inktime/<key>/latest.bin")
def esp_latest(key: str):
    """返回电子相框兼容下载路径 latest.bin。"""
    return _send(_service("device").latest(key))


@public_blueprint.get("/static/inktime/<key>/preview.png")
def esp_preview(key: str):
    """返回电子相框预览图片。"""
    return _send(_service("device").preview(key))


@public_blueprint.get("/files/")
@public_blueprint.get("/files/<path:subpath>")
def browse(subpath: str = ""):
    """按配置浏览输出目录或发送其中的文件。"""
    result = _service("files").browse(subpath)
    if isinstance(result, FileContent):
        return _send(result)
    return Response(result, mimetype="text/html")
