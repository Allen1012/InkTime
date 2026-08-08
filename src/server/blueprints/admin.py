"""阶段二后台登录、退出、页面与接口统一保护。"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from ..auth import is_safe_next_target
from ..extensions import csrf, login_manager
from ..forms import LoginForm


admin_page_blueprint = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_blueprint = Blueprint("admin_api", __name__, url_prefix="/api/admin")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _authentication_service() -> Any:
    """取得当前应用实例的管理员认证服务。"""
    return current_app.extensions["inktime_services"]["auth"]


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
    """渲染已认证管理员的阶段二后台首页。"""
    return render_template("admin/index.html")


@admin_api_blueprint.get("")
def status():
    """返回阶段二认证状态和当前用户名，不暴露管理员内部字段。"""
    return jsonify(
        {
            "status": "ok",
            "data": {
                "phase": 2,
                "authentication": "implemented",
                "username": current_user.username,
            },
        }
    )
