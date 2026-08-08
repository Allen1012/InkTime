"""管理员认证与阶段三只读后台页面。"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from ..auth import is_safe_next_target
from ..errors import ParameterError
from ..extensions import csrf, login_manager
from ..forms import LoginForm


admin_page_blueprint = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_blueprint = Blueprint("admin_api", __name__, url_prefix="/api/admin")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _authentication_service() -> Any:
    """取得当前应用实例的管理员认证服务。"""
    return current_app.extensions["inktime_services"]["auth"]


def _admin_photo_service() -> Any:
    """取得当前应用实例的后台只读照片服务。"""
    return current_app.extensions["inktime_services"]["admin_photo"]


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


@admin_page_blueprint.before_request
def protect_admin_pages():
    """让当前及未来后台页面默认要求登录，并在写请求上校验 CSRF。"""
    if request.endpoint == "admin.login":
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
    """让当前及未来后台接口默认先认证，再对写请求校验 CSRF。"""
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

    form = LoginForm()
    if request.method == "GET":
        form.next.data = request.args.get("next", "")
        return render_template("admin/login.html", form=form)

    next_target = form.next.data
    form_is_valid = form.validate_on_submit()
    if form_is_valid:
        client_ip = request.remote_addr or "unknown"
        admin_user = _authentication_service().authenticate(
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
    return render_template("admin/login.html", form=form), 401


@admin_page_blueprint.post("/logout")
def logout():
    """销毁当前管理员会话；退出仅允许携带 CSRF token 的 POST 请求。"""
    logout_user()
    session.clear()
    return redirect(url_for("admin.login"))


@admin_page_blueprint.get("")
def index():
    """渲染可独立降级统计卡片的后台首页。"""
    return render_template("admin/index.html", statistics=_admin_photo_service().dashboard())


@admin_page_blueprint.get("/photos")
def photos():
    """渲染受白名单约束的只读照片分页列表。"""
    result = _admin_photo_service().list_photos(
        page=_positive_integer_argument("page", 1),
        limit=_positive_integer_argument("limit", 24),
        query=request.args.get("query", ""),
        category=request.args.get("category", ""),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        sort=request.args.get("sort", "latest"),
        view=request.args.get("view", "grid"),
    )
    result["urls"] = {
        "previous": _admin_url(page=result["page"] - 1) if result["page"] > 1 else None,
        "next": _admin_url(page=result["page"] + 1) if result["page"] < result["total_pages"] else None,
        "grid": _admin_url(view="grid", page=1),
        "table": _admin_url(view="table", page=1),
    }
    return render_template("admin/photos.html", result=result)


@admin_page_blueprint.get("/photos/<int:photo_id>")
def photo_detail(photo_id: int):
    """渲染照片数据库信息和文件状态的只读详情。"""
    return render_template(
        "admin/photo_detail.html", photo=_admin_photo_service().detail(photo_id)
    )


@admin_page_blueprint.get("/jobs")
def jobs():
    """渲染阶段边界明确的任务能力只读说明页。"""
    return render_template("admin/jobs.html")


@admin_api_blueprint.get("")
def status():
    """返回阶段三认证与只读后台状态，不暴露管理员内部字段。"""
    return jsonify(
        {
            "status": "ok",
            "data": {
                "phase": 3,
                "authentication": "implemented",
                "readonly_admin": "implemented",
                "username": current_user.username,
            },
        }
    )
