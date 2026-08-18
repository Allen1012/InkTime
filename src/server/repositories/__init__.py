"""Web 服务的数据访问层。"""

from .admin_user_repository import (
    AdminUserRepository,
    DuplicateAdminUsernameError,
    FirstAdminAlreadyCreatedError,
)
from .photo_management_repository import PhotoManagementRepository
from .photo_repository import PhotoRepository

__all__ = [
    "AdminUserRepository",
    "DuplicateAdminUsernameError",
    "FirstAdminAlreadyCreatedError",
    "PhotoManagementRepository",
    "PhotoRepository",
]
