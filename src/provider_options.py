"""厂商特有请求参数的解析与合并。

单独成模块而不是放进 `src/server/model_providers.py`，是因为批量分析脚本
`src/analysis/analyze_photos_docker.py` 也要用它来合并请求体，而 analysis 侧不应
依赖 server 层（那会把 Flask 拖进纯命令行入口）。这里两个函数都是纯函数，不碰数据库、
不碰配置注册表，因此两侧都能安全导入。

为什么需要「厂商特有参数」这个概念：同一个 OpenAI 兼容协议，各家的扩展参数并不通用。
`enable_thinking` 只有千问认，硬传给别家可能直接 400；而 `response_format` 在千问上
反而让正文变成 `[{"caption": ...}]`。这类差异只能按厂商配，不能全局写死。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping


LOGGER = logging.getLogger(__name__)


def parse_request_options(raw: Any) -> dict[str, Any]:
    """把档案里的高级请求参数解析为字典，取值不可用时返回空字典。

    读取路径刻意宽容：这一列是自由格式的 JSON，若因手工改库等原因存进了非法内容，
    宁可当作「没有额外参数」按默认行为发请求，也不要让整条分析链路失效。写入路径
    由服务层严格校验，因此正常途径存不进非法值。

    Args:
        raw: 档案里的 `request_options` 取值，允许为空、字符串或已解析的映射。

    Returns:
        参数字典；无有效内容时返回空字典。
    """
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        LOGGER.warning("Provider request options is not valid JSON, ignoring it")
        return {}
    if not isinstance(parsed, dict):
        LOGGER.warning("Provider request options is not a JSON object, ignoring it")
        return {}
    return {str(key): value for key, value in parsed.items()}


def apply_request_options(
    payload: dict[str, Any], options: Mapping[str, Any] | None
) -> dict[str, Any]:
    """把厂商的额外参数合并进请求体，值为空表示删掉该参数。

    需要「删掉」这个语义是因为有的参数只能通过不发送来关闭：千问关掉思考后正文本身
    就是干净的一句话，`response_format` 反而让它输出 `[{"caption": ...}]`，而
    OpenAI 兼容层没有「结构化输出=关」这种取值，只能整个参数不发。

    Args:
        payload: 已构造好的请求体，就地修改并返回。
        options: 厂商档案里的额外参数，允许为空。

    Returns:
        合并后的请求体。
    """
    for key, value in (options or {}).items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload
