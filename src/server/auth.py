"""管理员身份、认证服务、数据库共享登录限流和 Flask-Login 接入。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

import click
from flask import Flask, current_app, jsonify, redirect, request, url_for
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from src.database import database_connection, write_transaction

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
    """使用 SQLite 失败事件实现跨进程共享的登录滑动窗口。"""

    def __init__(
        self,
        database_path: str | Path,
        secret_key: str,
        max_failures: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化数据库限流器并捕获不可逆派生键所需的应用密钥。

        Args:
            database_path: 存储登录失败事件的 SQLite 数据库路径。
            secret_key: 仅用于派生匿名 attempt_key 的应用签名密钥。
            max_failures: 窗口内允许的最大失败次数。
            window_seconds: 单个连续失败窗口秒数。
            clock: 可选的 Unix 时间提供器，供隔离验证推进时间。
        """
        self._database_path = Path(database_path).expanduser().resolve()
        self._secret_key = secret_key.encode("utf-8")
        self._max_failures = max(1, max_failures)
        self._window_seconds = max(1, window_seconds)
        self._clock = clock or time.time

    @staticmethod
    def normalize_client_ip(client_ip: str | None) -> str:
        """规范化直接连接地址；非法或空地址统一映射为 unknown。"""
        try:
            return ipaddress.ip_address(str(client_ip or "").strip()).compressed
        except ValueError:
            return "unknown"

    def _attempt_key(self, username: str, client_ip: str | None) -> str:
        """用 HMAC-SHA256 派生不暴露用户名和客户端地址的稳定组合键。"""
        identity = (
            username.strip().casefold()
            + "\0"
            + self.normalize_client_ip(client_ip)
        ).encode("utf-8")
        return hmac.new(self._secret_key, identity, hashlib.sha256).hexdigest()

    def is_limited(self, username: str, client_ip: str | None) -> bool:
        """只读统计当前组合窗口内事件，判断是否达到失败阈值。

        Args:
            username: 用户提交的管理员用户名。
            client_ip: Flask 提供的直接连接客户端地址，不读取转发头。

        Returns:
            窗口内失败次数达到阈值时返回 True。
        """
        cutoff = int(self._clock()) - self._window_seconds
        attempt_key = self._attempt_key(username, client_ip)
        with database_connection(self._database_path, read_only=True) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM admin_login_failures "
                "WHERE attempt_key=? AND failed_at_epoch>?",
                (attempt_key, cutoff),
            ).fetchone()[0]
        return int(count) >= self._max_failures

    def record_failure(self, username: str, client_ip: str | None) -> bool:
        """短事务记录失败、清理过期事件并返回记录后的限流状态。

        当前组合的过期事件先全部删除；全局清理每次最多删除 256 条，避免单次登录
        长时间持有写锁。随机 nonce 允许同一秒内并发失败分别落库。

        Args:
            username: 用户提交的管理员用户名。
            client_ip: Flask 提供的直接连接客户端地址。

        Returns:
            插入本次失败后窗口内事件数达到阈值时返回 True。
        """
        failed_at_epoch = int(self._clock())
        cutoff = failed_at_epoch - self._window_seconds
        attempt_key = self._attempt_key(username, client_ip)
        with write_transaction(self._database_path) as connection:
            connection.execute(
                "DELETE FROM admin_login_failures "
                "WHERE attempt_key=? AND failed_at_epoch<=?",
                (attempt_key, cutoff),
            )
            connection.execute(
                "DELETE FROM admin_login_failures WHERE rowid IN ("
                "SELECT rowid FROM admin_login_failures "
                "WHERE failed_at_epoch<=? ORDER BY failed_at_epoch LIMIT 256)",
                (cutoff,),
            )
            connection.execute(
                "INSERT INTO admin_login_failures"
                "(attempt_key,failed_at_epoch,attempt_nonce) VALUES (?,?,?)",
                (attempt_key, failed_at_epoch, secrets.token_hex(16)),
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM admin_login_failures "
                "WHERE attempt_key=? AND failed_at_epoch>?",
                (attempt_key, cutoff),
            ).fetchone()[0]
        return int(count) >= self._max_failures

    def complete_successful_login(
        self, admin_user_id: int, username: str, client_ip: str | None
    ) -> None:
        """同一短事务清除当前组合失败事件并更新管理员登录时间。

        密码哈希校验和管理员查询必须在调用本方法前完成，不能占用写事务。

        Args:
            admin_user_id: 已通过密码和启用状态校验的管理员主键。
            username: 本次提交并已去除首尾空白的用户名。
            client_ip: Flask 提供的直接连接客户端地址。
        """
        now = int(self._clock())
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
        attempt_key = self._attempt_key(username, client_ip)
        with write_transaction(self._database_path) as connection:
            connection.execute(
                "DELETE FROM admin_login_failures WHERE attempt_key=?",
                (attempt_key,),
            )
            cursor = connection.execute(
                "UPDATE admin_users SET last_login_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, admin_user_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("成功登录的管理员记录不存在")


class AuthenticationService:
    """编排管理员认证、时序差异缓解、共享限流和原子登录完成。"""

    def __init__(
        self,
        repository: AdminUserRepository,
        database_path: str | Path,
        secret_key: str,
        max_failures: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化认证服务及数据库共享失败窗口。

        Args:
            repository: 管理员账号只读查询与创建仓储。
            database_path: 登录失败事件和管理员账号所在数据库路径。
            secret_key: 派生匿名失败组合键的应用签名密钥。
            max_failures: 连续失败阈值。
            window_seconds: 连续失败统计窗口秒数。
            clock: 可选 Unix 时间提供器，供隔离验证使用。
        """
        self._repository = repository
        self._limiter = LoginAttemptLimiter(
            database_path, secret_key, max_failures, window_seconds, clock
        )

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
        """统一校验管理员凭据并更新共享限流与最后登录时间。

        已限流请求在查询账号和计算密码哈希前直接失败；未限流且不存在的用户仍执行
        dummy hash 校验。账号不存在、密码错误、停用和限流均返回 None，避免调用方
        泄露账号状态。成功时清理当前用户名与地址组合的失败事件，并与最后登录时间
        更新处于同一短数据库事务。

        Args:
            username: 用户提交的管理员用户名。
            password: 用户提交的明文密码，仅在本次校验调用中使用。
            client_ip: 直接连接客户端地址；非法或空值统一按 unknown 处理。

        Returns:
            认证成功的最小管理员身份；失败时返回 None。
        """
        normalized_username = username.strip()
        normalized_client_ip = self._limiter.normalize_client_ip(client_ip)
        if self._limiter.is_limited(normalized_username, normalized_client_ip):
            current_app.logger.warning(
                "Admin login rate limited, username=[%s], client_ip=[%s]",
                normalized_username,
                normalized_client_ip,
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
            limited = self._limiter.record_failure(
                normalized_username, normalized_client_ip
            )
            message = "Admin login failure threshold reached" if limited else "Admin login failed"
            current_app.logger.warning(
                "%s, username=[%s], client_ip=[%s]",
                message,
                normalized_username,
                normalized_client_ip,
            )
            return None

        admin_user = AdminUser(int(row["id"]), str(row["username"]), True)
        self._limiter.complete_successful_login(
            admin_user.id, normalized_username, normalized_client_ip
        )
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
