"""Flask Blueprint 注册入口。"""

from .admin import admin_api_blueprint, admin_page_blueprint
from .public import public_blueprint

__all__ = ["admin_api_blueprint", "admin_page_blueprint", "public_blueprint"]
