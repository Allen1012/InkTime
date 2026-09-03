"""配置在线可改（阶段一）的临时数据库测试。"""

from __future__ import annotations

import json
import re
from typing import Any

from flask import render_template

from src.configuration import (
    RETIRED_SNAPSHOT_KEYS,
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
# 注意：API_URL、MODEL_NAME、TIMEOUT、VLM_MAX_LONG_EDGE 与 API_KEY 曾在这一组里，
# 现已随「模型接入只能存一套」的兜底配置一并移除，改由 model_providers 厂商档案承载。
# 它们进入 configuration.RETIRED_SNAPSHOT_KEYS，只为让历史任务快照仍可解析。
NEWLY_EDITABLE_KEYS = frozenset(
    {
        "PROJECT_NAME",
        "WORLD_CITIES_CSV",
        "CITY_GRID_DEG",
        "CITY_MAX_DISTANCE_KM",
        "HOME_LAT",
        "HOME_LON",
        "HOME_RADIUS_KM",
        "FONT_PATH",
    }
)
# 阶段二放开的配置项：上传上限、任务调度与回收站保留期，均改为方法内动态取值。
STAGE_TWO_EDITABLE_KEYS = frozenset(
    {
        "UPLOAD_MAX_FILES",
        "UPLOAD_MAX_BYTES",
        "UPLOAD_MAX_PIXELS",
        "UPLOAD_TARGET_BYTES",
        "UPLOAD_MAX_LONG_EDGE",
        "JOB_MAX_ATTEMPTS",
        "JOB_RETRY_BACKOFF_SECONDS",
        "JOB_LEASE_SECONDS",
        "JOB_RENEW_SECONDS",
        "JOB_POLL_SECONDS",
        "TRASH_RETENTION_DAYS",
        "ENABLE_REVIEW_WEBUI",
        "ENABLE_FILE_BROWSER",
    }
)
# 阶段三放开的配置项：照片目录，支持分号分隔多目录，写入时强校验。
STAGE_THREE_EDITABLE_KEYS = frozenset({"IMAGE_DIR"})
# 展示页生效时间段功能新增的配置项。
DISPLAY_WINDOW_KEYS = frozenset(
    {
        "DISPLAY_ACTIVE_WINDOWS",
        "DISPLAY_IDLE_MODE",
        "DISPLAY_IDLE_PHOTO_ID",
        "DISPLAY_REST_TEXT",
    }
)
# 天气显示功能新增的配置项。
WEATHER_KEYS = frozenset(
    {
        "WEATHER_ENABLED",
        "WEATHER_PROVIDER",
        "WEATHER_LOCATION",
        "WEATHER_LOCATION_NAME",
        "WEATHER_CACHE_MINUTES",
        "DISPLAY_WEATHER_SHOW",
        "DISPLAY_WEATHER_CORNER",
    }
)
# 历史事件国内数据源切换新增的配置项。
ONTHISDAY_SOURCE_KEYS = frozenset({"ONTHISDAY_SOURCE"})
# 新照片默认收录状态开关：同时决定分析由「改为已收录」还是「按张数放行」触发。
CURATION_KEYS = frozenset({"NEW_PHOTO_CURATION"})
# 多模型用途路由配置；不进入旧 settings 快照，改由顶层 provider 固化。
MODEL_ROUTING_KEYS = frozenset(
    {"ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER"}
)
# 阶段一之前已经可在线编辑的展示与渲染类配置。
PREVIOUSLY_EDITABLE_KEYS = frozenset(
    {
        "DISPLAY_TEMPLATE",
        "THUMBNAIL_MAX_EDGE",
        "THUMBNAIL_QUALITY",
        "THUMBNAIL_CACHE_ENABLED",
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
            NEWLY_EDITABLE_KEYS
            | STAGE_TWO_EDITABLE_KEYS
            | STAGE_THREE_EDITABLE_KEYS
            | DISPLAY_WINDOW_KEYS
            | WEATHER_KEYS
            | ONTHISDAY_SOURCE_KEYS
            | CURATION_KEYS
            | MODEL_ROUTING_KEYS
            | PREVIOUSLY_EDITABLE_KEYS,
            hot_editable,
        )
        for key in (
            NEWLY_EDITABLE_KEYS
            | STAGE_TWO_EDITABLE_KEYS
            | STAGE_THREE_EDITABLE_KEYS
            | DISPLAY_WINDOW_KEYS
            | WEATHER_KEYS
            | ONTHISDAY_SOURCE_KEYS
            | CURATION_KEYS
        ):
            definition = SETTING_REGISTRY[key]
            self.assertTrue(definition.editable, key)
            self.assertFalse(definition.restart_required, key)
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
        # 载体键刻意选非模型类配置：模型接入已经完全移出注册表，这些用例测的是
        # 「来源解析、热更新、快照、审计」这套通用机制，与具体是哪一项无关。
        self.environment = {
            "WORLD_CITIES_CSV": "./data/env-cities.csv",
            "CITY_MAX_DISTANCE_KM": "300",
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
        """验证改分析类配置后新进程与新任务快照都取到新值，环境值被覆盖。"""
        service = self.service()
        self.assertEqual("./data/env-cities.csv", service.get("WORLD_CITIES_CSV"))
        state = service.list_admin_settings()
        entry = next(
            item for item in state["settings"] if item["key"] == "WORLD_CITIES_CSV"
        )
        self.assertEqual("environment", entry["source"])
        self.assertTrue(entry["editable"])

        service.update_batch(
            {"WORLD_CITIES_CSV": "./data/hot-cities.csv"}, state["version"], self.actor
        )

        worker_view = self.service()
        self.assertEqual("./data/hot-cities.csv", worker_view.get("WORLD_CITIES_CSV"))
        _, snapshot_json = worker_view.task_snapshot("analysis")
        self.assertEqual(
            "./data/hot-cities.csv",
            json.loads(snapshot_json)["settings"]["WORLD_CITIES_CSV"],
        )
        entry = next(
            item
            for item in worker_view.list_admin_settings()["settings"]
            if item["key"] == "WORLD_CITIES_CSV"
        )
        self.assertEqual("database", entry["source"])

        audit = worker_view.list_admin_audit(10)[0]
        self.assertEqual(self.user_id, audit["modified_by_user_id"])
        self.assertEqual("test-admin", audit["modified_by_username"])
        self.assertEqual(
            [{
                "key": "WORLD_CITIES_CSV", "name": "城市索引路径",
                "old_value": "./data/env-cities.csv",
                "new_value": "./data/hot-cities.csv",
            }],
            audit["changes"],
        )

    def test_retired_model_keys_are_gone_from_the_registry(self) -> None:
        """模型接入的五个兜底键必须彻底退出注册表，避免两套并行配置来源。

        留着它们就会出现「改了配置却没生效」：页面上能改，执行侧却只看厂商档案。
        """
        for key in RETIRED_SNAPSHOT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, SETTING_REGISTRY)
                self.assertNotIn(key, self.service().snapshot("analysis")["settings"])

    def test_task_snapshot_still_accepts_retired_keys_from_history(self) -> None:
        """历史快照里带着退役键仍能解析，否则一次配置清理会打死全部在飞任务。

        快照是任务首次认领时固化的。原先 settings 走精确相等校验，从注册表删键会让
        队列里所有已认领任务立刻判 invalid_config_snapshot。
        """
        service = self.service()
        version, snapshot_json = service.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        # 模拟改造前固化的快照：settings 里多出那五个已退役的键
        snapshot["settings"].update({
            "API_URL": "http://legacy.example.com/v1/chat/completions",
            "MODEL_NAME": "legacy-model",
            "TIMEOUT": 600,
            "VLM_MAX_LONG_EDGE": 2560,
        })
        job = {
            "config_version": version,
            "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        }

        resolved = service.resolve_task_snapshot(job, "analysis")

        # 解析成功，且退役键不会被塞回结果——执行侧已经不认它们
        for key in ("API_URL", "MODEL_NAME", "TIMEOUT", "VLM_MAX_LONG_EDGE"):
            self.assertNotIn(key, resolved)
        self.assertIn("WORLD_CITIES_CSV", resolved)

    def test_database_with_retired_keys_still_loads(self) -> None:
        """数据库里留着退役键的旧值时，配置服务必须照常启动。

        这条是真实环境踩出来的：任何曾在后台改过模型配置的部署，`settings_json` 里
        就留着那几个键。`_validated_state` 原先对不在注册表的键判「数据库包含未知
        配置」并抛错，于是升级后配置服务读不出任何配置、服务根本起不来——而问题只是
        一份已经不再使用的旧值。测试库是干净的，所以全套测试通过也发现不了。
        """
        with self.database() as connection:
            connection.execute(
                "UPDATE app_settings SET settings_json=? WHERE id=1",
                (json.dumps({
                    "API_URL": "http://legacy.example.com/v1/chat/completions",
                    "MODEL_NAME": "legacy-model",
                    "API_KEY": "sk-legacy",
                    "TIMEOUT": 600,
                    "VLM_MAX_LONG_EDGE": 2560,
                    "PROJECT_NAME": "仍然有效的配置",
                }, ensure_ascii=False),),
            )

        service = self.service()

        # 退役键被忽略，同一份 JSON 里的有效配置照常生效
        self.assertEqual("仍然有效的配置", service.get("PROJECT_NAME"))
        for key in RETIRED_SNAPSHOT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, service.snapshot("analysis")["settings"])
        # 真正的未知键仍要报错，宽容不能扩大到「什么键都收」
        with self.database() as connection:
            connection.execute(
                "UPDATE app_settings SET settings_json=? WHERE id=1",
                (json.dumps({"SOME_TYPO_KEY": "x"}),),
            )
        with self.assertRaises(RuntimeError):
            self.service().get("PROJECT_NAME")

    def test_task_snapshot_still_rejects_genuinely_unknown_keys(self) -> None:
        """退役键白名单不能变成「什么键都收」，未知键仍须判失败。"""
        service = self.service()
        version, snapshot_json = service.task_snapshot("analysis")
        snapshot = json.loads(snapshot_json)
        snapshot["settings"]["SOME_TYPO_KEY"] = "x"
        job = {
            "config_version": version,
            "config_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        }

        with self.assertRaises(ValueError):
            service.resolve_task_snapshot(job, "analysis")

    def test_environment_overridden_flag_marks_shadowed_env_vars(self) -> None:
        """验证元数据能区分「值来自启动环境」与「环境值已被在线配置压住」。

        `source` 只报胜出方，看不出数据库覆盖压住了同名环境变量，页面因此无法
        提示「改了 .env 重启却没生效」。`environment_overridden` 专门补这一点。
        """
        service = self.service()
        entries = {
            item["key"]: item for item in service.list_admin_settings()["settings"]
        }
        # CITY_MAX_DISTANCE_KM 来自启动环境，尚未被在线覆盖。
        self.assertEqual("environment", entries["CITY_MAX_DISTANCE_KM"]["source"])
        self.assertFalse(entries["CITY_MAX_DISTANCE_KM"]["environment_overridden"])
        # DISPLAY_MIN_SCORE 不在启动环境里，取注册默认值。
        self.assertEqual("default", entries["DISPLAY_MIN_SCORE"]["source"])
        self.assertFalse(entries["DISPLAY_MIN_SCORE"]["environment_overridden"])

        version = service.list_admin_settings()["version"]
        service.update_batch(
            {"CITY_MAX_DISTANCE_KM": 420.0, "DISPLAY_MIN_SCORE": 60.0}, version, self.actor
        )

        entries = {
            item["key"]: item for item in self.service().list_admin_settings()["settings"]
        }
        # 两项来源都变成 database，但只有 CITY_MAX_DISTANCE_KM 压住了一个环境变量。
        self.assertEqual("database", entries["CITY_MAX_DISTANCE_KM"]["source"])
        self.assertTrue(entries["CITY_MAX_DISTANCE_KM"]["environment_overridden"])
        self.assertEqual("database", entries["DISPLAY_MIN_SCORE"]["source"])
        self.assertFalse(entries["DISPLAY_MIN_SCORE"]["environment_overridden"])
        # 只读项不受数据库影响，也不会被标成被覆盖。
        self.assertFalse(entries["DB_PATH"]["environment_overridden"])

    def test_environment_keys_separate_real_env_from_startup_defaults(self) -> None:
        """验证 environment_keys 能把「部署方设过」和「只是启动默认值」分开。

        Web 进程把 Flask 配置默认值一起当作启动值传进来，若不额外声明真正来自
        进程环境的键，页面会把几乎每一项都说成「来自环境变量」，等于没有信息。
        """
        service = ConfigurationService(
            self.database_path,
            environment={**self.environment, "DISPLAY_MIN_SCORE": "80"},
            environment_keys=["WORLD_CITIES_CSV"],
        )
        entries = {
            item["key"]: item for item in service.list_admin_settings()["settings"]
        }

        self.assertTrue(entries["WORLD_CITIES_CSV"]["from_environment"])
        # 启动值照旧生效，只是不再被当成部署方显式设置。
        self.assertEqual(80.0, entries["DISPLAY_MIN_SCORE"]["value"])
        self.assertEqual("environment", entries["DISPLAY_MIN_SCORE"]["source"])
        self.assertFalse(entries["DISPLAY_MIN_SCORE"]["from_environment"])
        self.assertFalse(entries["DISPLAY_MIN_SCORE"]["environment_overridden"])

    def test_no_editable_sensitive_setting_remains(self) -> None:
        """注册表里不应再有可在线编辑的敏感项。

        `API_KEY` 曾是唯一一个。模型密钥现在存在厂商档案里，只写不读的语义与用例都
        移到了 `tests/test_model_providers.py`。剩下的敏感项（会话密钥、设备下载密钥）
        只能改部署环境，不经这条在线路径，因此这里钉住「没有可编辑敏感项」这个前提——
        一旦有人新加了可编辑敏感项，就必须同时补回在线脱敏的用例。
        """
        editable_sensitive = sorted(
            key
            for key, definition in SETTING_REGISTRY.items()
            if definition.sensitive and definition.editable
        )

        self.assertEqual([], editable_sensitive)

    def test_stale_version_raises_conflict_without_writing(self) -> None:
        """验证基于过期版本的提交冲突且不覆盖已有修改。"""
        service = self.service()
        version = service.list_admin_settings()["version"]
        service.update_batch({"FONT_PATH": "/fonts/first.ttf"}, version, self.actor)

        with self.assertRaises(ConfigurationConflictError):
            self.service().update_batch(
                {"FONT_PATH": "/fonts/second.ttf"}, version, self.actor
            )
        self.assertEqual("/fonts/first.ttf", self.service().get("FONT_PATH"))

    def test_invalid_values_are_rejected_and_not_persisted(self) -> None:
        """验证超范围、错类型与非枚举值全部被拒绝且不落库。"""
        service = self.service()
        version = service.list_admin_settings()["version"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            service.update_batch(
                {
                    "CITY_GRID_DEG": 0,
                    "HOME_LAT": "31.5",
                    "CITY_MAX_DISTANCE_KM": 99999,
                    "ONTHISDAY_STRATEGY": "nope",
                },
                version,
                self.actor,
            )

        self.assertEqual(
            {"CITY_GRID_DEG", "HOME_LAT", "CITY_MAX_DISTANCE_KM", "ONTHISDAY_STRATEGY"},
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

    def test_page_renders_editable_inputs_and_hides_locked_keys(self) -> None:
        """验证放开项渲染为可编辑控件，只读与敏感项不出现可提交的输入框。

        密钥类断言已经移走：注册表里不再有可在线编辑的敏感项，模型密钥归厂商档案，
        只写语义与不回显的用例在 `tests/test_model_providers.py`。
        """
        app = self.application()
        with app.test_request_context("/admin/settings"):
            html = render_template("admin/settings.html", **_settings_context())

        for key in sorted(NEWLY_EDITABLE_KEYS):
            self.assertIn(f'name="{key}"', html)
        for key in sorted(DISPLAY_WINDOW_KEYS):
            self.assertIn(f'name="{key}"', html)
        self.assertNotIn("page-secret-value", html)
        for key in ("DB_PATH", "SECRET_KEY", "FLASK_PORT", "SESSION_COOKIE_SECURE"):
            self.assertNotIn(f'name="{key}"', html)
        # 退役的模型兜底键不应还有输入框，否则等于两套并行配置来源又回来了
        for key in sorted(RETIRED_SNAPSHOT_KEYS):
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

    def test_form_parsing_covers_every_editable_key(self) -> None:
        """验证表单解析纳入全部可编辑项，且不接收只读或已退役的键。

        原先这条用例还负责「留空密钥视为不变」，那个语义随 `API_KEY` 移到了厂商档案，
        对应用例在 `tests/test_model_providers.py`。
        """
        app = self.application()
        with app.test_request_context("/admin/settings"):
            state = _settings_context()["state"]
        form = {"expected_version": str(state["version"])}
        for item in state["settings"]:
            if not item["editable"] or item["sensitive"]:
                continue
            # 必须回填 display_value 而不是 value：带显示单位的项（按 MiB 填写的上传
            # 体积上限）在表单里就是显示单位，拿字节去填等于把 64 MiB 当成 64 MiB×1048576。
            # 无单位的项两者相等，因此这里统一用 display_value。
            value = item["display_value"]
            form[item["key"]] = "true" if value is True else "false" if value is False else str(value)

        with app.test_request_context("/admin/settings", method="POST", data=dict(form)):
            version, changes = _parse_settings_form()

        self.assertEqual(state["version"], version)
        self.assertIn("PROJECT_NAME", changes)
        self.assertIn("ANALYSIS_PROVIDER", changes)
        for key in sorted(RETIRED_SNAPSHOT_KEYS):
            self.assertNotIn(key, changes)

        # 即使有人手工往表单里塞退役键，解析也不该收下它
        injected = {key: "x" for key in RETIRED_SNAPSHOT_KEYS}
        with app.test_request_context(
            "/admin/settings", method="POST", data={**form, **injected}
        ):
            _, retired_changes = _parse_settings_form()
        for key in sorted(RETIRED_SNAPSHOT_KEYS):
            self.assertNotIn(key, retired_changes)


class SettingsTabLayoutTestCase(TemporaryDatabaseTestCase):
    """校验配置页分类标签的覆盖完整性、互斥性与渲染结果。"""

    def build_tabs(self):
        """在应用上下文中构造分类标签，返回标签列表。"""
        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            return _settings_context()["tabs"]

    def test_every_registered_setting_is_categorised_exactly_once(self) -> None:
        """验证注册表中每个配置项都被分类，且不会重复出现在多个标签。"""
        tabs = self.build_tabs()
        placed: list[str] = [
            item["key"]
            for tab in tabs
            for section in tab["sections"]
            for item in section["entries"]
        ]

        self.assertEqual(sorted(SETTING_REGISTRY), sorted(placed))
        self.assertEqual(len(placed), len(set(placed)))
        # 出现「未分类」段说明有新配置项漏登记到 _SETTINGS_TAB_LAYOUT。
        labels = [section["label"] for tab in tabs for section in tab["sections"]]
        self.assertNotIn("未分类", labels)

    def test_routing_fields_offer_provider_names_instead_of_free_typing(self) -> None:
        """用途路由必须给出可选档案名，不能让人凭记忆手打。

        值是厂商档案名称，打错一个字要等保存时才报错，而且用户根本不知道有哪些档案
        可选。这里钉住两件事：补全列表来自真实的启用档案，且渲染出可点击填入的标签。
        """
        app = create_app(self.application_config())
        with app.app_context():
            providers = app.extensions["inktime_services"]["model_providers"]
            admin_id = self.create_admin_user("suggest-admin")
            providers.create_provider(
                {
                    "name": "千问", "base_url": "https://dashscope.example.com/v1",
                    "model_name": "qwen-vl-max", "api_key": "",
                    "timeout_seconds": 600, "max_long_edge": 2560, "is_enabled": True,
                },
                admin_id, "suggest-admin",
            )
            providers.create_provider(
                {
                    "name": "停用的", "base_url": "https://off.example.com/v1",
                    "model_name": "off-vlm", "api_key": "",
                    "timeout_seconds": 600, "max_long_edge": 2560, "is_enabled": False,
                },
                admin_id, "suggest-admin",
            )

        with app.test_request_context("/admin/settings"):
            context = _settings_context()
            html = render_template("admin/settings.html", **context)

        entries = {item["key"]: item for item in context["state"]["settings"]}
        for key in ("ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER"):
            with self.subTest(key=key):
                # 只提示启用中的档案：停用的档案填进去保存会被拒绝
                self.assertEqual(["千问"], entries[key]["suggestions"])
                self.assertIn(f'list="suggest-{key}"', html)
                self.assertIn(f'id="suggest-{key}"', html)
                self.assertIn(f'data-fill-target="setting-{key}"', html)
        self.assertIn('data-fill-value="千问"', html)
        self.assertNotIn('data-fill-value="停用的"', html)

    def test_non_routing_fields_get_no_suggestion_list(self) -> None:
        """补全只挂在用途路由上，别的文本项不受影响。"""
        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            entries = {
                item["key"]: item for item in _settings_context()["state"]["settings"]
            }

        self.assertNotIn("suggestions", entries["PROJECT_NAME"])
        self.assertNotIn("suggestions", entries["FONT_PATH"])

    def test_model_tab_no_longer_offers_a_single_model_endpoint_section(self) -> None:
        """配置页不应再有「兼容模型接口」分段。

        那一段是模型接入只能存一套时代的入口。它和「模型厂商」页构成两套并行来源，
        「改了配置却没生效」正是这么来的，因此随注册表五个键一并移除。模型标签页
        现在只负责用途路由和与模型无关的分析参数。
        """
        tabs = self.build_tabs()
        model_tab = next(tab for tab in tabs if tab["id"] == "model")
        labels = [section["label"] for section in model_tab["sections"]]

        self.assertNotIn("兼容模型接口", labels)
        self.assertIn("用途路由", labels)
        routing = next(
            section for section in model_tab["sections"] if section["label"] == "用途路由"
        )
        self.assertEqual(
            ["ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER"],
            [item["key"] for item in routing["entries"]],
        )

    def test_system_and_security_tab_is_last_settings_tab(self) -> None:
        """验证只读的系统与安全项集中在最后一个带配置项的标签，其后只有纯记录标签。"""
        tabs = self.build_tabs()
        settings_tabs = [tab for tab in tabs if tab["sections"]]

        self.assertEqual("audit", tabs[-1]["id"], "配置审计是不含配置项的末位标签")
        self.assertEqual((), tuple(tabs[-1]["sections"]))
        self.assertEqual(0, tabs[-1]["count"])
        self.assertEqual("system", settings_tabs[-1]["id"])
        self.assertEqual(0, settings_tabs[-1]["editable_count"])
        system_keys = {
            item["key"]
            for section in settings_tabs[-1]["sections"]
            for item in section["entries"]
        }
        for key in ("SECRET_KEY", "DOWNLOAD_KEY", "DB_PATH", "SESSION_COOKIE_SECURE"):
            self.assertIn(key, system_keys)

    def test_page_renders_one_visible_panel_and_all_inputs(self) -> None:
        """验证页面渲染出全部标签、仅首个面板可见，且所有可编辑项都在同一表单内。"""
        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            context = _settings_context()
            html = render_template("admin/settings.html", **context)

        for tab in context["tabs"]:
            self.assertIn(f'id="settings-tab-{tab["id"]}"', html)
            self.assertIn(f'id="settings-panel-{tab["id"]}"', html)
        # 首个面板默认展开，其余面板带 hidden 属性等待脚本切换。
        panels = re.findall(
            r'<section class="settings-panel"[^>]*id="settings-panel-([a-z]+)"[^>]*?(hidden)?>',
            html,
            re.S,
        )
        self.assertEqual(len(context["tabs"]), len(panels))
        self.assertEqual("", panels[0][1])
        self.assertTrue(all(panel[1] == "hidden" for panel in panels[1:]))
        self.assertEqual(1, html.count('aria-selected="true"'))
        self.assertEqual(len(context["tabs"]) - 1, html.count('aria-selected="false"'))
        editable = [
            key
            for key, definition in SETTING_REGISTRY.items()
            if definition.editable and not definition.restart_required
        ]
        for key in editable:
            self.assertIn(f'name="{key}"', html)
        # 全部配置项共用一个表单，保存不受当前标签影响。
        self.assertEqual(1, html.count('id="settings-form"'))

    def test_validation_error_marks_owning_tab(self) -> None:
        """验证校验失败会在对应标签上标出错误数量，便于直接跳到出错分类。"""
        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            context = _settings_context(fields={"CITY_GRID_DEG": "必须是数字"})

        by_id = {tab["id"]: tab for tab in context["tabs"]}
        self.assertEqual(1, by_id["model"]["error_count"])
        self.assertEqual(0, by_id["display"]["error_count"])
