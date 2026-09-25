"""照片分析与展示文案业务提示词在线配置的回归测试。"""

from __future__ import annotations

import json

from flask import render_template

from src.analysis import analyze_photos_docker as legacy
from src.analysis.photo_analyzer import _temporary_legacy_configuration
from src.configuration import (
    DEFAULT_PHOTO_ANALYSIS_PROMPT,
    DEFAULT_PHOTO_NARRATION_PROMPT,
    PHOTO_ANALYSIS_PROMPT_MAX_LENGTH,
    PHOTO_NARRATION_PROMPT_MAX_LENGTH,
    SETTING_REGISTRY,
    ConfigurationActor,
    ConfigurationService,
    ConfigurationValidationError,
)
from src.server.app import create_app
from src.server.blueprints.admin import _settings_context
from tests.support import TemporaryDatabaseTestCase


class AnalysisPromptSettingsTestCase(TemporaryDatabaseTestCase):
    """验证提示词注册、持久化、历史快照兼容与执行期覆盖。"""

    def setUp(self) -> None:
        """创建绑定临时数据库的配置服务和审计操作者。"""
        super().setUp()
        self.configuration = ConfigurationService(
            self.database_path, environment=self.application_config()
        )
        self.actor = ConfigurationActor(self.create_admin_user(), "prompt-admin")

    def _update_prompts(self, analysis: str, narration: str) -> None:
        """按当前配置版本原子更新两段业务提示词。"""
        version = self.configuration.list_admin_settings()["version"]
        self.configuration.update_batch(
            {
                "PHOTO_ANALYSIS_PROMPT": analysis,
                "PHOTO_NARRATION_PROMPT": narration,
            },
            version,
            self.actor,
        )

    def test_registry_and_page_expose_multiline_prompt_fields(self) -> None:
        """两个提示词应可在线编辑，并使用带长度上限的多行文本域。"""
        for key in ("PHOTO_ANALYSIS_PROMPT", "PHOTO_NARRATION_PROMPT"):
            definition = SETTING_REGISTRY[key]
            self.assertTrue(definition.editable)
            self.assertFalse(definition.restart_required)
            self.assertIn("analysis", definition.scopes)

        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            html = render_template("admin/settings.html", **_settings_context())

        self.assertIn('name="PHOTO_ANALYSIS_PROMPT"', html)
        self.assertIn(f'maxlength="{PHOTO_ANALYSIS_PROMPT_MAX_LENGTH}"', html)
        self.assertIn('name="PHOTO_NARRATION_PROMPT"', html)
        self.assertIn(f'maxlength="{PHOTO_NARRATION_PROMPT_MAX_LENGTH}"', html)
        self.assertIn("JSON 字段、单句格式等输出协议由代码固定追加", html)

    def test_prompt_updates_are_persisted_and_snapshotted(self) -> None:
        """保存后的业务提示词应进入新的 analysis 任务快照。"""
        self._update_prompts("新的评分规则", "新的展示文案风格")

        values = self.configuration.get_many(
            ("PHOTO_ANALYSIS_PROMPT", "PHOTO_NARRATION_PROMPT")
        )
        self.assertEqual("新的评分规则", values["PHOTO_ANALYSIS_PROMPT"])
        self.assertEqual("新的展示文案风格", values["PHOTO_NARRATION_PROMPT"])

        version, snapshot_json = self.configuration.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        self.assertEqual(version, snapshot["version"])
        self.assertEqual("新的评分规则", snapshot["settings"]["PHOTO_ANALYSIS_PROMPT"])
        self.assertEqual(
            "新的展示文案风格", snapshot["settings"]["PHOTO_NARRATION_PROMPT"]
        )

    def test_empty_and_oversized_prompts_are_rejected_together(self) -> None:
        """任一提示词非法时整批配置不得部分写入。"""
        version = self.configuration.list_admin_settings()["version"]
        with self.assertRaises(ConfigurationValidationError):
            self.configuration.update_batch(
                {
                    "PHOTO_ANALYSIS_PROMPT": " ",
                    "PHOTO_NARRATION_PROMPT": "x" * (
                        PHOTO_NARRATION_PROMPT_MAX_LENGTH + 1
                    ),
                },
                version,
                self.actor,
            )

        values = self.configuration.get_many(
            ("PHOTO_ANALYSIS_PROMPT", "PHOTO_NARRATION_PROMPT")
        )
        self.assertEqual(DEFAULT_PHOTO_ANALYSIS_PROMPT, values["PHOTO_ANALYSIS_PROMPT"])
        self.assertEqual(
            DEFAULT_PHOTO_NARRATION_PROMPT, values["PHOTO_NARRATION_PROMPT"]
        )

    def test_historical_snapshot_uses_built_in_prompt_defaults(self) -> None:
        """升级前快照缺少提示词时必须补旧默认值，而不是读取当前在线值。"""
        version, snapshot_json = self.configuration.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        snapshot["settings"].pop("PHOTO_ANALYSIS_PROMPT")
        snapshot["settings"].pop("PHOTO_NARRATION_PROMPT")
        self._update_prompts("在线新评分规则", "在线新文案规则")

        resolved = self.configuration.resolve_task_snapshot(
            {
                "config_version": version,
                "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            },
            "analysis",
        )

        self.assertEqual(DEFAULT_PHOTO_ANALYSIS_PROMPT, resolved["PHOTO_ANALYSIS_PROMPT"])
        self.assertEqual(
            DEFAULT_PHOTO_NARRATION_PROMPT, resolved["PHOTO_NARRATION_PROMPT"]
        )

    def test_runtime_override_restores_module_prompts(self) -> None:
        """任务执行期使用快照提示词，退出后恢复模块默认值避免污染后续任务。"""
        settings = self.configuration.snapshot("analysis")["settings"]
        settings["PHOTO_ANALYSIS_PROMPT"] = "任务评分规则"
        settings["PHOTO_NARRATION_PROMPT"] = "任务文案规则"
        provider = {
            "base_url": "https://provider.example.com/v1",
            "model_name": "vision-model",
            "timeout_seconds": 30,
            "max_long_edge": 1024,
            "request_options": {},
        }
        previous_analysis = legacy.PHOTO_ANALYSIS_PROMPT
        previous_narration = legacy.PHOTO_NARRATION_PROMPT

        with _temporary_legacy_configuration(settings, "secret", provider):
            self.assertEqual("任务评分规则", legacy.PHOTO_ANALYSIS_PROMPT)
            self.assertEqual("任务文案规则", legacy.PHOTO_NARRATION_PROMPT)

        self.assertEqual(previous_analysis, legacy.PHOTO_ANALYSIS_PROMPT)
        self.assertEqual(previous_narration, legacy.PHOTO_NARRATION_PROMPT)
