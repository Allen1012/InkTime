"""管理员身份、认证服务、登录限流和 Flask-Login 接入。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque
from urllib.parse import unquote, urlsplit

import click
from flask import Flask, current_app, jsonify, redirect, request, url_for
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import login_manager
from .repositories.admin_user_repository import (
    AdminUserRepository,
    DuplicateAdminUsernameError,
)


_DUMMY_PASSWORD_HASH = generate_password_hash("inktime-dummy-password-never-valid")


@dataclass(frozen=True)
class AdminUser(UserMixin):
    """提供给 Flask-Login 的最小管理员身份，不暴露密码等内部字段。"""

    id: int
    username: str
    active: bool

    @property
    def is_active(self) -> bool:
        """返回账号是否允许建立和恢复登录会话。"""
        return self.active

    def get_id(self) -> str:
        """返回可安全写入签名会话的稳定管理员主键。"""
        return str(self.id)


class LoginAttemptLimiter:
    """按用户名和客户端地址限制进程内连续失败，所有状态操作均加锁。"""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        """初始化失败窗口。

        Args:
            max_failures: 窗口内允许的最大失败次数。
            window_seconds: 单个连续失败窗口秒数。
        """
        self._max_failures = max(1, max_failures)
        self._window_seconds = max(1, window_seconds)
        self._attempts: dict[tuple[str, str], Deque[float]] = {}
        self._lock = threading.Lock()

    def is_limited(self, username: str, client_ip: str) -> bool:
        """判断当前身份和地址组合是否仍在失败限制窗口内。"""
        key = self._key(username, client_ip)
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(key, now)
            return len(attempts) >= self._max_failures

    def record_failure(self, username: str, client_ip: str) -> bool:
        """记录一次失败并返回记录后是否达到限制阈值。"""
        key = self._key(username, client_ip)
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(key, now)
            attempts.append(now)
            self._attempts[key] = attempts
            return len(attempts) >= self._max_failures

    def clear(self, username: str, client_ip: str) -> None:
        """成功认证后清除该身份和地址组合的连续失败记录。"""
        with self._lock:
            self._attempts.pop(self._key(username, client_ip), None)

    @staticmethod
    def _key(username: str, client_ip: str) -> tuple[str, str]:
        """生成不区分用户名大小写的限流键。"""
        return username.casefold(), client_ip

    def _prune(self, key: tuple[str, str], now: float) -> Deque[float]:
        """删除窗口外记录；调用方必须持有锁。"""
        attempts = self._attempts.get(key, deque())
        cutoff = now - self._window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._attempts.pop(key, None)
        return attempts


class AuthenticationService:
    """编排管理员认证、时序差异缓解、失败限流和登录时间更新。"""

    def __init__(
        self,
        repository: AdminUserRepository,
        max_failures: int,
        window_seconds: int,
    ) -> None:
        """初始化认证服务。

        Args:
            repository: 管理员账号数据仓储。
            max_failures: 连续失败阈值。
            window_seconds: 连续失败统计窗口秒数。
        """
        self._repository = repository
        self._limiter = LoginAttemptLimiter(max_failures, window_seconds)

    def load_user(self, user_id: str) -> AdminUser | None:
        """恢复已签名会话中的启用管理员，非法主键直接视为匿名。

        Args:
            user_id: Flask-Login 会话中的字符串主键。

        Returns:
            启用的管理员身份；不存在、停用或格式错误时返回 None。
        """
        try:
            row = self._repository.find_by_id(int(user_id))
        except (TypeError, ValueError):
            return None
        if row is None or not bool(row["is_active"]):
            return None
        return AdminUser(int(row["id"]), str(row["username"]), True)

    def authenticate(
        self, username: str, password: str, client_ip: str
    ) -> AdminUser | None:
        """统一校验管理员凭据并更新限流与最后登录时间。

        已限流请求在查询账号和计算密码哈希前直接失败；未限流且不存在的用户仍执行
        dummy hash 校验。账号不存在、密码错误、停用和限流均返回 None，避免调用方
        泄露账号状态。

        Args:
            username: 用户提交的管理员用户名。
            password: 用户提交的明文密码，仅在本次校验调用中使用。
            client_ip: 用于进程内失败限流的客户端地址。

        Returns:
            认证成功的最小管理员身份；失败时返回 None。
        """
        normalized_username = username.strip()
        if self._limiter.is_limited(normalized_username, client_ip):
            current_app.logger.warning(
                "Admin login rate limited, username=[%s], client_ip=[%s]",
                normalized_username,
                client_ip,
            )
            return None

        row = self._repository.find_by_username(normalized_username)
        candidate_hash = str(row["password_hash"]) if row is not None else _DUMMY_PASSWORD_HASH
        try:
            password_matches = check_password_hash(candidate_hash, password)
        except (TypeError, ValueError):
            password_matches = False

        active = row is not None and bool(row["is_active"])
        if row is None or not password_matches or not active:
            limited = self._limiter.record_failure(normalized_username, client_ip)
            message = "Admin login failure threshold reached" if limited else "Admin login failed"
            current_app.logger.warning(
                "%s, username=[%s], client_ip=[%s]",
                message,
                normalized_username,
                client_ip,
            )
            return None

        admin_user = AdminUser(int(row["id"]), str(row["username"]), True)
        self._limiter.clear(normalized_username, client_ip)
        self._repository.update_last_login(admin_user.id)
        return admin_user

    def create_admin(self, username: str, password: str) -> int:
        """校验交互输入并创建管理员，不记录或输出密码与哈希。

        Args:
            username: 交互输入的用户名。
            password: 已通过两次确认的明文密码。

        Returns:
            新管理员的数据库主键。

        Raises:
            ValueError: 用户名为空或密码少于 12 个字符。
            DuplicateAdminUsernameError: 大小写不敏感用户名已存在。
        """
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("用户名不能为空")
        if len(normalized_username) > 128:
            raise ValueError("用户名不能超过 128 个字符")
        if len(password) < 12:
            raise ValueError("密码至少需要 12 个字符")
        return self._repository.create(
            normalized_username,
            generate_password_hash(password),
        )


def is_safe_next_target(target: str | None) -> bool:
    """仅允许以单个斜杠开头且不含站外结构的相对跳转目标。"""
    if not target:
        return False
    decoded = unquote(target)
    if not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded:
        return False
    parsed = urlsplit(decoded)
    return not parsed.scheme and not parsed.netloc


def register_authentication(app: Flask) -> None:
    """为应用注册用户恢复、未认证响应和安全的创建管理员命令。

    Args:
        app: 已注册认证服务的 Flask 应用实例。
    """

    @login_manager.user_loader
    def load_user(user_id: str):
        """通过当前应用的认证服务恢复管理员会话。"""
        return current_app.extensions["inktime_services"]["auth"].load_user(user_id)

    @login_manager.unauthorized_handler
    def unauthorized():
        """区分后台接口的统一 JSON 401 与后台页面登录跳转。"""
        if request.path.startswith("/api/admin"):
            return jsonify(
                {
                    "status": "error",
                    "error": {
                        "code": "authentication_required",
                        "message": "需要登录",
                    },
                }
            ), 401
        target = request.full_path.rstrip("?")
        return redirect(url_for("admin.login", next=target))

    @app.cli.command("create-admin")
    def create_admin_command() -> None:
        """交互创建管理员，密码隐藏输入并由 Click 执行二次确认。"""
        username = click.prompt("管理员用户名", type=str)
        password = click.prompt(
            "管理员密码",
            hide_input=True,
            confirmation_prompt="再次输入管理员密码",
        )
        service = current_app.extensions["inktime_services"]["auth"]
        try:
            service.create_admin(username, password)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        except DuplicateAdminUsernameError as error:
            raise click.ClickException("管理员用户名已存在") from error
        click.echo(f"管理员 {username.strip()} 已创建")
