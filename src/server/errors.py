"""Flask 页面与接口共享的安全错误类型和处理器。"""

from __future__ import annotations

from typing import Tuple

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException


class ApplicationError(Exception):
    """表示可以安全返回给客户端的业务错误。"""

    status_code = 500
    error_code = "server_error"
    default_message = "服务器内部错误"

    def __init__(self, message: str | None = None) -> None:
        """创建业务错误。

        Args:
            message: 可安全展示给客户端的错误说明。
        """
        super().__init__(message or self.default_message)
        self.public_message = message or self.default_message


class ParameterError(ApplicationError):
    """表示请求参数无效。"""

    status_code = 400
    error_code = "invalid_parameter"
    default_message = "请求参数无效"


class AuthenticationRequiredError(ApplicationError):
    """表示请求尚未完成身份认证。"""

    status_code = 401
    error_code = "authentication_required"
    default_message = "需要登录"


class PermissionDeniedError(ApplicationError):
    """表示当前请求没有访问权限。"""

    status_code = 403
    error_code = "permission_denied"
    default_message = "无权访问该资源"


class ResourceNotFoundError(ApplicationError):
    """表示请求的业务资源不存在。"""

    status_code = 404
    error_code = "resource_not_found"
    default_message = "资源不存在"


class ConflictError(ApplicationError):
    """表示请求与当前资源状态冲突。"""

    status_code = 409
    error_code = "conflict"
    default_message = "资源状态冲突"


class ServerError(ApplicationError):
    """表示已识别但不可恢复的服务器错误。"""


def _is_api_request() -> bool:
    """判断当前请求是否应返回 JSON 错误。"""
    return request.path.startswith("/api/")


def _api_error(code: str, message: str, status_code: int) -> Tuple[object, int]:
    """构造不泄露内部信息的统一接口错误响应。"""
    return jsonify({"status": "error", "error": {"code": code, "message": message}}), status_code


def register_error_handlers(app: Flask) -> None:
    """为应用注册统一业务、CSRF、HTTP 与未知异常处理器。

    Args:
        app: 要注册错误处理器的 Flask 应用实例。
    """

    @app.errorhandler(CSRFError)
    def handle_csrf_error(_error: CSRFError):
        """隐藏 CSRF 校验细节，仅返回统一安全错误。"""
        if _is_api_request():
            return _api_error("csrf_failed", "请求校验失败", 400)
        return render_template(
            "error.html", status_code=400, message="请求校验失败，请刷新页面后重试"
        ), 400

    @app.errorhandler(ApplicationError)
    def handle_application_error(error: ApplicationError):
        """按页面或接口格式返回可公开的业务错误。"""
        if _is_api_request():
            return _api_error(error.error_code, error.public_message, error.status_code)
        return render_template(
            "error.html", status_code=error.status_code, message=error.public_message
        ), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        """把框架 HTTP 异常转换为不泄露内部细节的响应。"""
        messages = {
            400: "请求参数无效",
            401: "需要登录",
            403: "无权访问该资源",
            404: "资源不存在",
            405: "请求方法不受支持",
            409: "资源状态冲突",
        }
        message = messages.get(error.code or 500, "请求处理失败")
        if _is_api_request():
            return _api_error(f"http_{error.code}", message, error.code or 500)
        return render_template(
            "error.html", status_code=error.code or 500, message=message
        ), error.code or 500

    @app.errorhandler(Exception)
    def handle_unexpected_error(_error: Exception):
        """记录未知异常上下文并向客户端隐藏内部错误。"""
        app.logger.exception(
            "Unhandled server error, path=[%s], method=[%s]",
            request.path,
            request.method,
        )
        if _is_api_request():
            return _api_error("server_error", "服务器内部错误", 500)
        return render_template(
            "error.html", status_code=500, message="服务器内部错误"
        ), 500
