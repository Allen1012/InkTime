"""配置在线可改（阶段一）的临时数据库测试。"""

from __future__ import annotations

import json
from typing import Any

from flask import render_template

from src.configuration import (
    SETTING_REGISTRY,
    ConfigurationActor,
    ConfigurationConflictError,
    ConfigurationService,
    ConfigurationValidationError,
)
from src.server.app import create_app
from src.server.blueprints.admin import _parse_settings_form, _settings_context
from tests.support import TemporaryDatabaseTestCase


# 阶段一放开的配置项：分析、渲染与站点名称类，全部可在后台修改且无需重启。
NEWLY_EDITABLE_KEYS = frozenset(
    {
        "PROJECT_NAME",
        "API_URL",
        "MODEL_NAME",
        "TIMEOUT",
        "VLM_MAX_LONG_EDGE",
        "WORLD_CITIES_CSV",
        "CITY_GRID_DEG",
        "CITY_MAX_DISTANCE_KM",
        "HOME_LAT",
        "HOME_LON",
        "HOME_RADIUS_KM",
        "FONT_PATH",
        "API_KEY",
    }
)
# 阶段二放开的配置项：上传上限、任务调度与回收站保留期，均改为方法内动态取值。
STAGE_TWO_EDITABLE_KEYS = frozenset(
    {
        "UPLOAD_MAX_FILES",
        "UPLOAD_MAX_BYTES",
        "UPLOAD_MAX_PIXELS",
        "JOB_MAX_ATTEMPTS",
        "JOB_LEASE_SECONDS",
        "JOB_RENEW_SECONDS",
        "JOB_POLL_SECONDS",
        "TRASH_RETENTION_DAYS",
        "ENABLE_REVIEW_WEBUI",
        "ENABLE_FILE_BROWSER",
    }
)
# 阶段一之前已经可在线编辑的展示与渲染类配置。
PREVIOUSLY_EDITABLE_KEYS = frozenset(
    {
        "DISPLAY_TEMPLATE",
        "DISPLAY_ROTATE_MODE",
        "DISPLAY_ROTATE_INTERVAL_SEC",
        "DISPLAY_KEEP_AWAKE",
        "DISPLAY_UI_HIDE_DELAY_SEC",
        "DISPLAY_MIN_SCORE",
        "DISPLAY_NEW_PHOTO_WEIGHT",
        "ONTHISDAY_COUNT",
        "ONTHISDAY_STRATEGY",
        "ONTHISDAY_MIN_YEAR",
        "PANEL_AI_MODEL",
        "MEMORY_THRESHOLD",
        "DAILY_PHOTO_QUANTITY",
        "FILL_FROM_GLOBAL",
    }
)
# 必须保持锁定的系统、网络与安全边界配置。
STILL_LOCKED_KEYS = (
    "APP_ENV",
    "DB_PATH",
    "IMAGE_DIR",
    "BIN_OUTPUT_DIR",
    "FLASK_HOST",
    "FLASK_PORT",
    "SECRET_KEY",
    "DOWNLOAD_KEY",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SAMESITE",
    "PERMANENT_SESSION_LIFETIME",
    "WTF_CSRF_TIME_LIMIT",
    "ADMIN_LOGIN_MAX_FAILURES",
    "ADMIN_LOGIN_FAILURE_WINDOW_SECONDS",
)


class ConfigurationRegistryTestCase(TemporaryDatabaseTestCase):
    """校验注册表的可编辑范围与生效时机标记。"""

    def test_newly_editable_keys_are_hot_and_locked_keys_unchanged(self) -> None:
        """验证放开项均为热生效，且系统与安全项仍然只读。"""
        hot_editable = {
            key
            for key, definition in SETTING_REGISTRY.items()
            if definition.editable and not definition.restart_required
        }
        self.assertEqual(
            NEWLY_EDITABLE_KEYS | STAGE_TWO_EDITABLE_KEYS | PREVIOUSLY_EDITABLE_KEYS,
            hot_editable,
        )
        for key in NEWLY_EDITABLE_KEYS | STAGE_TWO_EDITABLE_KEYS:
            definition = SETTING_REGISTRY[key]
            self.assertTrue(definition.editable, key)
            self.assertFalse(definition.restart_required, key)
        self.assertTrue(SETTING_REGISTRY["API_KEY"].sensitive)
        for key in STILL_LOCKED_KEYS:
            self.assertFalse(SETTING_REGISTRY[key].editable, key)

    def test_no_editable_setting_requires_restart(self) -> None:
        """验证不存在「可编辑但需重启」的自相矛盾配置。"""
        contradictory = sorted(
            key
            for key, definition in SETTING_REGISTRY.items()
            if definition.editable and definition.restart_required
        )
        self.assertEqual([], contradictory)


