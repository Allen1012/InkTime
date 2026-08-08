"""Web 服务的数据访问层。"""

from .admin_user_repository import AdminUserRepository, DuplicateAdminUsernameError
from .photo_repository import PhotoRepository

__all__ = ["AdminUserRepository", "DuplicateAdminUsernameError", "PhotoRepository"]
