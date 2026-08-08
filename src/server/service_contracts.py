"""阶段二后台能力的可扩展 Service 接口。"""

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


class ConfigServiceContract(Protocol):
    """定义不泄露敏感值的配置读取能力。"""

    def public_settings(self) -> Mapping[str, Any]:
        """返回允许公开展示的配置。"""
        ...


class AuthService(Protocol):
    """定义后台身份认证能力，阶段一不提供运行时实现。"""

    def authenticate(self, credentials: Mapping[str, str]) -> Mapping[str, Any] | None:
        """校验凭据并返回身份信息，失败时返回 None。"""
        ...


class JobService(Protocol):
    """定义后台任务查询与触发能力，阶段一不提供运行时实现。"""

    def list_jobs(self) -> Sequence[Mapping[str, Any]]:
        """返回可管理任务及其状态。"""
        ...

    def trigger_job(self, job_name: str) -> Mapping[str, Any]:
        """触发指定任务并返回受理结果。"""
        ...


class AuditService(Protocol):
    """定义后台审计记录写入能力，阶段一不提供运行时实现。"""

    def record(self, action: str, context: Mapping[str, Any]) -> None:
        """记录管理操作及必要上下文。"""
        ...
