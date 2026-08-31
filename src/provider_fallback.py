"""定义跨照片分析与信息面板共用的厂商降级错误语义。"""

from __future__ import annotations

import socket
import urllib.error
from typing import Any

import requests

try:
    import openai
except ImportError:  # pragma: no cover - 部署可选择仅使用 requests
    openai = None

CONNECTION_ERROR = "connection_error"
TIMEOUT = "timeout"
HTTP_429 = "http_429"
HTTP_5XX = "http_5xx"
FALLBACK_REASONS = frozenset({CONNECTION_ERROR, TIMEOUT, HTTP_429, HTTP_5XX})


class ProviderTransportError(RuntimeError):
    """携带稳定可降级原因，不暴露上游响应正文或异常消息。"""

    def __init__(self, reason: str, status_code: int | None = None) -> None:
        """保存白名单原因与可选状态码。

        Args:
            reason: 四种可降级原因之一。
            status_code: 可选 HTTP 状态码。
        """
        if reason not in FALLBACK_REASONS:
            raise ValueError("unsupported_provider_fallback_reason")
        self.reason = reason
        self.status_code = status_code
        detail = f", status=[{status_code}]" if status_code is not None else ""
        super().__init__(f"provider request failed, reason=[{reason}]{detail}")


class ProviderHTTPError(RuntimeError):
    """表示不可降级的稳定 HTTP 状态错误，不携带响应正文。"""

    def __init__(self, status_code: int) -> None:
        """保存 HTTP 状态码并生成稳定错误文本。

        Args:
            status_code: 上游返回的 HTTP 状态码。
        """
        self.status_code = int(status_code)
        super().__init__(f"provider request failed, status=[{self.status_code}]")


def _openai_type(name: str) -> tuple[type[BaseException], ...]:
    """安全取得可选 OpenAI 软件开发工具包异常类型。"""
    candidate = getattr(openai, name, None) if openai is not None else None
    return (candidate,) if isinstance(candidate, type) else ()


def provider_status_code(error: BaseException) -> int | None:
    """从结构化异常属性读取 HTTP 状态码，不解析异常消息文本。

    Args:
        error: 待分类异常。

    Returns:
        可用的 HTTP 状态码；异常不携带状态时返回空值。
    """
    if isinstance(error, ProviderHTTPError):
        return error.status_code
    if isinstance(error, ProviderTransportError):
        return error.status_code
    if isinstance(error, urllib.error.HTTPError):
        return int(error.code)
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    response: Any = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int) and not isinstance(response_status, bool):
        return response_status
    return None


def fallback_reason(error: BaseException) -> str | None:
    """仅按异常类型和结构化状态码识别允许跨厂商降级的原因。

    HTTPError 必须先于 URLError 判断，因为前者继承后者。响应解析、字段缺失、
    模型正文与业务内容错误均不在白名单内。

    Args:
        error: 模型调用抛出的原始或稳定包装异常。

    Returns:
        四种稳定原因之一；不可降级时返回空值。
    """
    if isinstance(error, ProviderTransportError):
        return error.reason
    if isinstance(error, urllib.error.HTTPError):
        status = int(error.code)
        if status == 429:
            return HTTP_429
        if 500 <= status <= 599:
            return HTTP_5XX
        return None
    if isinstance(error, urllib.error.URLError):
        return TIMEOUT if isinstance(error.reason, (TimeoutError, socket.timeout)) else CONNECTION_ERROR
    if isinstance(error, (requests.exceptions.Timeout, TimeoutError, socket.timeout)):
        return TIMEOUT
    if isinstance(error, requests.exceptions.ConnectionError):
        return CONNECTION_ERROR
    if isinstance(error, requests.exceptions.HTTPError):
        status = provider_status_code(error)
        if status == 429:
            return HTTP_429
        if status is not None and 500 <= status <= 599:
            return HTTP_5XX
        return None
    if _openai_type("APITimeoutError") and isinstance(error, _openai_type("APITimeoutError")):
        return TIMEOUT
    if _openai_type("APIConnectionError") and isinstance(error, _openai_type("APIConnectionError")):
        return CONNECTION_ERROR
    status = provider_status_code(error)
    if status == 429:
        return HTTP_429
    if status is not None and 500 <= status <= 599:
        return HTTP_5XX
    return None


def sanitized_provider_error(error: BaseException) -> BaseException:
    """把网络和 HTTP 异常转换成不含响应正文的稳定异常。

    Args:
        error: 上游客户端抛出的异常。

    Returns:
        可降级网络错误、稳定 HTTP 错误，或原始非网络异常。
    """
    reason = fallback_reason(error)
    status = provider_status_code(error)
    if reason is not None:
        return ProviderTransportError(reason, status)
    if status is not None:
        return ProviderHTTPError(status)
    return error
