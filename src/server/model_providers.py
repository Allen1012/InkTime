"""模型厂商档案的校验、解析与连通性测试服务。

存在的意义：模型接入配置原先只能存一套，接公司的、千问的、豆包的必须互相覆盖。
档案放独立表而不是配置注册表，根本原因是任务快照会断言「快照键集合与注册表非敏感键
精确相等」，往注册表加动态键（API_URL_1、API_URL_2）会让所有历史任务的旧快照立即失效。

本阶段只负责「存下来」：解析与执行链路接入在下一阶段，因此这里的 `resolve` 已经可用，
但还没有调用方。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from src.configuration import IMAGE_DIR_SEPARATOR

from .errors import ConflictError, ParameterError, ResourceNotFoundError
from .repositories.model_provider_repository import ModelProviderRepository


LOGGER = logging.getLogger(__name__)

# 引用厂商名的路由配置键。本阶段这些键还没登记进注册表，因此删除守卫查不到引用；
# 下一阶段登记后守卫自动生效，不需要再改这里。顺序即检查顺序，仅影响报错措辞。
ROUTING_KEYS = ("ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER")
# 厂商名分隔符，与照片目录、展示时间段沿用同一个分号约定：不与 URL 里的冒号冲突，
# 含空格的值也无需转义。因此厂商名本身不允许含分号。
PROVIDER_SEPARATOR = IMAGE_DIR_SEPARATOR
_NAME_MAX_LENGTH = 50
_TEXT_MAX_LENGTH = 500
# 连通性测试的超时上限：档案自身的超时可以配到 600 秒，但后台点一下测试不该把页面
# 挂十分钟。测试只是验证地址与密钥可用，几秒内不通基本就是配错了。
_TEST_TIMEOUT_CEILING = 20
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def resolve_endpoint(base_url: str) -> str:
    """把厂商地址归一化为可直接 POST 的对话补全端点。

    仓库里这个配置值历史上有两种写法：批量脚本把 `API_URL` 当成完整端点直接 POST，
    而 OpenAI 兼容 SDK 分支把它当成 `base_url`。两种写法都在用，因此这里按后缀判断，
    已经指向 `/chat/completions` 的原样使用，否则补上后缀。

    Args:
        base_url: 厂商档案里配置的接口地址。

    Returns:
        可直接发起 POST 的完整端点地址。
    """
    trimmed = str(base_url).strip().rstrip("/")
    if trimmed.endswith(_CHAT_COMPLETIONS_SUFFIX):
        return trimmed
    return f"{trimmed}{_CHAT_COMPLETIONS_SUFFIX}"


class ModelProviderService:
    """校验并维护多套模型接入档案，密钥只写不读。"""

    def __init__(
        self,
        repository: ModelProviderRepository,
        configuration_service: Any | None = None,
    ) -> None:
        """初始化厂商服务。

        Args:
            repository: 厂商档案仓储。
            configuration_service: 可选统一配置服务，用于删除前检查路由键引用。
        """
        self._repository = repository
        self._configuration_service = configuration_service

    # ------------------------------------------------------------------ 读取

    def list_providers(self) -> list[dict[str, Any]]:
        """返回全部档案，不含密钥原值。"""
        return self._repository.list_all()

    def resolve(
        self, name: Any, connection: Any | None = None
    ) -> dict[str, Any] | None:
        """按名称解析启用中的公开档案，可复用任务认领事务连接。

        Args:
            name: 厂商名称，允许为空。
            connection: 可选现有 SQLite 连接；传入后不负责关闭。

        Returns:
            档案公开字段；无法解析时返回空值并由调用方回退旧配置。
        """
        text = str(name or "").strip()
        if not text:
            return None
        provider = self._repository.resolve_enabled(text, connection=connection)
        if provider is None:
            LOGGER.warning(
                "Model provider unavailable, falling back to base settings, name=[%s]",
                text,
            )
        return provider

    def resolve_chain(
        self, raw: Any, connection: Any | None = None
    ) -> list[dict[str, Any]]:
        """把分号分隔的候选串解析为有序公开档案列表。

        Args:
            raw: 分号分隔的厂商名串，允许为空。
            connection: 可选现有 SQLite 连接，用于与任务认领保持同一事务视图。

        Returns:
            按配置顺序排列、去重后的可用档案列表。
        """
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in str(raw or "").split(PROVIDER_SEPARATOR):
            name = candidate.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            provider = self.resolve(name, connection=connection)
            if provider is not None:
                chain.append(provider)
        return chain

    def api_key_for(self, name: Any) -> str:
        """取指定厂商的密钥原值，供执行时现读现传。

        与 `API_KEY` 不进任务快照的既有做法一致：密钥永远不持久化到任务表。
        """
        text = str(name or "").strip()
        return self._repository.secret_for(text) if text else ""

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回厂商审计记录，密钥在写入时已脱敏。"""
        return self._repository.list_audit(limit)

    # ------------------------------------------------------------------ 写入

    def create_provider(
        self, values: Mapping[str, Any], actor_user_id: int | None, actor_username: str
    ) -> dict[str, Any]:
        """校验并新建一条厂商档案。

        Args:
            values: 页面或接口提交的字段。
            actor_user_id: 操作管理员编号。
            actor_username: 操作管理员用户名快照。

        Returns:
            新建档案的公开字段。

        Raises:
            ParameterError: 字段缺失或取值不合法。
            ConflictError: 名称已存在。
        """
        payload = self._normalize(values, require_all=True)
        try:
            return self._repository.create(
                payload, actor_user_id, actor_username, self._timestamp()
            )
        except Exception as error:  # sqlite3.IntegrityError 及其子类
            if "UNIQUE" in str(error).upper():
                raise ConflictError(f"厂商名称已存在: {payload['name']}") from error
            raise

    def update_provider(
        self,
        provider_id: Any,
        expected_version: Any,
        values: Mapping[str, Any],
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        """按乐观锁更新厂商档案。

        刻意**不允许改名**：路由键按名字引用档案，改名要么同步改所有引用、要么留下
        悬空引用。禁止改名让「删除后新建」成为唯一路径，语义更清楚，也不会出现
        「改完名字分析悄悄回退到兜底配置」这种没有任何提示的故障。

        Raises:
            ParameterError: 取值不合法，或试图修改名称。
            ResourceNotFoundError: 档案不存在。
            ConflictError: 版本已变化。
        """
        normalized_id = self._positive_integer(provider_id, "provider_id")
        normalized_version = self._positive_integer(expected_version, "version")
        current = self._repository.get(normalized_id)
        if current is None:
            raise ResourceNotFoundError("厂商档案不存在")
        if "name" in values:
            submitted = str(values["name"] or "").strip()
            if submitted and submitted != current["name"]:
                raise ParameterError(
                    "厂商名称不支持修改，请删除后重新新建；"
                    "名称被分析路由配置按名字引用，改名会留下悬空引用"
                )
        payload = self._normalize(values, require_all=False)
        payload.pop("name", None)
        if not payload:
            raise ParameterError("至少提供一个要修改的字段")
        updated = self._repository.update(
            normalized_id, normalized_version, payload,
            actor_user_id, actor_username, self._timestamp(),
        )
        if updated is None:
            raise ConflictError("厂商档案已被其他操作修改，请刷新后重试")
        return updated

    def delete_provider(
        self,
        provider_id: Any,
        expected_version: Any,
        actor_user_id: int | None,
        actor_username: str,
    ) -> dict[str, Any]:
        """删除厂商档案，被路由配置引用时拒绝。

        Raises:
            ParameterError: 仍被路由配置引用。
            ResourceNotFoundError: 档案不存在。
            ConflictError: 版本已变化。
        """
        normalized_id = self._positive_integer(provider_id, "provider_id")
        normalized_version = self._positive_integer(expected_version, "version")
        current = self._repository.get(normalized_id)
        if current is None:
            raise ResourceNotFoundError("厂商档案不存在")
        referencing = self.referencing_routes(str(current["name"]))
        if referencing:
            raise ParameterError(
                f"厂商仍被以下配置引用，请先改掉再删除: {'、'.join(referencing)}"
            )
        removed = self._repository.delete(
            normalized_id, normalized_version,
            actor_user_id, actor_username, self._timestamp(),
        )
        if removed is None:
            raise ConflictError("厂商档案已被其他操作修改，请刷新后重试")
        return removed

    def referencing_routes(self, name: str) -> list[str]:
        """列出仍引用指定厂商名的路由配置键。

        本阶段路由键尚未登记进注册表，因此恒返回空列表；下一阶段登记后自动生效。
        逐键 try/except KeyError 而不是先查注册表，是为了让两个阶段共用同一份代码。

        Args:
            name: 厂商名称。

        Returns:
            引用该名称的配置键列表。
        """
        if self._configuration_service is None:
            return []
        target = str(name).strip()
        referencing: list[str] = []
        for key in ROUTING_KEYS:
            try:
                raw = self._configuration_service.get(key)
            except KeyError:
                continue
            names = {
                item.strip()
                for item in str(raw or "").split(PROVIDER_SEPARATOR)
                if item.strip()
            }
            if target in names:
                referencing.append(key)
        return referencing

    def import_from_settings(
        self, name: str, actor_user_id: int | None, actor_username: str
    ) -> dict[str, Any]:
        """把当前生效的兜底模型配置导入为一条厂商档案。

        存在的意义是免掉数据迁移：改造上线时表是空的、分析链路继续走注册表里的
        `API_URL` 等兜底键，用户点一次这个动作就完成建档，不需要手抄一遍地址和密钥。

        Raises:
            ParameterError: 兜底配置本身不完整。
            ConflictError: 名称已存在。
        """
        if self._configuration_service is None:
            raise ParameterError("当前实例未注入配置服务，无法导入")
        current = self._configuration_service.get_many(
            ("API_URL", "MODEL_NAME", "API_KEY", "TIMEOUT", "VLM_MAX_LONG_EDGE")
        )
        if not str(current["API_URL"] or "").strip():
            raise ParameterError("当前未配置模型接口地址，无法导入")
        return self.create_provider(
            {
                "name": name,
                "base_url": current["API_URL"],
                "model_name": current["MODEL_NAME"],
                "api_key": current["API_KEY"],
                "timeout_seconds": current["TIMEOUT"],
                "max_long_edge": current["VLM_MAX_LONG_EDGE"],
                "is_enabled": True,
            },
            actor_user_id,
            actor_username,
        )

    # ------------------------------------------------------------ 连通性测试

    def test_connectivity(
        self,
        base_url: Any,
        model_name: Any,
        api_key: Any = "",
        provider_name: Any = None,
    ) -> dict[str, Any]:
        """向厂商发起一次最小对话请求，验证地址与密钥可用。

        存在的意义：不测的话，地址或密钥填错只能等分析任务失败才发现，而那时已经
        排了一批任务、每张都要重试三次。

        密钥留空且给了 `provider_name` 时，取该档案已存的密钥——页面上密钥不回显，
        用户点测试时输入框通常是空的，不这样处理就永远测不了已存档案。

        Args:
            base_url: 接口地址。
            model_name: 模型名。
            api_key: 密钥，留空表示取已存值。
            provider_name: 已存档案名称，用于取已存密钥。

        Returns:
            含 ok、endpoint 与 message 的结果；失败原因不含密钥。
        """
        endpoint = resolve_endpoint(base_url)
        model = str(model_name or "").strip()
        if not model:
            return {"ok": False, "endpoint": endpoint, "message": "未填写模型名"}
        secret = str(api_key or "").strip()
        if not secret and provider_name:
            secret = self.api_key_for(provider_name)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_TEST_TIMEOUT_CEILING
            ) as response:
                status = int(response.status)
        except urllib.error.HTTPError as error:
            # 有些兼容实现会对 max_tokens=1 之类的边界参数报 4xx，但这已经说明
            # 地址可达、鉴权通过，因此把鉴权类与其他 4xx 分开表述。
            if error.code in (401, 403):
                return {
                    "ok": False,
                    "endpoint": endpoint,
                    "message": f"鉴权失败（HTTP {error.code}），请检查密钥",
                }
            return {
                "ok": False,
                "endpoint": endpoint,
                "message": f"接口返回 HTTP {error.code}",
            }
        except urllib.error.URLError as error:
            return {
                "ok": False,
                "endpoint": endpoint,
                "message": f"无法连接：{error.reason}",
            }
        except (TimeoutError, OSError) as error:
            return {"ok": False, "endpoint": endpoint, "message": f"请求失败：{error}"}
        return {
            "ok": True,
            "endpoint": endpoint,
            "message": f"连接成功（HTTP {status}）",
        }

    # ---------------------------------------------------------------- 内部方法

    def _normalize(
        self, values: Mapping[str, Any], *, require_all: bool
    ) -> dict[str, Any]:
        """校验并归一化厂商字段。

        Args:
            values: 待校验的字段映射。
            require_all: 新建时要求必填字段齐全，更新时允许部分提交。

        Returns:
            可直接交给仓储的字段映射；提交了非空密钥时附带 `api_key_hint`。

        Raises:
            ParameterError: 字段缺失或取值不合法。
        """
        unknown = sorted(
            set(values)
            - {
                "name", "base_url", "model_name", "api_key",
                "timeout_seconds", "max_long_edge", "is_enabled",
            }
        )
        if unknown:
            raise ParameterError(f"不支持的厂商字段: {unknown}")
        payload: dict[str, Any] = {}
        if require_all or "name" in values:
            payload["name"] = self._name(values.get("name"))
        if require_all or "base_url" in values:
            payload["base_url"] = self._base_url(values.get("base_url"))
        if require_all or "model_name" in values:
            payload["model_name"] = self._text(
                values.get("model_name"), "模型名", required=True
            )
        if require_all or "timeout_seconds" in values:
            payload["timeout_seconds"] = self._bounded_int(
                values.get("timeout_seconds"), "请求超时秒数", 1, 3600
            )
        if require_all or "max_long_edge" in values:
            payload["max_long_edge"] = self._bounded_int(
                values.get("max_long_edge"), "图片最长边", 256, 8192
            )
        if require_all or "is_enabled" in values:
            payload["is_enabled"] = 1 if self._boolean(values.get("is_enabled")) else 0
        # 密钥留空表示保持原值，与 API_KEY 的既有语义一致：页面不回显，
        # 若把空串当成「清空」，用户改一次超时就会顺手把密钥抹掉。
        if "api_key" in values:
            secret = str(values["api_key"] or "").strip()
            if secret:
                if len(secret) > _TEXT_MAX_LENGTH:
                    raise ParameterError("密钥长度超出限制")
                payload["api_key"] = secret
                payload["api_key_hint"] = secret[-4:]
            elif require_all:
                payload["api_key"] = ""
                payload["api_key_hint"] = ""
        elif require_all:
            payload["api_key"] = ""
            payload["api_key_hint"] = ""
        return payload

    @staticmethod
    def _name(value: Any) -> str:
        """校验厂商名称：非空、限长、不含分号。"""
        text = str(value or "").strip()
        if not text:
            raise ParameterError("厂商名称不能为空")
        if len(text) > _NAME_MAX_LENGTH:
            raise ParameterError(f"厂商名称不能超过 {_NAME_MAX_LENGTH} 个字符")
        if PROVIDER_SEPARATOR in text:
            raise ParameterError(
                f"厂商名称不能包含 {PROVIDER_SEPARATOR}，该字符用于分隔降级候选"
            )
        return text

    @staticmethod
    def _base_url(value: Any) -> str:
        """校验接口地址必须是 HTTP 或 HTTPS 绝对地址。"""
        text = str(value or "").strip()
        if not text:
            raise ParameterError("接口地址不能为空")
        if len(text) > _TEXT_MAX_LENGTH:
            raise ParameterError("接口地址长度超出限制")
        if not text.startswith(("http://", "https://")):
            raise ParameterError("接口地址必须以 http:// 或 https:// 开头")
        return text

    @staticmethod
    def _text(value: Any, label: str, *, required: bool) -> str:
        """校验限长文本字段。"""
        text = str(value or "").strip()
        if required and not text:
            raise ParameterError(f"{label}不能为空")
        if len(text) > _TEXT_MAX_LENGTH:
            raise ParameterError(f"{label}长度超出限制")
        return text

    @staticmethod
    def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
        """校验闭区间内的整数，拒绝布尔值与非数字。"""
        if isinstance(value, bool) or value is None or value == "":
            raise ParameterError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ParameterError(
                f"{label}必须是 {minimum} 到 {maximum} 之间的整数"
            ) from error
        if not minimum <= number <= maximum:
            raise ParameterError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
        return number

    @staticmethod
    def _boolean(value: Any) -> bool:
        """把表单与 JSON 常见的布尔写法归一化。"""
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _positive_integer(value: Any, label: str) -> int:
        """校验正整数入参。"""
        if isinstance(value, bool):
            raise ParameterError(f"{label} 必须是正整数")
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ParameterError(f"{label} 必须是正整数") from error
        if number < 1:
            raise ParameterError(f"{label} 必须是正整数")
        return number

    @staticmethod
    def _timestamp() -> str:
        """返回与其他表一致的协调世界时字符串。"""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
