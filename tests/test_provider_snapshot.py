"""验证多模型用途路由的任务快照兼容性与密钥边界。"""

from __future__ import annotations

import json

from src.analysis.run_worker import build_provider_task_snapshot
from src.configuration import ConfigurationActor, ConfigurationService
from src.server.model_providers import ModelProviderService
from src.server.repositories.model_provider_repository import ModelProviderRepository
from tests.support import TemporaryDatabaseTestCase


SECRET = "sk-provider-snapshot-secret"


class ProviderSnapshotTestCase(TemporaryDatabaseTestCase):
    """验证旧快照、新 provider 块和认领时固化语义。"""

    def setUp(self) -> None:
        """准备统一配置服务和两个启用中的厂商档案。"""
        super().setUp()
        self.configuration = ConfigurationService(self.database_path)
        self.providers = ModelProviderService(
            ModelProviderRepository(self.database_path),
            configuration_service=self.configuration,
        )
        self.actor = ConfigurationActor(None, "snapshot-test")
        self.analysis = self.providers.create_provider(
            {
                "name": "公司模型",
                "base_url": "https://company.example.com/v1",
                "model_name": "company-vlm",
                "api_key": SECRET,
                "timeout_seconds": 120,
                "max_long_edge": 2048,
                "is_enabled": True,
            },
            None,
            "snapshot-test",
        )
        self.narration = self.providers.create_provider(
            {
                "name": "旁白模型",
                "base_url": "https://narration.example.com/v1/chat/completions",
                "model_name": "narration-vlm",
                "api_key": "sk-narration-secret",
                "timeout_seconds": 60,
                "max_long_edge": 1280,
                "is_enabled": True,
            },
            None,
            "snapshot-test",
        )

    def _update_routes(self, **changes: str) -> None:
        """按当前配置版本保存用途路由。"""
        version = self.configuration.list_admin_settings()["version"]
        self.configuration.update_batch(changes, version, self.actor)

    def test_old_snapshot_without_provider_remains_valid(self) -> None:
        """阶段二上线前的两字段快照继续按原 settings 严格解析。"""
        version, snapshot_json = self.configuration.task_snapshot("analysis")
        job = {"config_version": version, "config_snapshot_json": snapshot_json}

        settings = self.configuration.resolve_task_snapshot(job, "analysis")
        providers = self.configuration.resolve_task_provider_snapshot(job, "analysis")

        self.assertEqual(self.configuration.get("MODEL_NAME"), settings["MODEL_NAME"])
        self.assertEqual({}, providers)
        self.assertEqual({"version", "settings"}, set(json.loads(snapshot_json)))

    def test_analysis_and_narration_providers_are_frozen_without_secrets(self) -> None:
        """首次认领同时固化分析与旁白公开参数，快照不含任何密钥字段或值。"""
        self._update_routes(
            ANALYSIS_PROVIDER="公司模型", NARRATION_PROVIDER="旁白模型"
        )
        with self.database() as connection:
            version, snapshot_json = build_provider_task_snapshot(
                self.configuration,
                self.providers,
                "analyze_photo",
                "analysis",
                connection,
            )
        job = {"config_version": version, "config_snapshot_json": snapshot_json}
        provider = self.configuration.resolve_task_provider_snapshot(job, "analysis")

        self.assertEqual("公司模型", provider["analysis"][0]["name"])
        self.assertEqual("旁白模型", provider["narration"][0]["name"])
        self.assertEqual("company-vlm", provider["analysis"][0]["model_name"])
        self.assertNotIn("api_key", snapshot_json.lower())
        self.assertNotIn(SECRET, snapshot_json)
        self.assertNotIn("sk-narration-secret", snapshot_json)

    def test_route_switch_does_not_change_already_claimed_snapshot(self) -> None:
        """路由切换只影响后续认领，已生成快照仍使用原厂商。"""
        self._update_routes(ANALYSIS_PROVIDER="公司模型")
        with self.database() as connection:
            version, old_json = build_provider_task_snapshot(
                self.configuration,
                self.providers,
                "analyze_photo",
                "analysis",
                connection,
            )
        self._update_routes(ANALYSIS_PROVIDER="旁白模型")
        with self.database() as connection:
            new_version, new_json = build_provider_task_snapshot(
                self.configuration,
                self.providers,
                "analyze_photo",
                "analysis",
                connection,
            )

        old_job = {"config_version": version, "config_snapshot_json": old_json}
        new_job = {"config_version": new_version, "config_snapshot_json": new_json}
        self.assertEqual(
            "公司模型",
            self.configuration.resolve_task_provider_snapshot(old_job, "analysis")[
                "analysis"
            ][0]["name"],
        )
        self.assertEqual(
            "旁白模型",
            self.configuration.resolve_task_provider_snapshot(new_job, "analysis")[
                "analysis"
            ][0]["name"],
        )

    def test_provider_snapshot_rejects_secret_or_unknown_fields(self) -> None:
        """provider 块采用精确白名单，密钥字段和未知字段都会使任务稳定失败。"""
        version, snapshot_json = self.configuration.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        snapshot["provider"] = {
            "analysis": {
                "id": self.analysis["id"],
                "name": "公司模型",
                "version": self.analysis["version"],
                "base_url": self.analysis["base_url"],
                "model_name": self.analysis["model_name"],
                "timeout_seconds": self.analysis["timeout_seconds"],
                "max_long_edge": self.analysis["max_long_edge"],
                "api_key": SECRET,
            }
        }
        job = {
            "config_version": version,
            "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        }

        with self.assertRaisesRegex(ValueError, "provider.analysis 字段不合法"):
            self.configuration.resolve_task_snapshot(job, "analysis")

    def _candidate_snapshot(self, **extra) -> dict:
        """构造一条合法的 analysis 候选快照，便于用例只覆盖关心的字段。"""
        candidate = {
            "id": self.analysis["id"],
            "name": "公司模型",
            "version": self.analysis["version"],
            "base_url": self.analysis["base_url"],
            "model_name": self.analysis["model_name"],
            "timeout_seconds": self.analysis["timeout_seconds"],
            "max_long_edge": self.analysis["max_long_edge"],
        }
        candidate.update(extra)
        return candidate

    def _resolve_with_candidate(self, candidate: dict) -> dict:
        """把给定候选塞进 analysis 用途并走一次完整快照解析。"""
        version, snapshot_json = self.configuration.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        snapshot["provider"] = {"analysis": [candidate]}
        job = {
            "config_version": version,
            "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        }
        return self.configuration.resolve_task_provider_snapshot(job, "analysis")

    def test_snapshot_carries_optional_request_options(self) -> None:
        """配了高级请求参数的候选，参数随快照一起固化并原样读回。"""
        options = {"enable_thinking": False, "response_format": None}

        providers = self._resolve_with_candidate(
            self._candidate_snapshot(request_options=options)
        )

        self.assertEqual(options, providers["analysis"][0]["request_options"])

    def test_snapshot_without_request_options_stays_valid(self) -> None:
        """没配额外参数的候选不带该键，阶段三之前固化的快照因此继续可读。"""
        providers = self._resolve_with_candidate(self._candidate_snapshot())

        self.assertNotIn("request_options", providers["analysis"][0])

    def test_snapshot_rejects_non_object_request_options(self) -> None:
        """该字段必须是对象：数组或标量没有合并进请求体顶层的语义。"""
        for invalid in (["enable_thinking"], "false", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "高级请求参数必须是对象"):
                    self._resolve_with_candidate(
                        self._candidate_snapshot(request_options=invalid)
                    )

    def test_stage_three_snapshot_freezes_complete_ordered_chains(self) -> None:
        """阶段三为两个用途固化 resolve_chain 的完整有序公开档案数组。"""
        self._update_routes(
            ANALYSIS_PROVIDER="公司模型;旁白模型",
            NARRATION_PROVIDER="旁白模型;公司模型",
        )
        with self.database() as connection:
            version, snapshot_json = build_provider_task_snapshot(
                self.configuration, self.providers, "analyze_photo", "analysis", connection
            )
        snapshot = json.loads(snapshot_json)
        job = {"config_version": version, "config_snapshot_json": snapshot_json}
        providers = self.configuration.resolve_task_provider_snapshot(job, "analysis")

        self.assertIsInstance(snapshot["provider"]["analysis"], list)
        self.assertEqual(
            ["公司模型", "旁白模型"],
            [item["name"] for item in providers["analysis"]],
        )
        self.assertEqual(
            ["旁白模型", "公司模型"],
            [item["name"] for item in providers["narration"]],
        )
        self.assertNotIn("api_key", snapshot_json.lower())

    def test_stage_two_single_provider_object_is_promoted_to_list(self) -> None:
        """阶段二用途单对象快照继续可读并统一返回单元素列表。"""
        self._update_routes(ANALYSIS_PROVIDER="公司模型")
        with self.database() as connection:
            version, snapshot_json = build_provider_task_snapshot(
                self.configuration, self.providers, "analyze_photo", "analysis", connection
            )
        snapshot = json.loads(snapshot_json)
        snapshot["provider"]["analysis"] = snapshot["provider"]["analysis"][0]
        legacy_json = json.dumps(snapshot, ensure_ascii=False)
        providers = self.configuration.resolve_task_provider_snapshot(
            {"config_version": version, "config_snapshot_json": legacy_json}, "analysis"
        )

        self.assertEqual(["公司模型"], [item["name"] for item in providers["analysis"]])

    def test_provider_snapshot_rejects_empty_candidate_array(self) -> None:
        """用途数组必须非空，避免执行期把错误配置误当旧配置。"""
        version, snapshot_json = self.configuration.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        snapshot["provider"] = {"analysis": []}
        job = {
            "config_version": version,
            "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        }

        with self.assertRaisesRegex(ValueError, "必须是非空数组"):
            self.configuration.resolve_task_snapshot(job, "analysis")