class ConfigurationUpdateTestCase(TemporaryDatabaseTestCase):
    """校验配置写入、来源解析、任务快照与审计行为。"""

    def setUp(self) -> None:
        """准备审计外键所需的管理员，并固定启动环境值。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.environment = {
            "MODEL_NAME": "env-model",
            "API_URL": "http://127.0.0.1:9/v1/chat/completions",
            "API_KEY": "env-api-key",
            "TIMEOUT": "300",
            "HOME_LAT": "31.5",
        }

    def service(self) -> ConfigurationService:
        """新建一个配置服务，模拟独立进程重新读取数据库。"""
        return ConfigurationService(self.database_path, environment=self.environment)

    def stored_values(self) -> dict[str, Any]:
        """直接读取数据库中的配置覆盖值，用于确认是否真的落库。"""
        with self.database() as connection:
            row = connection.execute(
                "SELECT settings_json FROM app_settings WHERE id=1"
            ).fetchone()
        return json.loads(row["settings_json"])

    def test_analysis_setting_change_reaches_other_process_and_task_snapshot(self) -> None:
        """验证改模型后新进程与新任务快照都取到新值，环境值被覆盖。"""
        service = self.service()
        self.assertEqual("env-model", service.get("MODEL_NAME"))
        state = service.list_admin_settings()
        entry = next(item for item in state["settings"] if item["key"] == "MODEL_NAME")
        self.assertEqual("environment", entry["source"])
        self.assertTrue(entry["editable"])

        service.update_batch({"MODEL_NAME": "hot-model"}, state["version"], self.actor)

        worker_view = self.service()
        self.assertEqual("hot-model", worker_view.get("MODEL_NAME"))
        _, snapshot_json = worker_view.task_snapshot("analysis")
        self.assertEqual("hot-model", json.loads(snapshot_json)["settings"]["MODEL_NAME"])
        entry = next(
            item
            for item in worker_view.list_admin_settings()["settings"]
            if item["key"] == "MODEL_NAME"
        )
        self.assertEqual("database", entry["source"])

        audit = worker_view.list_admin_audit(10)[0]
        self.assertEqual(self.user_id, audit["modified_by_user_id"])
        self.assertEqual("test-admin", audit["modified_by_username"])
        self.assertEqual(
            [{"key": "MODEL_NAME", "name": "分析模型", "old_value": "env-model", "new_value": "hot-model"}],
            audit["changes"],
        )

    def test_api_key_is_written_but_never_exposed(self) -> None:
        """验证密钥可覆盖、立即生效，且不在管理视图、快照与审计中回显。"""
        service = self.service()
        state = service.list_admin_settings()
        secret_entry = next(item for item in state["settings"] if item["key"] == "API_KEY")
        self.assertTrue(secret_entry["configured"])
        self.assertNotIn("value", secret_entry)

        service.update_batch({"API_KEY": " sk-hot-secret "}, state["version"], self.actor)

        reader = self.service()
        self.assertEqual("sk-hot-secret", reader.get("API_KEY"))
        self.assertEqual("sk-hot-secret", self.stored_values()["API_KEY"])
        secret_entry = next(
            item for item in reader.list_admin_settings()["settings"] if item["key"] == "API_KEY"
        )
        self.assertTrue(secret_entry["configured"])
        self.assertNotIn("value", secret_entry)
        self.assertNotIn("API_KEY", reader.snapshot("worker")["settings"])
        self.assertNotIn("API_KEY", reader.snapshot("analysis")["settings"])
        _, snapshot_json = reader.task_snapshot("analysis")
        self.assertNotIn("sk-hot-secret", snapshot_json)
        audit_change = reader.list_admin_audit(10)[0]["changes"][0]
        self.assertEqual("API_KEY", audit_change["key"])
        self.assertEqual("已脱敏", audit_change["old_value"])
        self.assertEqual("已脱敏", audit_change["new_value"])

    def test_blank_api_key_submission_keeps_current_value(self) -> None:
        """验证留空提交密钥既不清空原值也不产生新版本。"""
        service = self.service()
        version = service.list_admin_settings()["version"]
        service.update_batch({"API_KEY": "sk-first"}, version, self.actor)
        after_write = self.service().list_admin_settings()["version"]

        result = self.service().update_batch({"API_KEY": "   "}, after_write, self.actor)

        self.assertEqual(after_write, result["version"])
        self.assertEqual("sk-first", self.service().get("API_KEY"))
        self.assertEqual(1, len(self.service().list_admin_audit(10)))

    def test_stale_version_raises_conflict_without_writing(self) -> None:
        """验证基于过期版本的提交冲突且不覆盖已有修改。"""
        service = self.service()
        version = service.list_admin_settings()["version"]
        service.update_batch({"MODEL_NAME": "first-writer"}, version, self.actor)

        with self.assertRaises(ConfigurationConflictError):
            self.service().update_batch({"MODEL_NAME": "second-writer"}, version, self.actor)
        self.assertEqual("first-writer", self.service().get("MODEL_NAME"))

    def test_invalid_values_are_rejected_and_not_persisted(self) -> None:
        """验证超范围、错类型与非枚举值全部被拒绝且不落库。"""
        service = self.service()
        version = service.list_admin_settings()["version"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            service.update_batch(
                {
                    "TIMEOUT": 0,
                    "HOME_LAT": "31.5",
                    "VLM_MAX_LONG_EDGE": 99999,
                    "ONTHISDAY_STRATEGY": "nope",
                },
                version,
                self.actor,
            )

        self.assertEqual(
            {"TIMEOUT", "HOME_LAT", "VLM_MAX_LONG_EDGE", "ONTHISDAY_STRATEGY"},
            set(captured.exception.errors),
        )
        self.assertEqual({}, self.stored_values())
        self.assertEqual(version, self.service().list_admin_settings()["version"])

    def test_locked_and_unknown_keys_are_rejected(self) -> None:
        """验证系统、安全与未知配置无法通过管理写入。"""
        service = self.service()
        version = service.list_admin_settings()["version"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            service.update_batch(
                {
                    "SECRET_KEY": "attempt",
                    "DOWNLOAD_KEY": "attempt",
                    "DB_PATH": "/tmp/evil.db",
                    "NOT_A_SETTING": 1,
                },
                version,
                self.actor,
            )

        self.assertEqual(
            {"SECRET_KEY", "DOWNLOAD_KEY", "DB_PATH", "NOT_A_SETTING"},
            set(captured.exception.errors),
        )
        self.assertEqual({}, self.stored_values())


class SettingsPageTestCase(TemporaryDatabaseTestCase):
    """校验配置页渲染与表单解析对敏感项的只写处理。"""

    def application(self):
        """创建绑定临时数据库并固定分析类配置的应用实例。"""
        config = self.application_config()
        config.update(
            {
                "MODEL_NAME": "page-model",
                "API_URL": "http://127.0.0.1:9/v1/chat/completions",
                "API_KEY": "page-secret-value",
                "PROJECT_NAME": "临时相册",
                "TIMEOUT": 300,
                "VLM_MAX_LONG_EDGE": 2560,
                "WORLD_CITIES_CSV": "./data/world_cities_zh.csv",
                "CITY_GRID_DEG": 1.0,
                "CITY_MAX_DISTANCE_KM": 100.0,
                "HOME_LAT": 31.5,
                "HOME_LON": 121.5,
                "HOME_RADIUS_KM": 60.0,
                "FONT_PATH": "",
                "MEMORY_THRESHOLD": 70.0,
                "FILL_FROM_GLOBAL": True,
            }
        )
        return create_app(config)

    def test_page_renders_editable_inputs_without_leaking_secret(self) -> None:
        """验证放开项渲染为可编辑控件，密钥为只写密码框且不回显。"""
        app = self.application()
        with app.test_request_context("/admin/settings"):
            html = render_template("admin/settings.html", **_settings_context())

        for key in sorted(NEWLY_EDITABLE_KEYS - {"API_KEY"}):
            self.assertIn(f'name="{key}"', html)
        self.assertIn('type="password" name="API_KEY"', html)
        self.assertNotIn("page-secret-value", html)
        self.assertIn("已配置，留空保持不变", html)
        for key in ("DB_PATH", "SECRET_KEY", "FLASK_PORT", "SESSION_COOKIE_SECURE"):
            self.assertNotIn(f'name="{key}"', html)

    def test_project_name_change_applies_without_restart(self) -> None:
        """验证站点名称改完即刻反映到公开页面，无需重建应用。"""
        app = self.application()
        client = app.test_client()
        configuration = app.extensions["inktime_services"]["configuration"]

        first = client.get("/")
        self.assertEqual(200, first.status_code)
        self.assertIn("临时相册", first.get_data(as_text=True))

        configuration.update_batch(
            {"PROJECT_NAME": "改名后的相册"},
            configuration.list_admin_settings()["version"],
            ConfigurationActor(self.create_admin_user(), "test-admin"),
        )

        second = client.get("/")
        self.assertEqual(200, second.status_code)
        body = second.get_data(as_text=True)
        self.assertIn("改名后的相册", body)
        self.assertNotIn("临时相册", body)

    def test_form_parsing_treats_blank_secret_as_unchanged(self) -> None:
        """验证表单解析在留空时跳过密钥，在填值时纳入本批变更。"""
        app = self.application()
        with app.test_request_context("/admin/settings"):
            state = _settings_context()["state"]
        form = {"expected_version": str(state["version"])}
        for item in state["settings"]:
            if not item["editable"] or item["sensitive"]:
                continue
            value = item["value"]
            form[item["key"]] = "true" if value is True else "false" if value is False else str(value)

        with app.test_request_context("/admin/settings", method="POST", data=dict(form)):
            version, changes = _parse_settings_form()
        self.assertEqual(state["version"], version)
        self.assertNotIn("API_KEY", changes)
        self.assertEqual("page-model", changes["MODEL_NAME"])

        with app.test_request_context(
            "/admin/settings", method="POST", data={**form, "API_KEY": "  "}
        ):
            _, blank_changes = _parse_settings_form()
        self.assertNotIn("API_KEY", blank_changes)

        with app.test_request_context(
            "/admin/settings", method="POST", data={**form, "API_KEY": "sk-form"}
        ):
            _, filled_changes = _parse_settings_form()
        self.assertEqual("sk-form", filled_changes["API_KEY"])
