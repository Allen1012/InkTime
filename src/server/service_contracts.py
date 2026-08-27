"""后台能力的可扩展 Service 接口。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class PhotoServiceContract(Protocol):
    """定义公开照片查询能力，具体实现负责业务序列化。"""

    def list_photos(
        self, page: int, photo_filter: str, sort: str, limit: int
    ) -> Mapping[str, Any]:
        """返回分页照片列表。"""
        ...

    def detail(self, photo_id: int) -> Mapping[str, Any]:
        """返回指定照片详情。"""
        ...


class AdminPhotoServiceContract(Protocol):
    """定义后台统计、筛选列表、详情和受控照片写入能力。"""

    def dashboard(self) -> Mapping[str, Any]:
        """返回可独立降级的首页统计。"""
        ...

    def list_photos(
        self,
        page: int,
        limit: int,
        query: str,
        category: str,
        analysis_status: str,
        date_from: str,
        date_to: str,
        sort: str,
        view: str,
    ) -> Mapping[str, Any]:
        """返回后台照片分页结果和筛选上下文。"""
        ...

    def detail(self, photo_id: int) -> Mapping[str, Any]:
        """返回照片数据库信息、生命周期和文件状态。"""
        ...

    def update_photo(
        self,
        photo_id: int,
        expected_version: int,
        values: Mapping[str, Any],
        admin_user_id: int,
        admin_username: str,
    ) -> Mapping[str, Any]:
        """按预期版本更新单张照片并记录审计。"""
        ...

    def batch_update(
        self,
        items: Sequence[Mapping[str, Any]],
        changes: Mapping[str, Any],
        admin_user_id: int,
        admin_username: str,
    ) -> Mapping[str, Any]:
        """按 changes 里实际给出的字段批量更新并返回逐项结果。"""
        ...


class ConfigServiceContract(Protocol):
    """定义不泄露敏感值的配置读取能力。"""

    def public_settings(self) -> Mapping[str, Any]:
        """返回允许公开展示的配置。"""
        ...


class AuthService(Protocol):
    """定义后台身份恢复、凭据校验和管理员创建能力。"""

    def load_user(self, user_id: str) -> Any | None:
        """按会话主键恢复启用的管理员身份，不存在时返回 None。"""
        ...

    def authenticate(
        self, username: str, password: str, client_ip: str
    ) -> Any | None:
        """校验凭据并应用失败限流，成功时返回最小管理员身份。"""
        ...

    def create_admin(self, username: str, password: str) -> int:
        """校验交互输入并安全创建管理员，返回数据库主键。"""
        ...

    def has_admins(self) -> bool:
        """返回系统是否已经存在管理员。"""
        ...

    def create_first_admin(
        self, username: str, password: str, setup_token: str
    ) -> int:
        """验证一次性令牌并原子创建首个管理员。"""
        ...


class JobService(Protocol):
    """定义后台任务查询与触发能力，当前阶段不提供运行时实现。"""

    def list_jobs(self) -> Sequence[Mapping[str, Any]]:
        """返回可管理任务及其状态。"""
        ...

    def trigger_job(self, job_name: str) -> Mapping[str, Any]:
        """触发指定任务并返回受理结果。"""
        ...


class AuditService(Protocol):
    """定义后台审计记录写入能力，当前阶段不提供运行时实现。"""

    def record(self, action: str, context: Mapping[str, Any]) -> None:
        """记录管理操作及必要上下文。"""
        ...
