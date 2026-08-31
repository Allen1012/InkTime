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

        self.assertEqual("公司模型", provider["analysis"]["name"])
        self.assertEqual("旁白模型", provider["narration"]["name"])
        self.assertEqual("company-vlm", provider["analysis"]["model_name"])
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
            ]["name"],
        )
        self.assertEqual(
            "旁白模型",
            self.configuration.resolve_task_provider_snapshot(new_job, "analysis")[
                "analysis"
            ]["name"],
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
