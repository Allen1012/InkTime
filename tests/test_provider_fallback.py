"""验证阶段三厂商候选链只对明确网络错误执行单向降级。"""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
import unittest
from pathlib import Path
from typing import Any

import requests

from src.analysis import analyze_photos_docker as legacy
from src.analysis import photo_analyzer
from src.provider_fallback import (
    CONNECTION_ERROR,
    HTTP_429,
    HTTP_5XX,
    TIMEOUT,
    ProviderTransportError,
    fallback_reason,
)
from src.server import panel
from src.server.admin_jobs import AnalysisWorker


def _http_error(status: int) -> urllib.error.HTTPError:
    """构造不含响应正文的 urllib HTTP 错误。"""
    return urllib.error.HTTPError("https://provider.invalid", status, "failed", {}, io.BytesIO())


def _provider(name: str, model: str, edge: int = 1024) -> dict[str, Any]:
    """构造照片适配层使用的最小运行时厂商档案。"""
    return {
        "id": 1 if name == "主厂商" else 2,
        "name": name,
        "version": 1,
        "base_url": f"https://{model}.example.com/v1",
        "model_name": model,
        "timeout_seconds": 30,
        "max_long_edge": edge,
        "api_key": f"key-{model}",
    }


def _settings() -> dict[str, Any]:
    """构造照片适配层临时旧模块配置所需的完整设置。"""
    return {
        "API_URL": "https://legacy.example.com/v1/chat/completions",
        "MODEL_NAME": "legacy",
        "TIMEOUT": 30,
        "VLM_MAX_LONG_EDGE": 1024,
        "WORLD_CITIES_CSV": "",
        "CITY_GRID_DEG": 0.25,
        "CITY_MAX_DISTANCE_KM": 100,
        "HOME_LAT": 0.0,
        "HOME_LON": 0.0,
        "HOME_RADIUS_KM": 1.0,
    }


