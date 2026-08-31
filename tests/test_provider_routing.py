"""验证多模型用途路由写入校验、运行时选择和旧配置兼容。"""

from __future__ import annotations

from src.configuration import (
    ConfigurationActor,
    ConfigurationService,
    ConfigurationValidationError,
)
from src.server.model_providers import ModelProviderService
from src.server.repositories.model_provider_repository import ModelProviderRepository
from src.server.services import PanelService
from tests.support import TemporaryDatabaseTestCase


class _PanelRecorder:
    """记录 PanelService 传给面板模块的最终模型参数。"""

    def __init__(self) -> None:
        """初始化为空调用记录。"""
        self.calls: list[dict] = []

    def get_panel_data(self, **kwargs):
        """保存调用参数并返回最小合法聚合结果。"""
        self.calls.append(dict(kwargs))
        return {"onthisday": {"available": True}}


class ProviderRoutingTestCase(TemporaryDatabaseTestCase):
    """验证三类用途路由和信息面板优先级。"""

    def setUp(self) -> None:
        """准备旧配置、厂商服务及两个启用中的厂商档案。"""
        super().setUp()
        self.configuration = ConfigurationService(
            self.database_path,
            environment={
                "API_URL": "https://legacy.example.com/v1/chat/completions",
                "API_KEY": "legacy-secret",
                "MODEL_NAME": "legacy-model",
            },
        )
        self.providers = ModelProviderService(
            ModelProviderRepository(self.database_path),
            configuration_service=self.configuration,
        )
        self.actor = ConfigurationActor(None, "routing-test")
        self.company = self._create_provider(
            "公司模型", "https://company.example.com/v1", "company-vlm", "company-secret"
        )
        self.panel = self._create_provider(
            "面板模型",
            "https://panel.example.com/v1/chat/completions",
            "panel-vlm",
            "panel-secret",
        )

    def _create_provider(
        self, name: str, base_url: str, model_name: str, api_key: str
    ) -> dict:
        """创建一条合法的启用厂商档案。"""
        return self.providers.create_provider(
            {
                "name": name,
                "base_url": base_url,
                "model_name": model_name,
                "api_key": api_key,
                "timeout_seconds": 90,
                "max_long_edge": 1536,
                "is_enabled": True,
            },
            None,
            "routing-test",
        )

    def _update(self, **changes) -> None:
        """按当前版本更新配置。"""
        version = self.configuration.list_admin_settings()["version"]
        self.configuration.update_batch(changes, version, self.actor)

    def test_route_write_requires_existing_enabled_provider(self) -> None:
        """保存路由时拒绝不存在或已停用的厂商，避免产生新的悬空引用。"""
        version = self.configuration.list_admin_settings()["version"]
        with self.assertRaises(ConfigurationValidationError) as captured:
            self.configuration.update_batch(
                {"ANALYSIS_PROVIDER": "不存在"}, version, self.actor
            )
        self.assertIn("厂商不存在或未启用", captured.exception.errors["ANALYSIS_PROVIDER"])

        self.providers.update_provider(
            self.company["id"],
            self.company["version"],
            {"is_enabled": False},
            None,
            "routing-test",
        )
        version = self.configuration.list_admin_settings()["version"]
        with self.assertRaises(ConfigurationValidationError):
            self.configuration.update_batch(
                {"ANALYSIS_PROVIDER": "公司模型"}, version, self.actor
            )

    def test_referencing_routes_reports_all_configured_usages(self) -> None:
        """删除守卫能准确报告厂商被哪些用途路由引用。"""
        self._update(
            ANALYSIS_PROVIDER="公司模型",
            NARRATION_PROVIDER="公司模型;面板模型",
            PANEL_PROVIDER="面板模型",
        )

        self.assertEqual(
            ["ANALYSIS_PROVIDER", "NARRATION_PROVIDER"],
            self.providers.referencing_routes("公司模型"),
        )
        self.assertEqual(
            ["NARRATION_PROVIDER", "PANEL_PROVIDER"],
            self.providers.referencing_routes("面板模型"),
        )

    def test_panel_provider_overrides_legacy_endpoint_and_keeps_model_override(self) -> None:
        """PANEL_PROVIDER 选中厂商，PANEL_AI_MODEL 仍可覆盖该厂商模型名。"""
        self._update(PANEL_PROVIDER="面板模型", PANEL_AI_MODEL="panel-model-override")
        recorder = _PanelRecorder()
        service = PanelService(recorder, self.configuration, self.providers)

        result = service.get_data(force=True)

        self.assertEqual("ok", result["status"])
        call = recorder.calls[-1]
        self.assertEqual("https://panel.example.com/v1/chat/completions", call["api_url"])
        self.assertEqual("panel-secret", call["api_key"])
        self.assertEqual("panel-vlm", call["model_name"])
        self.assertEqual("panel-model-override", call["panel_ai_model"])

    def test_legacy_panel_model_precedes_analysis_provider_fallback(self) -> None:
        """没有 PANEL_PROVIDER 时，旧 PANEL_AI_MODEL 继续绑定旧接口而不跟随分析厂商。"""
        self._update(ANALYSIS_PROVIDER="公司模型", PANEL_AI_MODEL="legacy-panel-model")
        recorder = _PanelRecorder()

        PanelService(recorder, self.configuration, self.providers).get_data(force=False)

        call = recorder.calls[-1]
        self.assertEqual("https://legacy.example.com/v1/chat/completions", call["api_url"])
        self.assertEqual("legacy-secret", call["api_key"])
        self.assertEqual("legacy-model", call["model_name"])
        self.assertEqual("legacy-panel-model", call["panel_ai_model"])

    def test_panel_follows_analysis_provider_only_when_panel_settings_are_empty(self) -> None:
        """PANEL_PROVIDER 与 PANEL_AI_MODEL 都空时才跟随 ANALYSIS_PROVIDER。"""
        self._update(ANALYSIS_PROVIDER="公司模型")
        recorder = _PanelRecorder()

        PanelService(recorder, self.configuration, self.providers).get_data(force=False)

        call = recorder.calls[-1]
        self.assertEqual("https://company.example.com/v1/chat/completions", call["api_url"])
        self.assertEqual("company-secret", call["api_key"])
        self.assertEqual("company-vlm", call["model_name"])


