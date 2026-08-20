"""管理员认证、照片管理页面与受保护写接口。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_user, logout_user

from ..admin_jobs import JobTransitionError
from ..auth import InvalidInitialSetupTokenError, is_safe_next_target
from ..errors import ParameterError
from ..extensions import csrf, login_manager
from ..forms import LoginForm, PhotoEditForm, SetupForm
from ..repositories import FirstAdminAlreadyCreatedError


admin_page_blueprint = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_blueprint = Blueprint("admin_api", __name__, url_prefix="/api/admin")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _authentication_service() -> Any:
    """取得当前应用实例的管理员认证服务。"""
    return current_app.extensions["inktime_services"]["auth"]


def _admin_photo_service() -> Any:
    """取得当前应用实例的后台照片查询服务。"""
    return current_app.extensions["inktime_services"]["admin_photo"]


def _admin_photo_management_service() -> Any:
    """取得当前应用实例的后台照片写入与审计服务。"""
    return current_app.extensions["inktime_services"]["admin_photo_management"]


def _admin_job_service() -> Any:
    """取得当前应用实例的合并任务查询与管理服务。"""
    return current_app.extensions["inktime_services"]["admin_jobs"]


def _photo_job_service() -> Any:
    """取得阶段五照片分析任务服务。"""
    return current_app.extensions["inktime_services"]["photo_jobs"]


def _accepts_json_response() -> bool:
    """判断详情页动作是否明确请求 JSON 响应。"""
    return request.accept_mimetypes.best == "application/json"


def _draft_enqueue_response(photo_id: int, result: Mapping[str, Any], action: str):
    """把草稿入队结果转换为安全 JSON 或带确认提示的页面重定向。"""
    safe_job = _photo_job_service().latest_draft(photo_id)
    if _accepts_json_response():
        return jsonify({"status": "ok", "data": safe_job}), 202
    if result.get("duplicate"):
        flash(f"该照片已有{action}任务，生成完成后请确认并保存")
    else:
        flash(f"已排队{action}，生成完成后请确认并保存")
    return redirect(url_for("admin.photo_detail", photo_id=photo_id))


def _job_mode_conflict_response(error: JobTransitionError, photo_id: int, json_only: bool = False):
    """把正式任务与详情页待确认任务的模式冲突转换为 HTTP 409 响应。"""
    code = str(error)
    messages = {
        "active_formal_job_exists": "该照片已有正式任务在运行，请等待完成后再生成待确认结果",
        "active_draft_job_exists": "该照片已有待确认生成任务，请确认保存或等待结束后再操作",
    }
    if code not in messages:
        raise error
    message = messages[code]
    if json_only or _accepts_json_response():
        return jsonify({
            "status": "error",
            "error": {"code": code, "message": message},
        }), 409
    flash(message)
    return redirect(url_for("admin.photo_detail", photo_id=photo_id))


def _photo_lifecycle_service() -> Any:
    """取得回收站、永久删除与过期清理服务。"""
    return current_app.extensions["inktime_services"]["photo_lifecycle"]


def _upload_service() -> Any:
    """取得当前应用实例的安全上传服务。"""
    return current_app.extensions["inktime_services"]["uploads"]


def _configuration_service() -> Any:
    """取得当前应用实例的统一配置读取、写入与审计服务。"""
    return current_app.extensions["inktime_services"]["configuration"]


def _display_window_context() -> dict[str, Any]:
    """构造展示生效时间段的可读摘要与常用预设，供配置页直接呈现。

    使用者写下的字符串经过跨零点拆分与区间合并后，真正生效的范围可能与直觉不同；
    再叠加「区间右开」与当前切换模式，很容易出现「少刷一次」或「周末连续停两天」
    这类意外。这里把结果与预计次数摊开显示，比让人心算配置更可靠。

    Returns:
        含原始值、摘要行、预计次数与预设列表的上下文字典。
    """
    from src.configuration import (
        describe_time_windows,
        estimate_daily_rotations,
        parse_time_windows,
        WEEKDAY_LABELS,
    )

    configuration = _configuration_service()
    settings = configuration.get_many(
        ("DISPLAY_ACTIVE_WINDOWS", "DISPLAY_ROTATE_MODE", "DISPLAY_ROTATE_INTERVAL_SEC")
    )
    raw = str(settings["DISPLAY_ACTIVE_WINDOWS"] or "")
    try:
        windows = parse_time_windows(raw)
        error = None
    except ValueError as parse_error:
        windows = parse_time_windows("")
        error = str(parse_error)
    counts = estimate_daily_rotations(
        windows,
        str(settings["DISPLAY_ROTATE_MODE"]),
        float(settings["DISPLAY_ROTATE_INTERVAL_SEC"]),
    )
    rotations = None
    if counts is not None:
        rotations = [
            {"label": WEEKDAY_LABELS[weekday], "count": counts[weekday]}
            for weekday in sorted(counts)
        ]
    return {
        "raw": raw,
        "summary": describe_time_windows(windows),
        "error": error,
        "rotations": rotations,
        "rotate_mode": str(settings["DISPLAY_ROTATE_MODE"]),
        "presets": [
            {"label": "全天生效", "value": ""},
            {"label": "每天 09:00 到 22:00", "value": "09:00-22:30"},
            {"label": "工作日 09:00 到 22:00", "value": "Mon-Fri@09:00-22:30"},
            {
                "label": "工作日与周末不同",
                "value": "Mon-Fri@09:00-22:30;Sat,Sun@10:00-23:30",
            },
            {"label": "只在晚间", "value": "18:00-23:30"},
        ],
    }


# 配置页标签布局：按「日常调什么」而非注册表的 group 字段编排，让一次设置所需
# 的配置项落在同一屏。模型接口地址、模型名与密钥必须同段，否则接入一个模型要在
# 两个分类之间来回跳。只读的系统与安全项集中放到最后一个标签，避免占据日常位置。
# 这里只描述展示顺序，可编辑性、类型与校验仍由 src/configuration.py 的注册表决定。
_SETTINGS_TAB_LAYOUT: tuple[dict[str, Any], ...] = (
    {
        "id": "model",
        "label": "模型与分析",
        "icon": "model",
        "summary": "视觉模型接入、照片目录与地理位置推断。改动从下一个分析任务生效。",
        "cards": ("image_dirs",),
        "sections": (
            {
                "label": "模型接口",
                "hint": "接口地址、模型名与密钥放在一起，接入一个新模型只需改这一段。密钥留空表示保持原值。",
                "keys": ("API_URL", "MODEL_NAME", "API_KEY", "TIMEOUT", "VLM_MAX_LONG_EDGE"),
            },
            {
                "label": "照片目录",
                "hint": "只能填写容器已挂载、存在且可读的目录；在线修改不会新增挂载。",
                "keys": ("IMAGE_DIR",),
            },
            {
                "label": "地点与城市推断",
                "hint": "用于把照片坐标换算成中文城市名，并判断是否属于常驻地。",
                "keys": (
                    "WORLD_CITIES_CSV",
                    "CITY_GRID_DEG",
                    "CITY_MAX_DISTANCE_KM",
                    "HOME_LAT",
                    "HOME_LON",
                    "HOME_RADIUS_KM",
                ),
            },
        ),
    },
    {
        "id": "display",
        "label": "展示与天气",
        "icon": "display",
        "summary": "网页展示页的模板、轮播、缩略图、天气与信息面板。保存后立即生效。",
        "cards": ("display_windows",),
        "sections": (
            {
                "label": "站点与功能开关",
                "hint": "产物目录浏览需两个开关同时启用才开放 /files/，不影响照片墙与展示页。",
                "keys": ("PROJECT_NAME", "ENABLE_REVIEW_WEBUI", "ENABLE_FILE_BROWSER"),
            },
            {
                "label": "展示页与轮播",
                "hint": "切换模式为 interval 时才使用下面的间隔秒数。",
                "keys": (
                    "DISPLAY_TEMPLATE",
                    "DISPLAY_ROTATE_MODE",
                    "DISPLAY_ROTATE_INTERVAL_SEC",
                    "DISPLAY_KEEP_AWAKE",
                    "DISPLAY_UI_HIDE_DELAY_SEC",
                    "DISPLAY_MIN_SCORE",
                    "DISPLAY_NEW_PHOTO_WEIGHT",
                ),
            },
            {
                "label": "生效时间段与休息期",
                "hint": "留空表示全天生效；非生效时间段不消耗展示次数。上方卡片显示的是解析后的实际结果。",
                "keys": (
                    "DISPLAY_ACTIVE_WINDOWS",
                    "DISPLAY_IDLE_MODE",
                    "DISPLAY_IDLE_PHOTO_ID",
                    "DISPLAY_REST_TEXT",
                ),
            },
            {
                "label": "缩略图",
                "hint": "照片墙与后台网格所用缩略图。调大长边会提升清晰度并增加缓存体积。",
                "keys": (
                    "THUMBNAIL_MAX_EDGE",
                    "THUMBNAIL_CACHE_ENABLED",
                    "THUMBNAIL_QUALITY",
                ),
            },
            {
                "label": "天气",
                "hint": "数据源免注册免密钥。关闭总开关后不会向外部服务发起任何请求。",
                "keys": (
                    "WEATHER_ENABLED",
                    "WEATHER_PROVIDER",
                    "WEATHER_LOCATION",
                    "WEATHER_LOCATION_NAME",
                    "WEATHER_CACHE_MINUTES",
                    "DISPLAY_WEATHER_SHOW",
                    "DISPLAY_WEATHER_CORNER",
                ),
            },
            {
                "label": "历史上的今天",
                "hint": "模型筛选策略才会用到信息面板模型，留空则回退分析模型。",
                "keys": (
                    "ONTHISDAY_COUNT",
                    "ONTHISDAY_SOURCE",
                    "ONTHISDAY_STRATEGY",
                    "ONTHISDAY_MIN_YEAR",
                    "PANEL_AI_MODEL",
                ),
            },
        ),
    },
    {
        "id": "render",
        "label": "渲染与设备",
        "icon": "render",
        "summary": "每日墨水屏选片与渲染，以及设备下载地址。改动从下一次渲染生效。",
        "cards": ("device_download",),
        "sections": (
            {
                "label": "每日选片",
                "hint": "历史同日高分照片不足时，可从全局高分照片补足当天画面。",
                "keys": ("MEMORY_THRESHOLD", "DAILY_PHOTO_QUANTITY", "FILL_FROM_GLOBAL"),
            },
            {
                "label": "渲染",
                "hint": "字体留空会把中文渲染成豆腐块且不报错。",
                "keys": ("FONT_PATH",),
            },
        ),
    },
    {
        "id": "worker",
        "label": "上传与任务",
        "icon": "worker",
        "summary": "上传限额与压缩、后台任务重试与租约、回收站保留期。",
        "sections": (
            {
                "label": "上传限额与压缩",
                "hint": "落盘目标体积与长边上限填零表示不压缩、不缩放。",
                "keys": (
                    "UPLOAD_MAX_FILES",
                    "UPLOAD_MAX_BYTES",
                    "UPLOAD_TARGET_BYTES",
                    "UPLOAD_MAX_LONG_EDGE",
                    "UPLOAD_MAX_PIXELS",
                ),
            },
            {
                "label": "后台任务",
                "hint": "续租间隔必须小于租约时长；退避秒数填零会在上游抖动时几秒内烧光尝试次数。",
                "keys": (
                    "JOB_MAX_ATTEMPTS",
                    "JOB_RETRY_BACKOFF_SECONDS",
                    "JOB_LEASE_SECONDS",
                    "JOB_RENEW_SECONDS",
                    "JOB_POLL_SECONDS",
                ),
            },
            {
                "label": "回收站",
                "hint": "过期清理的默认保留天数，实际清理仍需手动或定时触发。",
                "keys": ("TRASH_RETENTION_DAYS",),
            },
        ),
    },
    {
        "id": "system",
        "label": "系统与安全",
        "icon": "system",
        "summary": "部署环境、密钥与会话策略。这些项只能在部署环境修改，此处仅供核对。",
        "cards": (),
        "sections": (
            {
                "label": "运行环境与路径",
                "hint": "监听地址端口与数据库、输出目录只能在部署环境修改，改后需重启进程。",
                "keys": (
                    "APP_ENV",
                    "DB_PATH",
                    "BIN_OUTPUT_DIR",
                    "FLASK_HOST",
                    "FLASK_PORT",
                ),
            },
            {
                "label": "密钥",
                "hint": "缺省时持久化到数据库同目录的隐藏文件；删除后下次启动会生成新值，既有登录会话失效、设备下载地址改变。",
                "keys": ("SECRET_KEY", "DOWNLOAD_KEY"),
            },
            {
                "label": "会话与登录限流",
                "hint": "生产环境强制要求仅安全传输，此时必须通过 HTTPS 访问后台。",
                "keys": (
                    "SESSION_COOKIE_HTTPONLY",
                    "SESSION_COOKIE_SAMESITE",
                    "SESSION_COOKIE_SECURE",
                    "PERMANENT_SESSION_LIFETIME",
                    "WTF_CSRF_TIME_LIMIT",
                    "ADMIN_LOGIN_MAX_FAILURES",
                    "ADMIN_LOGIN_FAILURE_WINDOW_SECONDS",
                ),
            },
        ),
    },
    {
        # 纯记录标签：不含任何配置项，只放审计表，避免它挤在系统与安全项下面。
        "id": "audit",
        "label": "配置审计",
        "icon": "audit",
        "summary": "最近若干次在线配置改动的时间、版本、修改人与逐项变更。只读，不含可保存项。",
        "cards": ("audits",),
        "sections": (),
    },
)


def _settings_tabs(
    items: list[dict[str, Any]], fields: Mapping[str, str]
) -> list[dict[str, Any]]:
    """按标签布局重排配置项，并统计每个标签的可编辑数与校验错误数。

    未在布局中列出的配置项不会被丢弃，而是兜底追加到最后一个带配置项标签的「未分类」段。
    新增配置项时即使忘记登记分类，页面依然能显示并提交，只是位置不理想。

    Args:
        items: `list_admin_settings` 返回的配置项元数据列表。
        fields: 逐配置项的校验错误，用于在标签上标出出错数量。

    Returns:
        可直接渲染的标签列表，每项含分段、可编辑数量与错误数量。
    """
    by_key = {item["key"]: item for item in items}
    placed: set[str] = set()
    tabs: list[dict[str, Any]] = []
    for spec in _SETTINGS_TAB_LAYOUT:
        sections: list[dict[str, Any]] = []
        for section in spec["sections"]:
            entries = [by_key[key] for key in section["keys"] if key in by_key]
            if not entries:
                continue
            placed.update(entry["key"] for entry in entries)
            sections.append(
                {
                    "label": section["label"],
                    "hint": section.get("hint", ""),
                    "entries": entries,
                }
            )
        tabs.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "icon": spec["icon"],
                "summary": spec["summary"],
                "cards": spec.get("cards", ()),
                "sections": sections,
            }
        )
    leftover = [item for item in items if item["key"] not in placed]
    if leftover:
        # 兜底段要落在最后一个带配置项的标签，纯记录标签（配置审计）不收留配置项。
        target = next(tab for tab in reversed(tabs) if tab["sections"])
        target["sections"].append(
            {
                "label": "未分类",
                "hint": "这些配置项尚未登记到分类表，请在 _SETTINGS_TAB_LAYOUT 中补充。",
                "entries": leftover,
            }
        )
    for tab in tabs:
        tab_entries = [
            entry for section in tab["sections"] for entry in section["entries"]
        ]
        tab["count"] = len(tab_entries)
        tab["editable_count"] = sum(
            1
            for entry in tab_entries
            if entry["editable"] and not entry["restart_required"]
        )
        tab["error_count"] = sum(1 for entry in tab_entries if entry["key"] in fields)
    return tabs


def _settings_context(
    *, message: str | None = None, fields: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """构造不回显提交值的配置管理页面上下文。"""
    state = _configuration_service().list_admin_settings()
    field_errors = dict(fields or {})
    return {
        "state": state,
        "tabs": _settings_tabs(state["settings"], field_errors),
        "audits": _configuration_service().list_admin_audit(50),
        "image_dirs": _photo_lifecycle_service().image_directory_status(),
        "device_download_url": url_for(
            "public.esp_latest",
            key=current_app.config["DOWNLOAD_KEY"],
            _external=True,
        ),
        "display_windows": _display_window_context(),
        "message": message,
        "fields": field_errors,
    }


def _render_settings_error(
    message: str, status_code: int, fields: Mapping[str, str] | None = None
):
    """以安全页面展示配置错误，不回显用户提交的配置值。"""
    return render_template(
        "admin/settings.html", **_settings_context(message=message, fields=fields)
    ), status_code


def _parse_settings_form() -> tuple[int, dict[str, Any]]:
    """按注册表类型解析配置表单，只接受当前允许在线编辑的配置。"""
    from src.configuration import ConfigurationValidationError

    errors: dict[str, str] = {}
    try:
        expected_version = int(request.form.get("expected_version", ""))
    except (TypeError, ValueError):
        expected_version = -1
        errors["expected_version"] = "配置版本必须是整数"
    changes: dict[str, Any] = {}
    for key, definition in _configuration_service().registry.items():
        if not definition.editable or definition.restart_required:
            continue
        if definition.sensitive:
            # 敏感项在页面上不回显，留空即表示本次不修改。
            raw_secret = request.form.get(key)
            if raw_secret is None or not raw_secret.strip():
                continue
            changes[key] = raw_secret
            continue
        raw_value = request.form.get(key)
        if raw_value is None:
            errors[key] = "缺少配置值"
            continue
        try:
            if definition.value_type == "integer":
                changes[key] = int(raw_value)
            elif definition.value_type == "float":
                changes[key] = float(raw_value)
            elif definition.value_type == "boolean":
                if raw_value not in ("true", "false"):
                    raise ValueError
                changes[key] = raw_value == "true"
            else:
                changes[key] = raw_value
        except ValueError:
            errors[key] = {
                "integer": "必须是整数",
                "float": "必须是数字",
                "boolean": "必须是布尔值",
            }.get(definition.value_type, "格式无效")
    if errors:
        raise ConfigurationValidationError(errors)
    return expected_version, changes


@admin_page_blueprint.route("/settings", methods=["GET", "POST"])
def settings():
    """展示全部注册配置、最近审计，并处理可编辑配置的原子提交。"""
    from src.configuration import (
        ConfigurationActor,
        ConfigurationConflictError,
        ConfigurationValidationError,
    )

    if request.method == "GET":
        return render_template("admin/settings.html", **_settings_context())
    try:
        expected_version, changes = _parse_settings_form()
        _configuration_service().update_batch(
            changes,
            expected_version,
            ConfigurationActor(int(current_user.id), current_user.username),
        )
    except ConfigurationValidationError as error:
        return _render_settings_error("配置校验失败，请检查标记字段", 400, error.errors)
    except ConfigurationConflictError:
        return _render_settings_error("配置已被其他请求更新，请刷新页面后重试", 409)
    flash("配置已保存")
    return redirect(url_for("admin.settings"))


def _configuration_api_error(
    code: str, message: str, status_code: int, **details: Any
):
    """构造配置接口专用的统一错误 JSON，并附加安全结构化详情。"""
    error = {"code": code, "message": message, **details}
    return jsonify({"status": "error", "error": error}), status_code


@admin_api_blueprint.get("/settings")
def get_settings_api():
    """返回全部配置管理元数据，敏感项仅包含是否已配置。"""
    return jsonify({"status": "ok", "data": _configuration_service().list_admin_settings()})


@admin_api_blueprint.patch("/settings")
def update_settings_api():
    """校验 JSON 配置批次并以当前管理员身份原子提交。"""
    from src.configuration import (
        ConfigurationActor,
        ConfigurationConflictError,
        ConfigurationValidationError,
    )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _configuration_api_error(
            "invalid_parameter", "请求体必须是 JSON 对象", 400
        )
    expected_version = payload.get("expected_version")
    changes = payload.get("changes")
    fields: dict[str, str] = {}
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        fields["expected_version"] = "必须是整数"
    if not isinstance(changes, dict):
        fields["changes"] = "必须是对象"
    if fields:
        return _configuration_api_error(
            "validation_error", "配置校验失败", 400, fields=fields
        )
    try:
        result = _configuration_service().update_batch(
            changes,
            expected_version,
            ConfigurationActor(int(current_user.id), current_user.username),
        )
    except ConfigurationValidationError as error:
        return _configuration_api_error(
            "validation_error", "配置校验失败", 400, fields=error.errors
        )
    except ConfigurationConflictError as error:
        return _configuration_api_error(
            "configuration_conflict",
            "配置版本冲突，请刷新后重试",
            409,
            current_version=error.current_version,
        )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.get("/settings/audit")
def get_settings_audit_api():
    """返回限制为一至一百条的倒序配置审计记录。"""
    try:
        limit = int(request.args.get("limit", "50"))
        records = _configuration_service().list_admin_audit(limit)
    except (TypeError, ValueError):
        return _configuration_api_error(
            "invalid_parameter", "limit 必须是 1 到 100 之间的整数", 400
        )
    return jsonify({"status": "ok", "data": records})


def _positive_integer_argument(name: str, default: int) -> int:
    """读取正整数查询参数，格式错误时返回安全参数错误。"""
    raw_value = request.args.get(name)
    if raw_value in (None, ""):
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ParameterError(f"{name} 必须为正整数") from error
    if value < 1:
        raise ParameterError(f"{name} 必须为正整数")
    return value


def _admin_url(**updates: Any) -> str:
    """保留当前筛选参数并构造后台照片列表链接。"""
    parameters = request.args.to_dict()
    parameters.update({key: value for key, value in updates.items() if value is not None})
    return url_for("admin.photos", **parameters)


def _photo_form(photo: Mapping[str, Any]) -> PhotoEditForm:
    """把照片详情转换为编辑表单初值，不在模板中拼接日期格式。"""
    date_taken = str(photo.get("date_taken") or "")
    if date_taken:
        date_taken = date_taken.replace(":", "-", 2).replace(" ", "T", 1)
    return PhotoEditForm(
        data={
            "version": photo["version"],
            "caption": photo.get("description") or "",
            "side_caption": photo.get("side_caption") or "",
            "memory_score": photo.get("memory_score"),
            "beauty_score": photo.get("beauty_score"),
            "reason": photo.get("reason") or "",
            "exif_city": photo.get("location") or "",
            "category": photo.get("category") or "",
            "date_taken": date_taken,
            "analysis_status": photo.get("analysis_status") or "legacy",
        }
    )


def _edit_form_values(form: PhotoEditForm) -> dict[str, Any]:
    """提取页面允许编辑的字段，历史状态保持原值而不重新写入。"""
    values: dict[str, Any] = {
        "caption": form.caption.data,
        "side_caption": form.side_caption.data,
        "memory_score": form.memory_score.data,
        "beauty_score": form.beauty_score.data,
        "reason": form.reason.data,
        "exif_city": form.exif_city.data,
        "category": form.category.data,
        "date_taken": form.date_taken.data,
    }
    if form.analysis_status.data != "legacy":
        values["analysis_status"] = form.analysis_status.data
    return values


@admin_page_blueprint.before_request
def protect_admin_pages():
    """默认保护后台页面，仅允许登录和首次设置匿名访问。"""
    if request.endpoint in {"admin.login", "admin.setup"}:
        if request.method in _MUTATING_METHODS:
            csrf.protect()
        return None
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if request.method in _MUTATING_METHODS:
        csrf.protect()
    return None


@admin_api_blueprint.before_request
def protect_admin_api():
    """让当前及未来后台接口默认先认证，再对写请求校验跨站请求伪造令牌。"""
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if request.method in _MUTATING_METHODS:
        csrf.protect()
    return None


@admin_page_blueprint.route("/login", methods=["GET", "POST"])
def login():
    """显示登录表单并建立受限的永久管理员会话。"""
    if current_user.is_authenticated:
        return redirect(url_for("admin.index"))

    authentication_service = _authentication_service()
    setup_available = not authentication_service.has_admins()
    form = LoginForm()
    if request.method == "GET":
        form.next.data = request.args.get("next", "")
        return render_template(
            "admin/login.html", form=form, setup_available=setup_available
        )

    next_target = form.next.data
    form_is_valid = form.validate_on_submit()
    if form_is_valid:
        client_ip = request.remote_addr or "unknown"
        admin_user = authentication_service.authenticate(
            form.username.data,
            form.password.data,
            client_ip,
        )
        if admin_user is not None:
            session.clear()
            login_user(admin_user, remember=False, fresh=True)
            session.permanent = True
            if is_safe_next_target(next_target):
                return redirect(next_target)
            return redirect(url_for("admin.index"))

    flash("登录失败，请检查凭据或稍后重试")
    form.password.data = ""
    form.next.data = next_target if is_safe_next_target(next_target) else ""
    return render_template(
        "admin/login.html", form=form, setup_available=setup_available
    ), 401


@admin_page_blueprint.route("/setup", methods=["GET", "POST"])
def setup():
    """仅在管理员表为空时按部署令牌策略处理首次管理员设置。"""
    authentication_service = _authentication_service()
    if authentication_service.has_admins():
        abort(404)

    initial_setup_token_required = authentication_service.initial_setup_token_required
    form = SetupForm(setup_token_required=initial_setup_token_required)
    template_context = {
        "form": form,
        "initial_setup_token_required": initial_setup_token_required,
    }
    if request.method == "GET":
        return render_template("admin/setup.html", **template_context)

    if not form.validate_on_submit():
        message = next(
            (messages[0] for messages in form.errors.values() if messages),
            "请检查首次设置表单",
        )
        flash(message)
        form.password.data = ""
        form.confirm_password.data = ""
        form.setup_token.data = ""
        return render_template("admin/setup.html", **template_context), 400

    try:
        authentication_service.create_first_admin(
            form.username.data,
            form.password.data,
            form.setup_token.data or None,
        )
    except InvalidInitialSetupTokenError:
        flash("初始化令牌无效")
        form.password.data = ""
        form.confirm_password.data = ""
        form.setup_token.data = ""
        return render_template("admin/setup.html", **template_context), 403
    except FirstAdminAlreadyCreatedError:
        abort(404)
    except ValueError as error:
        flash(str(error))
        form.password.data = ""
        form.confirm_password.data = ""
        form.setup_token.data = ""
        return render_template("admin/setup.html", **template_context), 400

    flash("首个管理员已创建，请登录")
    return redirect(url_for("admin.login"))


@admin_page_blueprint.post("/logout")
def logout():
    """销毁当前管理员会话；退出仅允许携带令牌的 POST 请求。"""
    logout_user()
    session.clear()
    return redirect(url_for("admin.login"))


@admin_page_blueprint.get("")
def index():
    """渲染可独立降级统计卡片的后台首页。"""
    service = _admin_photo_service()
    return render_template(
        "admin/index.html",
        statistics=service.dashboard(),
        recent_window_days=service.RECENT_WINDOW_DAYS,
    )


@admin_page_blueprint.get("/photos")
def photos():
    """渲染受白名单约束的照片分页列表和批量操作表单。"""
    result = _admin_photo_service().list_photos(
        page=_positive_integer_argument("page", 1),
        limit=_positive_integer_argument("limit", 24),
        query=request.args.get("query", ""),
        category=request.args.get("category", ""),
        analysis_status=request.args.get("analysis_status", ""),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        sort=request.args.get("sort", "latest"),
        view=request.args.get("view", "grid"),
        missing_date=request.args.get("missing_date") == "1",
    )
    result["urls"] = {
        "previous": _admin_url(page=result["page"] - 1) if result["page"] > 1 else None,
        "next": _admin_url(page=result["page"] + 1) if result["page"] < result["total_pages"] else None,
        "grid": _admin_url(view="grid", page=1),
        "table": _admin_url(view="table", page=1),
    }
    return render_template("admin/photos.html", result=result)


@admin_page_blueprint.post("/photos/batch")
def batch_photos():
    """处理页面批量编辑或移入回收站操作并展示逐项结果。"""
    items: list[dict[str, int]] = []
    for raw_item in request.form.getlist("selected"):
        try:
            photo_id, version = raw_item.split(":", 1)
            items.append({"id": int(photo_id), "version": int(version)})
        except (TypeError, ValueError) as error:
            raise ParameterError("批量照片选择格式无效") from error
    action = (
        "soft_delete"
        if request.form.get("batch_soft_delete") == "1"
        else request.form.get("action")
    )
    if action == "soft_delete":
        result = _photo_lifecycle_service().batch_soft_delete(
            items,
            int(current_user.id),
            current_user.username,
        )
    else:
        result = _admin_photo_management_service().batch_update(
            action,
            items,
            request.form.get("value"),
            current_user.id,
            current_user.username,
        )
    flash(
        f"批量操作完成：成功 {result['success_count']} 项，失败 {result['failure_count']} 项"
    )
    return redirect(url_for("admin.photos"))


@admin_page_blueprint.route("/photos/<int:photo_id>", methods=["GET", "POST"])
def photo_detail(photo_id: int):
    """展示照片详情，并用乐观锁提交受限字段编辑。"""
    photo = _admin_photo_service().detail(photo_id)
    if request.method == "GET":
        return render_template(
            "admin/photo_detail.html", photo=photo, form=_photo_form(photo)
        )

    form = PhotoEditForm()
    if not form.validate_on_submit():
        flash("照片字段校验失败，请检查输入")
        return render_template("admin/photo_detail.html", photo=photo, form=form), 400
    _admin_photo_management_service().update_photo(
        photo_id,
        form.version.data,
        _edit_form_values(form),
        current_user.id,
        current_user.username,
    )
    flash("照片信息已保存")
    return redirect(url_for("admin.photo_detail", photo_id=photo_id))


# 缩略图缓存时长：源文件变化或缩略图配置调整都会改变校验值，因此可以放长一些
_THUMBNAIL_CACHE_SECONDS = 7 * 24 * 3600


def _conditional_thumbnail(path: Any):
    """先比对校验值，命中则直接返回 304，不生成图片。

    与公开接口同一套逻辑：生成一张缩略图要解码四千像素级原图，而校验值只需要
    `stat()` 与两个配置值。
    """
    media = current_app.extensions["inktime_services"]["media"]
    etag = media.thumbnail_etag(path)
    if request.if_none_match.contains_weak(etag.strip('W/"')):
        response = Response(status=304)
    else:
        content = media.render_thumbnail(path)
        response = Response(content.data, mimetype=content.mimetype)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"private, max-age={_THUMBNAIL_CACHE_SECONDS}"
    return response


@admin_page_blueprint.get("/photos/<int:photo_id>/thumbnail")
def admin_photo_thumbnail(photo_id: int):
    """返回仅供已认证管理员查看的活动照片缩略图，并支持条件请求。"""
    return _conditional_thumbnail(_admin_photo_service().admin_thumbnail_path(photo_id))


@admin_page_blueprint.get("/photos/<int:photo_id>/full")
def admin_photo_full(photo_id: int):
    """返回仅供已认证管理员查看的活动照片原图。"""
    content = _admin_photo_service().admin_full_photo(photo_id)
    return send_file(content.path, mimetype=content.mimetype, as_attachment=False)


@admin_page_blueprint.get("/photos/upload")
def upload_photos_page():
    """渲染受认证保护的多文件上传页面，并下发服务端上传限制。"""
    return render_template(
        "admin/upload.html",
        limits={
            "max_files": int(current_app.config["UPLOAD_MAX_FILES"]),
            "max_bytes": int(current_app.config["UPLOAD_MAX_BYTES"]),
        },
    )


def _library_scan_service() -> Any:
    """取得当前应用实例的照片目录扫描服务。"""
    return current_app.extensions["inktime_services"]["library_scan"]


@admin_page_blueprint.post("/photos/scan")
def scan_library():
    """扫描照片目录，登记新照片并排队分析，随后回到照片列表。"""
    result = _library_scan_service().scan(int(current_user.id))
    if result["registered"]:
        message = (
            f"扫描完成：发现 {result['discovered']} 张图片，"
            f"新登记 {result['registered']} 张并已排队分析"
        )
        if result["remaining"]:
            message += f"，仍有 {result['remaining']} 张未登记，请再次扫描"
    elif result["discovered"]:
        message = f"扫描完成：发现 {result['discovered']} 张图片，均已在库，无需新增"
    else:
        message = "扫描完成：照片目录下没有可分析的图片"
    flash(message)
    return redirect(url_for("admin.photos"))


@admin_page_blueprint.get("/jobs")
def jobs():
    """渲染任务类型、状态、进度、重试、错误和关联照片。"""
    return render_template("admin/jobs.html", jobs=_admin_job_service().list_jobs())


@admin_page_blueprint.post("/jobs/<queue>/<int:job_id>/cancel")
def cancel_job(queue: str, job_id: int):
    """按明确队列处理页面任务取消。"""
    _admin_job_service().cancel(queue, job_id, int(current_user.id))
    flash("任务取消请求已提交")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<int:job_id>/cancel")
def cancel_photo_job_legacy(job_id: int):
    """兼容阶段五页面路径并取消照片分析任务。"""
    _admin_job_service().cancel("photo", job_id, int(current_user.id))
    flash("任务取消请求已提交")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<queue>/<int:job_id>/retry")
def retry_job(queue: str, job_id: int):
    """按明确队列处理页面任务重试。"""
    _admin_job_service().retry(queue, job_id, int(current_user.id))
    flash("任务已重新排队")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<int:job_id>/retry")
def retry_photo_job_legacy(job_id: int):
    """兼容阶段五页面路径并重试照片分析任务。"""
    _admin_job_service().retry("photo", job_id, int(current_user.id))
    flash("任务已重新排队")
    return redirect(url_for("admin.jobs"))


@admin_api_blueprint.get("")
def status():
    """返回阶段六照片管理、回收站和维护任务能力状态。"""
    return jsonify(
        {
            "status": "ok",
            "data": {
                "phase": 6,
                "authentication": "implemented",
                "photo_editing": "implemented",
                "batch_operations": "implemented",
                "uploads": "implemented",
                "background_jobs": "implemented",
                "trash": "implemented",
                "restore": "implemented",
                "permanent_delete": "implemented",
                "artifact_blocking": "implemented",
                "username": current_user.username,
            },
        }
    )


@admin_api_blueprint.patch("/photos/<int:photo_id>")
def update_photo_api(photo_id: int):
    """按 JSON 中的预期版本更新单张照片并返回新版本。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    values = dict(payload)
    version = values.pop("version", None)
    result = _admin_photo_management_service().update_photo(
        photo_id,
        version,
        values,
        current_user.id,
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/batch")
def batch_photos_api():
    """批量设置分类或分析状态并返回逐项成功与失败结果。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _admin_photo_management_service().batch_update(
        payload.get("action"),
        payload.get("items"),
        payload.get("value"),
        current_user.id,
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/upload")
def upload_photos():
    """整批校验并原子保存最多十张图片，再创建 pending 分析任务。"""
    try:
        result = _upload_service().upload(request.files.getlist("photos"), int(current_user.id))
    except ValueError as error:
        raise ParameterError(str(error)) from error
    if request.accept_mimetypes.accept_html and not request.is_json:
        counts = result["counts"]
        flash(f"上传完成：接收 {counts['accepted']} 张，重复 {counts['duplicate']} 张")
        return redirect(url_for("admin.jobs"))
    return jsonify({"status": "ok", "data": result}), 201


@admin_api_blueprint.post("/photos/scan-library")
def scan_library_api():
    """扫描照片目录并登记新照片，返回本次统计。"""
    return jsonify({"status": "ok", "data": _library_scan_service().scan(int(current_user.id))}), 202


@admin_api_blueprint.post("/jobs/backfill-content-hash")
def enqueue_content_hash_backfill():
    """安全创建低优先级历史最终文件摘要回填任务，重复任务自动跳过。"""
    payload = request.get_json(silent=True) or {}
    limit = payload.get("limit", 1000) if isinstance(payload, dict) else 1000
    try:
        result = _photo_job_service().enqueue_hash_backfill(int(current_user.id), int(limit))
    except (TypeError, ValueError) as error:
        raise ParameterError("limit 必须是整数") from error
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.get("/jobs")
def list_jobs_api():
    """返回后台任务列表，支持 ETag 条件请求减少轮询带宽。

    摘要必须覆盖前端会渲染的全部字段：只取 status/progress 会导致
    worker 只更新了错误信息或结果摘要时被 304 挡住，页面显示过期内容。
    """
    jobs = _admin_job_service().list_jobs()
    digest_input = json.dumps(
        [
            (
                job["queue"],
                job["id"],
                job["status"],
                job.get("progress", 0),
                job.get("attempts", 0),
                job.get("error_code"),
                job.get("error_summary"),
                job.get("result_summary"),
            )
            for job in jobs
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    etag = f'W/"{hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]}"'
    if request.if_none_match.contains_weak(etag.strip('W/"')):
        return Response(status=304, headers={"ETag": etag})
    response = jsonify({"status": "ok", "data": jobs})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@admin_api_blueprint.post("/jobs/<queue>/<int:job_id>/cancel")
def cancel_job_api(queue: str, job_id: int):
    """按明确队列取消等待任务或请求运行任务协作取消。"""
    state = _admin_job_service().cancel(queue, job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": {"state": state}})


@admin_api_blueprint.post("/jobs/<int:job_id>/cancel")
def cancel_photo_job_api_legacy(job_id: int):
    """兼容阶段五接口路径并取消照片分析任务。"""
    state = _admin_job_service().cancel("photo", job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": {"state": state}})


@admin_api_blueprint.post("/jobs/<queue>/<int:job_id>/retry")
def retry_job_api(queue: str, job_id: int):
    """按明确队列重新排队合法终态任务。"""
    result = _admin_job_service().retry(queue, job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/jobs/<int:job_id>/retry")
def retry_photo_job_api_legacy(job_id: int):
    """兼容阶段五接口路径并重试照片分析任务。"""
    result = _admin_job_service().retry("photo", job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/<int:photo_id>/reanalyze")
def reanalyze_photo_api(photo_id: int):
    """为单张照片排队完整重新分析，不清空旧业务字段。"""
    try:
        result = _photo_job_service().enqueue_analysis([photo_id], int(current_user.id))[0]
    except JobTransitionError as error:
        return _job_mode_conflict_response(error, photo_id, json_only=True)
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.post("/photos/reanalyze")
def reanalyze_photos_api():
    """为最多一百张照片批量排队重新分析。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("photo_ids"), list):
        raise ParameterError("请求体必须包含 photo_ids 数组")
    result = _photo_job_service().enqueue_analysis(payload["photo_ids"], int(current_user.id))
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.post("/photos/<int:photo_id>/regenerate-narration")
def regenerate_narration_api(photo_id: int):
    """为单张照片排队重新生成旁白，失败时保留旧旁白。"""
    try:
        result = _photo_job_service().enqueue_narration(photo_id, int(current_user.id))
    except JobTransitionError as error:
        return _job_mode_conflict_response(error, photo_id, json_only=True)
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.get("/photos/<int:photo_id>/draft")
def photo_draft_api(photo_id: int):
    """返回照片当前版本下最新待确认任务的安全视图。"""
    return jsonify({"status": "ok", "data": _photo_job_service().latest_draft(photo_id)})


