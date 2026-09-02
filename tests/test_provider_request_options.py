"""厂商特有请求参数的解析与合并测试。

存在的意义：同一个 OpenAI 兼容协议，各家扩展参数并不通用。`enable_thinking` 只有千问
认，硬传给别家可能直接 400；而 `response_format` 在千问上反而让旁白正文多包一层数组。
这类差异只能按厂商配，因此需要一条「合并进请求体、并且能删掉某个参数」的通路。

「删掉」这个语义不能省：OpenAI 兼容层没有「结构化输出=关」这种取值，唯一的关闭方式
就是整个参数不发送。
"""

from __future__ import annotations

import unittest

from src.provider_options import apply_request_options, parse_request_options


class ParseRequestOptionsTestCase(unittest.TestCase):
    """验证读取路径对各种存储取值都能给出可用结果。"""

    def test_valid_json_object_is_parsed(self) -> None:
        """正常取值解析为字典。"""
        self.assertEqual(
            {"enable_thinking": False},
            parse_request_options('{"enable_thinking": false}'),
        )

    def test_mapping_is_accepted_as_is(self) -> None:
        """已解析的映射直接规范化，快照双读路径会传这种形态。"""
        self.assertEqual(
            {"enable_thinking": False}, parse_request_options({"enable_thinking": False})
        )

    def test_null_value_is_preserved(self) -> None:
        """空值必须保留：它表示「删掉该参数」，丢掉就等于该语义失效。"""
        self.assertEqual(
            {"response_format": None}, parse_request_options('{"response_format": null}')
        )

    def test_blank_values_yield_empty_options(self) -> None:
        """空串、空对象与缺省都表示没有额外参数。"""
        for raw in ("", "   ", "{}", None):
            with self.subTest(raw=raw):
                self.assertEqual({}, parse_request_options(raw))

    def test_unusable_values_fall_back_to_empty(self) -> None:
        """读取路径宽容：手工改库留下的非法值按无参数处理，不让分析链路失效。"""
        for raw in ("{not json", '["array"]', '"scalar"', "123"):
            with self.subTest(raw=raw):
                self.assertEqual({}, parse_request_options(raw))


class ApplyRequestOptionsTestCase(unittest.TestCase):
    """验证合并语义，尤其是删除参数这一条。"""

    def base_payload(self) -> dict:
        """构造一份带结构化输出的请求体。"""
        return {
            "model": "some-model",
            "max_tokens": 2048,
            "response_format": {"type": "json_schema"},
        }

    def test_new_key_is_added(self) -> None:
        """新参数直接加进请求体顶层。"""
        payload = apply_request_options(self.base_payload(), {"enable_thinking": False})

        self.assertIs(False, payload["enable_thinking"])

    def test_existing_key_is_overridden(self) -> None:
        """已有参数被覆盖，便于按厂商调 token 预算之类的取值。"""
        payload = apply_request_options(self.base_payload(), {"max_tokens": 256})

        self.assertEqual(256, payload["max_tokens"])

    def test_null_removes_the_parameter(self) -> None:
        """空值删掉该参数，这是关闭结构化输出的唯一方式。"""
        payload = apply_request_options(self.base_payload(), {"response_format": None})

        self.assertNotIn("response_format", payload)

    def test_removing_absent_key_is_harmless(self) -> None:
        """删一个本来就没有的参数不应报错。"""
        payload = apply_request_options({"model": "m"}, {"response_format": None})

        self.assertEqual({"model": "m"}, payload)

    def test_empty_options_leave_payload_untouched(self) -> None:
        """没有额外参数时请求体保持原样，未配厂商的行为完全不变。"""
        for options in ({}, None):
            with self.subTest(options=options):
                self.assertEqual(
                    self.base_payload(), apply_request_options(self.base_payload(), options)
                )

    def test_qwen_combination_disables_thinking_and_structured_output(self) -> None:
        """千问的实际取值：一次合并同时关掉思考与结构化输出。"""
        payload = apply_request_options(
            self.base_payload(),
            {"enable_thinking": False, "response_format": None},
        )

        self.assertIs(False, payload["enable_thinking"])
        self.assertNotIn("response_format", payload)
        self.assertEqual(2048, payload["max_tokens"])


class LegacyOverrideTestCase(unittest.TestCase):
    """验证参数随候选覆盖进分析模块全局，并在退出时完整还原。"""

    def settings(self) -> dict:
        """构造分析作用域配置的最小可用集合。"""
        return {
            "API_URL": "https://legacy.example.com/v1/chat/completions",
            "MODEL_NAME": "legacy-model",
            "TIMEOUT": 30,
            "VLM_MAX_LONG_EDGE": 1024,
            "WORLD_CITIES_CSV": "",
            "CITY_GRID_DEG": 1.0,
            "CITY_MAX_DISTANCE_KM": 50.0,
            "HOME_LAT": 0.0,
            "HOME_LON": 0.0,
            "HOME_RADIUS_KM": 5.0,
        }

    def candidate(self, name: str, options: dict | None) -> dict:
        """构造一条带或不带额外参数的候选。"""
        return {
            "id": 1,
            "name": name,
            "version": 1,
            "base_url": f"https://{name}.example.com/v1",
            "model_name": f"{name}-model",
            "timeout_seconds": 30,
            "max_long_edge": 1024,
            **({"request_options": options} if options is not None else {}),
        }

    def test_options_are_applied_and_restored(self) -> None:
        """候选的额外参数在覆盖期内生效，退出后还原为原值。

        还原这一条必须守住：不还原的话，一个配了参数的厂商会污染同进程后续所有分析，
        表现为「随机某些照片用错了参数」，极难排查。
        """
        import src.analysis.analyze_photos_docker as legacy
        from src.analysis.photo_analyzer import _temporary_legacy_configuration

        before = legacy.PROVIDER_REQUEST_OPTIONS
        options = {"enable_thinking": False, "response_format": None}

        with _temporary_legacy_configuration(
            self.settings(), "secret", self.candidate("qwen", options)
        ):
            self.assertEqual(options, legacy.PROVIDER_REQUEST_OPTIONS)

        self.assertIs(before, legacy.PROVIDER_REQUEST_OPTIONS)

    def test_candidate_without_options_clears_previous_value(self) -> None:
        """切到没配参数的候选时必须清空，不能留着上一个候选的取值。"""
        import src.analysis.analyze_photos_docker as legacy
        from src.analysis.photo_analyzer import _temporary_legacy_configuration

        with _temporary_legacy_configuration(
            self.settings(), "secret", self.candidate("qwen", {"enable_thinking": False})
        ):
            pass
        with _temporary_legacy_configuration(
            self.settings(), "secret", self.candidate("company", None)
        ):
            self.assertEqual({}, legacy.PROVIDER_REQUEST_OPTIONS)

    def test_missing_provider_is_rejected_instead_of_falling_back(self) -> None:
        """没有厂商档案时明确失败，而不是回退到一套注册表兜底配置。

        兜底键已从注册表移除。静默回退是个陷阱：路由填错或档案被删时分析照样"成功"，
        只是悄悄换了一套配置，等发现时已经烧了一批额度。
        """
        from src.analysis.photo_analyzer import (
            NoModelProviderError,
            _temporary_legacy_configuration,
        )

        with self.assertRaises(NoModelProviderError):
            with _temporary_legacy_configuration(self.settings(), "secret", None):
                pass  # pragma: no cover - 进入上下文即应抛错


if __name__ == "__main__":
    unittest.main()