class ProviderFallbackClassificationTestCase(unittest.TestCase):
    """验证可降级白名单只依赖异常类型和结构化状态码。"""

    def test_network_and_retryable_http_errors_are_fallbackable(self) -> None:
        """连接、超时、HTTP 429 与 HTTP 5xx 返回稳定降级原因。"""
        self.assertEqual(CONNECTION_ERROR, fallback_reason(requests.ConnectionError()))
        self.assertEqual(TIMEOUT, fallback_reason(requests.Timeout()))
        self.assertEqual(HTTP_429, fallback_reason(_http_error(429)))
        self.assertEqual(HTTP_5XX, fallback_reason(_http_error(500)))

    def test_client_and_content_errors_are_not_fallbackable(self) -> None:
        """其他 HTTP 4xx、响应和模型内容错误不得切换厂商。"""
        for error in (
            _http_error(400),
            _http_error(401),
            json.JSONDecodeError("bad", "{", 0),
            KeyError("choices"),
            ValueError("body json invalid"),
            photo_analyzer.NarrationGenerationError("content invalid"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(fallback_reason(error))


class OpenAIRequestBoundaryTestCase(unittest.TestCase):
    """验证 OpenAI 软件开发工具包失败后不会对同厂商再发 requests 请求。"""

    def test_openai_failure_does_not_resend_with_requests(self) -> None:
        """OpenAI 网络失败直接上抛稳定异常，requests 调用次数保持零。"""
        original = {
            name: getattr(legacy, name)
            for name in ("OpenAI", "API_URL", "API_BASE_URL", "API_KEY", "read_exif")
        }
        original_post = legacy.requests.post
        self.addCleanup(lambda: [setattr(legacy, key, value) for key, value in original.items()])
        self.addCleanup(setattr, legacy.requests, "post", original_post)
        calls = {"requests": 0}

        class _Completions:
            """模拟一次 OpenAI 网络失败。"""

            @staticmethod
            def create(**_kwargs: Any) -> Any:
                """抛出结构化连接异常。"""
                raise requests.ConnectionError()

        class _Client:
            """提供 OpenAI 客户端所需的 chat.completions 属性。"""

            def __init__(self, **_kwargs: Any) -> None:
                """初始化最小客户端结构。"""
                self.chat = type("Chat", (), {"completions": _Completions()})()

        def post(*_args: Any, **_kwargs: Any) -> Any:
            """记录不应发生的 requests 重发。"""
            calls["requests"] += 1
            raise AssertionError("requests should not be called")

        legacy.OpenAI = _Client
        legacy.API_URL = "https://dashscope.example.com/v1/chat/completions"
        legacy.API_BASE_URL = "https://dashscope.example.com/v1"
        legacy.API_KEY = "secret"
        legacy.read_exif = lambda _path: {}
        legacy.requests.post = post

        with self.assertRaises(ProviderTransportError):
            legacy.call_vlm(Path("/tmp/sample.jpg"), "encoded")
        self.assertEqual(0, calls["requests"])


class PhotoProviderChainTestCase(unittest.TestCase):
    """验证评分和旁白按用途独立遍历并正确复用图片编码。"""

    def setUp(self) -> None:
        """替换模型和图片函数，并在用例结束后恢复。"""
        for name in (
            "encode_image_to_b64", "call_vlm", "generate_side_caption",
            "resolve_datetime", "resolve_city_index_path",
        ):
            self.addCleanup(setattr, legacy, name, getattr(legacy, name))
        legacy.resolve_city_index_path = lambda _value: Path("/tmp/cities.csv")
        legacy.resolve_datetime = lambda _path, _value, original_filename=None: (None, "none")

    def test_scoring_succeeds_once_and_narration_falls_back_once(self) -> None:
        """旁白主厂商网络失败后切备用，不重复已成功评分并记录一次转向。"""
        calls = {"score": 0, "narration": 0, "encode": []}
        events: list[tuple[str, str, str, str]] = []

        def encode(_path: Path) -> str:
            """记录当前厂商模型与图片边长。"""
            calls["encode"].append((legacy.MODEL_NAME, legacy.VLM_MAX_LONG_EDGE))
            return f"{legacy.MODEL_NAME}:{legacy.VLM_MAX_LONG_EDGE}"

        def score(_path: Path, _image_b64: str) -> tuple[dict[str, Any], dict[str, Any]]:
            """评分始终成功并记录调用次数。"""
            calls["score"] += 1
            return ({"caption": "描述", "type": "日常", "memory_score": 70,
                     "beauty_score": 60, "reason": "理由"}, {"datetime": None})

        def narration(_path: Path, _image_b64: str) -> str:
            """主厂商连接失败，备用厂商成功。"""
            calls["narration"] += 1
            if legacy.MODEL_NAME == "primary":
                raise requests.ConnectionError()
            return "备用旁白"

        def fallback(purpose: str, reason: str, current: dict, following: dict) -> None:
            """记录实际候选转向。"""
            events.append((purpose, reason, current["name"], following["name"]))

        legacy.encode_image_to_b64 = encode
        legacy.call_vlm = score
        legacy.generate_side_caption = narration
        primary = _provider("主厂商", "primary", 1024)
        backup = _provider("备用厂商", "backup", 2048)

        result = photo_analyzer.analyze_single_photo(
            Path("/tmp/sample.jpg"),
            settings=_settings(),
            provider_chain=[primary],
            narration_provider_chain=[primary, backup],
            on_provider_fallback=fallback,
            city_resolver=lambda _lat, _lon: "",
        )

        self.assertEqual("备用旁白", result["side_caption"])
        self.assertEqual(1, calls["score"])
        self.assertEqual(2, calls["narration"])
        self.assertEqual([("primary", 1024), ("backup", 2048)], calls["encode"])
        self.assertEqual([("narration", CONNECTION_ERROR, "主厂商", "备用厂商")], events)

    def test_all_retryable_reasons_advance_to_backup(self) -> None:
        """连接、超时、HTTP 429 和 HTTP 5xx 都只向后尝试一次备用厂商。"""
        primary = _provider("主厂商", "primary", 1024)
        backup = _provider("备用厂商", "backup", 2048)
        cases = (
            requests.ConnectionError(),
            requests.Timeout(),
            _http_error(429),
            _http_error(500),
        )
        for first_error in cases:
            with self.subTest(error=type(first_error).__name__, reason=fallback_reason(first_error)):
                calls: list[str] = []
                events: list[str] = []
                legacy.encode_image_to_b64 = lambda _path: f"encoded:{legacy.MODEL_NAME}"

                def score(_path: Path, _image_b64: str) -> tuple[dict[str, Any], dict[str, Any]]:
                    """主厂商抛出当前网络错误，备用厂商返回合法评分。"""
                    calls.append(legacy.MODEL_NAME)
                    if legacy.MODEL_NAME == "primary":
                        raise first_error
                    return ({"caption": "描述", "type": "日常", "memory_score": 70,
                             "beauty_score": 60, "reason": "理由"}, {"datetime": None})

                legacy.call_vlm = score
                legacy.generate_side_caption = lambda _path, _image: "旁白"
                result = photo_analyzer.analyze_single_photo(
                    Path("/tmp/sample.jpg"), settings=_settings(),
                    provider_chain=[primary, backup],
                    narration_provider_chain=[backup],
                    on_provider_fallback=lambda purpose, reason, _old, _new: events.append(
                        f"{purpose}:{reason}"
                    ),
                    city_resolver=lambda _lat, _lon: "",
                )

                self.assertEqual("描述", result["caption"])
                self.assertEqual(["primary", "backup"], calls)
                self.assertEqual([f"analysis:{fallback_reason(first_error)}"], events)

    def test_client_and_response_errors_do_not_advance(self) -> None:
        """HTTP 400/401、响应 JSON、字段和正文 JSON 错误均停在当前厂商。"""
        primary = _provider("主厂商", "primary")
        backup = _provider("备用厂商", "backup")
        cases = (
            _http_error(400),
            _http_error(401),
            json.JSONDecodeError("bad response", "{", 0),
            KeyError("choices"),
            ValueError("model body json invalid"),
        )
        for first_error in cases:
            with self.subTest(error=type(first_error).__name__):
                calls: list[str] = []
                events: list[str] = []
                legacy.encode_image_to_b64 = lambda _path: "encoded"

                def score(_path: Path, _image_b64: str) -> tuple[dict[str, Any], dict[str, Any]]:
                    """记录当前厂商并抛出不可降级错误。"""
                    calls.append(legacy.MODEL_NAME)
                    raise first_error

                legacy.call_vlm = score
                with self.assertRaises(type(first_error)):
                    photo_analyzer.analyze_single_photo(
                        Path("/tmp/sample.jpg"), settings=_settings(),
                        provider_chain=[primary, backup],
                        on_provider_fallback=lambda purpose, reason, _old, _new: events.append(
                            f"{purpose}:{reason}"
                        ),
                    )
                self.assertEqual(["primary"], calls)
                self.assertEqual([], events)


class _WorkerRepository:
    """记录工作线程整链耗尽后的任务级失败次数。"""

    def __init__(self) -> None:
        """初始化失败和完成计数。"""
        self.failures = 0
        self.fallbacks = 0

    def is_cancel_requested(self, _job_id: int, _worker_id: str) -> bool:
        """测试任务始终可执行。"""
        return False

    def renew_lease(self, _job_id: int, _worker_id: str, _seconds: int) -> bool:
        """测试期间始终保持租约。"""
        return True

    def record_provider_fallback(self, *_args: Any) -> bool:
        """记录一次候选转向。"""
        self.fallbacks += 1
        return True

    def fail_attempt(self, *_args: Any, **_kwargs: Any) -> str:
        """记录一次任务级失败。"""
        self.failures += 1
        return "pending"

    def complete(self, *_args: Any, **_kwargs: Any) -> None:
        """整链耗尽用例不应完成。"""
        raise AssertionError("job should not complete")


class _WorkerConfiguration:
    """提供最小任务设置和两项分析候选快照。"""

    def resolve_task_snapshot(self, _job: dict, _scope: str) -> dict[str, Any]:
        """返回最小非空设置映射。"""
        return {"configured": True}

    def resolve_task_provider_snapshot(self, _job: dict, _scope: str) -> dict[str, list[dict]]:
        """返回两个有序分析候选。"""
        return {"analysis": [_provider("主厂商", "primary"), _provider("备用厂商", "backup")]}

    def get(self, _key: str) -> str:
        """返回旧密钥回退值。"""
        return "legacy-key"


class WorkerChainExhaustionTestCase(unittest.TestCase):
    """验证整条候选链耗尽后才消耗一次任务尝试。"""

    def test_chain_exhaustion_calls_fail_attempt_once(self) -> None:
        """两个候选均网络失败时记录一次转向和一次任务级失败。"""
        repository = _WorkerRepository()

        def analyzer(_path: Path, **kwargs: Any) -> dict[str, Any]:
            """模拟遍历两个候选后仍失败。"""
            chain = kwargs["provider_chain"]
            kwargs["on_provider_fallback"](
                "analysis", CONNECTION_ERROR, chain[0], chain[1]
            )
            raise ProviderTransportError(CONNECTION_ERROR)

        worker = AnalysisWorker(
            repository,
            analyzer,
            lambda *_args, **_kwargs: "旁白",
            configuration_service=_WorkerConfiguration(),
        )
        worker._execute({
            "id": 1, "photo_id": 2, "job_type": "analyze_photo",
            "photo_path": "/tmp/sample.jpg", "config_version": 1,
            "config_snapshot_json": "{}", "photo_original_filename": "sample.jpg",
        })

        self.assertEqual(1, repository.fallbacks)
        self.assertEqual(1, repository.failures)


class PanelProviderFallbackTestCase(unittest.TestCase):
    """验证面板只在网络类错误时切换厂商。"""

    def setUp(self) -> None:
        """清空缓存并恢复人工智能调用函数。"""
        panel.reset_cache()
        self.addCleanup(panel.reset_cache)
        self.original_call = panel._call_ai
        self.addCleanup(setattr, panel, "_call_ai", self.original_call)
        self.items = [
            {"year": 2001, "text": "事件一"},
            {"year": 2002, "text": "事件二"},
        ]
        self.chain = [
            {"name": "主厂商", "api_url": "https://primary", "api_key": "a", "model_name": "m1"},
            {"name": "备用厂商", "api_url": "https://backup", "api_key": "b", "model_name": "m2"},
        ]

    def test_network_error_switches_provider(self) -> None:
        """主厂商连接失败后调用备用厂商并返回其结果。"""
        calls: list[str] = []

        def call(_items: list, _count: int, _override: str, url: str, _key: str, _model: str) -> list:
            """主地址失败，备用地址成功。"""
            calls.append(url)
            if url == "https://primary":
                raise urllib.error.URLError(OSError())
            return [{"year": 2002, "text": "备用结果", "ai": True}]

        panel._call_ai = call
        result = panel._ai_select(
            self.items, dt.date(2026, 1, 2), 1, 1900,
            "", "", "", "", "baidu", self.chain,
        )

        self.assertEqual(["https://primary", "https://backup"], calls)
        self.assertEqual("备用结果", result[0]["text"])

    def test_content_error_immediately_uses_curated(self) -> None:
        """模型内容错误不调用备用厂商，立即返回规则精选。"""
        calls: list[str] = []

        def call(_items: list, _count: int, _override: str, url: str, _key: str, _model: str) -> list:
            """记录地址并抛出不可降级内容错误。"""
            calls.append(url)
            raise ValueError("invalid content")

        panel._call_ai = call
        result = panel._ai_select(
            self.items, dt.date(2026, 1, 2), 1, 1900,
            "", "", "", "", "baidu", self.chain,
        )

        self.assertEqual(["https://primary"], calls)
        self.assertFalse(result[0].get("ai", False))


if __name__ == "__main__":
    unittest.main()