@admin_page_blueprint.post("/photos/<int:photo_id>/reanalyze")
def reanalyze_photo(photo_id: int):
    """排队不会直接写照片的完整分析草稿，完成后由管理员确认保存。"""
    try:
        result = _photo_job_service().enqueue_analysis_draft(
            photo_id, int(current_user.id)
        )
    except JobTransitionError as error:
        return _job_mode_conflict_response(error, photo_id)
    return _draft_enqueue_response(photo_id, result, "重新分析")


@admin_page_blueprint.post("/photos/<int:photo_id>/regenerate-narration")
def regenerate_narration(photo_id: int):
    """排队不会直接写照片的旁白草稿，完成后由管理员确认保存。"""
    try:
        result = _photo_job_service().enqueue_narration_draft(
            photo_id, int(current_user.id)
        )
    except JobTransitionError as error:
        return _job_mode_conflict_response(error, photo_id)
    return _draft_enqueue_response(photo_id, result, "重写文案")


@admin_page_blueprint.post("/photos/<int:photo_id>/trash")
def soft_delete_photo(photo_id: int):
    """把活动照片安全移入回收站并触发显示产物重渲染。"""
    _photo_lifecycle_service().soft_delete(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    flash("照片已移入回收站，旧显示产物已屏蔽并等待重渲染")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.get("/trash")
def trash():
    """渲染包含删除快照和操作入口的回收站分页页面。"""
    result = _photo_lifecycle_service().list_trash(
        _positive_integer_argument("page", 1),
        _positive_integer_argument("limit", 24),
    )
    result["previous_url"] = (
        url_for("admin.trash", page=result["page"] - 1, limit=result["limit"])
        if result["page"] > 1
        else None
    )
    result["next_url"] = (
        url_for("admin.trash", page=result["page"] + 1, limit=result["limit"])
        if result["page"] < result["total_pages"]
        else None
    )
    return render_template(
        "admin/trash.html",
        result=result,
        retention_days=_photo_lifecycle_service().retention_days,
    )


@admin_page_blueprint.post("/trash/<int:photo_id>/restore")
def restore_trash_photo(photo_id: int):
    """把回收站文件不覆盖地恢复至删除前位置。"""
    _photo_lifecycle_service().restore(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    flash("照片已恢复，显示产物已屏蔽并等待重渲染")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.route("/trash/<int:photo_id>/purge", methods=["GET", "POST"])
def confirm_purge_photo(photo_id: int):
    """使用独立确认页面和预期版本永久删除回收站照片。"""
    photo = _photo_lifecycle_service().get_trash_photo(photo_id)
    if request.method == "GET":
        return render_template("admin/purge_confirm.html", photo=photo)
    _photo_lifecycle_service().purge(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
        request.form.get("confirmation"),
    )
    flash("照片已永久删除")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.get("/trash/cleanup-preview")
def trash_cleanup_preview():
    """只读预览默认保留期限之前的回收站照片。"""
    preview = _photo_lifecycle_service().cleanup_preview(limit=100)
    return render_template("admin/trash_cleanup.html", preview=preview)


@admin_page_blueprint.post("/trash/cleanup")
def enqueue_trash_cleanup():
    """按明确截止时间和批量大小排队过期回收站清理。"""
    try:
        batch_size = int(request.form.get("batch_size", "100"))
    except (TypeError, ValueError) as error:
        raise ParameterError("batch_size 必须是整数") from error
    result = _photo_lifecycle_service().enqueue_cleanup(
        int(current_user.id),
        current_user.username,
        cutoff=request.form.get("cutoff") or None,
        batch_size=batch_size,
    )
    flash(f"清理任务已排队：维护任务 #{result['id']}")
    return redirect(url_for("admin.jobs"))


@admin_api_blueprint.get("/trash")
def list_trash_api():
    """返回受认证保护的回收站分页数据。"""
    result = _photo_lifecycle_service().list_trash(
        _positive_integer_argument("page", 1),
        _positive_integer_argument("limit", 24),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.delete("/photos/<int:photo_id>")
@admin_api_blueprint.post("/photos/<int:photo_id>/trash")
def soft_delete_photo_api(photo_id: int):
    """按预期版本安全软删除活动照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().soft_delete(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/<int:photo_id>/restore")
@admin_api_blueprint.post("/trash/<int:photo_id>/restore")
def restore_trash_photo_api(photo_id: int):
    """按预期版本安全恢复回收站照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().restore(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.delete("/trash/<int:photo_id>")
@admin_api_blueprint.post("/trash/<int:photo_id>/purge")
def purge_trash_photo_api(photo_id: int):
    """使用确认文本和预期版本永久删除回收站照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().purge(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
        payload.get("confirmation"),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.get("/trash/cleanup-preview")
def trash_cleanup_preview_api():
    """只读返回达到保留期限的稳定编号清理预览。"""
    result = _photo_lifecycle_service().cleanup_preview(
        cutoff=request.args.get("cutoff") or None,
        limit=_positive_integer_argument("limit", 100),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/trash/cleanup")
def enqueue_trash_cleanup_api():
    """排队独立过期回收站维护任务。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    try:
        batch_size = int(payload.get("batch_size", 100))
    except (TypeError, ValueError) as error:
        raise ParameterError("batch_size 必须是整数") from error
    result = _photo_lifecycle_service().enqueue_cleanup(
        int(current_user.id),
        current_user.username,
        cutoff=payload.get("cutoff"),
        batch_size=batch_size,
    )
    return jsonify({"status": "ok", "data": result}), 202