class _WorkerRepository:
    """为工作进程参数路由测试提供无数据库副作用的最小仓储。"""

    def __init__(self) -> None:
        """初始化完成结果记录。"""
        self.result = None

    def is_cancel_requested(self, _job_id, _worker_id) -> bool:
        """测试任务始终保持可执行。"""
        return False

    def renew_lease(self, _job_id, _worker_id, _lease_seconds) -> bool:
        """测试执行很快，若触发心跳也保持租约。"""
        return True

    def complete(self, _job, _worker_id, result) -> None:
        """记录分析完成结果。"""
        self.result = result

    def fail_attempt(self, *_args, **_kwargs):
        """任何失败都应直接使测试失败。"""
        raise AssertionError("工作进程不应进入失败路径")


class WorkerProviderRoutingTestCase(ProviderRoutingTestCase):
    """验证工作进程把固化厂商和现读密钥传给分析函数。"""

    def test_worker_passes_distinct_analysis_and_narration_providers(self) -> None:
        """分析任务执行时分别传入两种用途的档案与密钥。"""
        from pathlib import Path

        from src.analysis.run_worker import build_provider_task_snapshot
        from src.server.admin_jobs import AnalysisWorker

        self._update(
            ANALYSIS_PROVIDER="公司模型", NARRATION_PROVIDER="面板模型"
        )
        with self.database() as connection:
            version, snapshot_json = build_provider_task_snapshot(
                self.configuration,
                self.providers,
                "analyze_photo",
                "analysis",
                connection,
            )
        captured = {}

        def analyzer(_path: Path, **kwargs):
            """记录工作进程传入的模型参数并返回最小结果。"""
            captured.update(kwargs)
            return {"caption": "完成"}

        repository = _WorkerRepository()
        worker = AnalysisWorker(
            repository,
            analyzer,
            lambda *_args, **_kwargs: "旁白",
            configuration_service=self.configuration,
            model_provider_service=self.providers,
        )
        worker._execute(
            {
                "id": 1,
                "photo_id": 2,
                "job_type": "analyze_photo",
                "photo_path": "/tmp/provider-routing-test.jpg",
                "photo_original_filename": "original.jpg",
                "config_version": version,
                "config_snapshot_json": snapshot_json,
            }
        )

        self.assertEqual("公司模型", captured["provider"]["name"])
        self.assertEqual("面板模型", captured["narration_provider"]["name"])
        self.assertEqual("company-secret", captured["api_key"])
        self.assertEqual("panel-secret", captured["narration_api_key"])
        self.assertEqual({"caption": "完成"}, repository.result)
