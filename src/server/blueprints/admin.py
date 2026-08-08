"""阶段一后台页面与接口 Blueprint 骨架。"""

from flask import Blueprint, jsonify, render_template


admin_page_blueprint = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_blueprint = Blueprint("admin_api", __name__, url_prefix="/api/admin")


@admin_page_blueprint.get("")
def index():
    """渲染阶段一空后台首页，不引入阶段二认证行为。"""
    return render_template("admin/index.html")


@admin_api_blueprint.get("")
def status():
    """返回后台接口骨架状态，并与公开设置模拟接口保持隔离。"""
    return jsonify({"status": "ok", "data": {"phase": 1, "authentication": "not_implemented"}})
