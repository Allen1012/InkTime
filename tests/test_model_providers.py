"""验证模型厂商档案的存储、校验、脱敏与连通性测试。

改造背景：模型接入配置原先只能存一套，接公司自建、千问、豆包必须互相覆盖。档案放独立
表而不是配置注册表，根本原因是任务快照会断言「快照键集合与注册表非敏感键精确相等」，
往注册表加动态键会让所有历史任务的旧快照立即失效。

本阶段只负责「存下来」，因此这里**不**验证分析链路真的用上了档案——那是下一阶段的事。
本阶段反过来要验证一条：不建任何档案时旧链路完全不受影响。

最要紧的一组断言是密钥不外泄：既不进列表接口、不进页面，也不进审计表。密钥一旦落库，
清理成本远高于事前防住，因此这里从存储层、服务层、页面三个方向都钉一遍。
"""

from __future__ import annotations

import json
import re

from flask import render_template

from src.configuration import (
    REDACTED_TEXT,
    ConfigurationActor,
    is_sensitive_key,
    redact_sensitive_values,
    redact_settings_audit_history,
)
from src.migrations import DEFAULT_MIGRATIONS_DIR
from src.server.app import create_app
from src.server.errors import ConflictError, ParameterError, ResourceNotFoundError
from src.server.model_providers import ModelProviderService, resolve_endpoint
from src.server.repositories.model_provider_repository import (
    PUBLIC_COLUMNS,
    ModelProviderRepository,
)
from tests.support import TemporaryDatabaseTestCase

ADMIN_USERNAME = "provider-admin"
ADMIN_PASSWORD = "inktime-provider-password"
SECRET = "sk-test-secret-value-1234"


def _provider_values(name: str = "千问", **overrides) -> dict:
    """构造一份合法的厂商字段，便于用例只覆盖关心的字段。"""
    values = {
        "name": name,
        "base_url": "https://dashscope.example.com/compatible-mode/v1",
        "model_name": "qwen3-vl-30b-a3b-instruct",
        "api_key": SECRET,
        "timeout_seconds": 600,
        "max_long_edge": 2560,
        "is_enabled": True,
    }
    values.update(overrides)
    return values


class ProviderStorageTestCase(TemporaryDatabaseTestCase):
    """验证档案增删改查、乐观锁与唯一名约束。"""

    def setUp(self) -> None:
        """准备应用、厂商服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"]["model_providers"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _create(self, **overrides) -> dict:
        """新建一条档案。"""
        return self.service.create_provider(
            _provider_values(**overrides), self.admin_id, ADMIN_USERNAME
        )

    def test_create_persists_and_lists_provider(self) -> None:
        """新建的档案能被列出，字段原样保留。"""
        created = self._create()

        listed = self.service.list_providers()

        self.assertEqual(1, len(listed))
        self.assertEqual("千问", listed[0]["name"])
        self.assertEqual(600, listed[0]["timeout_seconds"])
        self.assertEqual(1, listed[0]["is_enabled"])
        self.assertEqual(created["id"], listed[0]["id"])

    def test_multiple_providers_coexist(self) -> None:
        """三套配置能同时存在，这正是本次改造要解决的核心问题。"""
        self._create(name="公司", base_url="http://10.0.0.2:1234/v1")
        self._create(name="千问")
        self._create(name="豆包", base_url="https://ark.example.com/api/v3")

        names = [item["name"] for item in self.service.list_providers()]

        self.assertEqual(["公司", "千问", "豆包"], sorted(names, key=names.index))
        self.assertEqual(3, len(names))

    def test_duplicate_name_is_rejected(self) -> None:
        """名称唯一：路由键按名字引用档案，重名会让引用产生歧义。"""
        self._create(name="千问")

        with self.assertRaises(ConflictError):
            self._create(name="千问")

    def test_update_requires_matching_version(self) -> None:
        """乐观锁：版本不符时拒绝更新。"""
        created = self._create()

        self.service.update_provider(
            created["id"], created["version"], {"timeout_seconds": 300},
            self.admin_id, ADMIN_USERNAME,
        )

        with self.assertRaises(ConflictError):
            self.service.update_provider(
                created["id"], created["version"], {"timeout_seconds": 120},
                self.admin_id, ADMIN_USERNAME,
            )

    def test_update_bumps_version_and_applies_change(self) -> None:
        """更新成功后版本递增，改动落库。"""
        created = self._create()

        updated = self.service.update_provider(
            created["id"], created["version"], {"timeout_seconds": 300},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual(created["version"] + 1, updated["version"])
        self.assertEqual(300, updated["timeout_seconds"])

    def test_rename_is_rejected(self) -> None:
        """禁止改名：路由键按名字引用，改名会留下悬空引用且没有任何提示。"""
        created = self._create(name="千问")

        with self.assertRaises(ParameterError) as captured:
            self.service.update_provider(
                created["id"], created["version"], {"name": "阿里千问"},
                self.admin_id, ADMIN_USERNAME,
            )

        self.assertIn("不支持修改", str(captured.exception))

    def test_submitting_same_name_is_not_treated_as_rename(self) -> None:
        """页面可能原样回填名称，提交同名不该被当成改名而报错。"""
        created = self._create(name="千问")

        updated = self.service.update_provider(
            created["id"], created["version"],
            {"name": "千问", "timeout_seconds": 120},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual(120, updated["timeout_seconds"])

    def test_delete_removes_provider_and_keeps_audit(self) -> None:
        """删除后档案消失，但审计仍能查到是谁删的。"""
        created = self._create()

        self.service.delete_provider(
            created["id"], created["version"], self.admin_id, ADMIN_USERNAME
        )

        self.assertEqual([], self.service.list_providers())
        actions = [row["action"] for row in self.service.list_audit()]
        self.assertIn("provider_delete", actions)

    def test_update_and_delete_reject_missing_provider(self) -> None:
        """对不存在的档案操作要报 404 而不是静默成功。"""
        with self.assertRaises(ResourceNotFoundError):
            self.service.update_provider(
                999, 1, {"timeout_seconds": 60}, self.admin_id, ADMIN_USERNAME
            )
        with self.assertRaises(ResourceNotFoundError):
            self.service.delete_provider(999, 1, self.admin_id, ADMIN_USERNAME)

    def test_disabled_provider_does_not_resolve(self) -> None:
        """停用与不存在等价：调用方两种情况都回退兜底配置。"""
        created = self._create()
        self.service.update_provider(
            created["id"], created["version"], {"is_enabled": False},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertIsNone(self.service.resolve("千问"))
        self.assertEqual("", self.service.api_key_for("千问"))

    def test_resolve_chain_keeps_order_and_skips_unusable(self) -> None:
        """降级候选按配置顺序返回，不可用的跳过而不是让整条链失效。"""
        self._create(name="千问")
        self._create(name="公司", base_url="http://10.0.0.2:1234/v1")

        chain = self.service.resolve_chain("公司;不存在的;千问;公司")

        self.assertEqual(["公司", "千问"], [item["name"] for item in chain])

    def test_multiple_models_are_normalized_on_one_provider(self) -> None:
        """一个厂商可配多个模型，存回时去掉空格、空项与重复项。"""
        created = self._create(
            model_name="  qwen-vl-max ;; qwen3-vl-30b ;qwen-vl-max ;  "
        )

        self.assertEqual("qwen-vl-max;qwen3-vl-30b", created["model_name"])

    def test_blank_model_list_is_rejected(self) -> None:
        """只填分隔符等于没填模型名，必须拒绝而不是存下一条不可用档案。"""
        with self.assertRaises(ParameterError):
            self._create(model_name=" ; ; ")

    def test_new_provider_activates_first_model_by_default(self) -> None:
        """没指定启用模型时默认用模型池第一个，不留空值让执行期无模型可用。"""
        created = self._create(model_name="model-a;model-b;model-c")

        self.assertEqual("model-a", created["active_model"])

    def test_active_model_can_be_chosen_explicitly(self) -> None:
        """可以在新建时直接指定启用池里的哪一个模型。"""
        created = self._create(model_name="model-a;model-b", active_model="model-b")

        self.assertEqual("model-b", created["active_model"])

    def test_active_model_outside_pool_is_rejected(self) -> None:
        """显式选了池外的模型必须报错：那是提交与页面不一致，不能静默改成别的。"""
        with self.assertRaises(ParameterError):
            self._create(model_name="model-a;model-b", active_model="model-z")

    def test_switching_active_model_only_touches_that_field(self) -> None:
        """切换启用模型不需要重新提交模型池与连接参数。"""
        created = self._create(model_name="model-a;model-b")

        updated = self.service.update_provider(
            created["id"], created["version"], {"active_model": "model-b"},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual("model-b", updated["active_model"])
        self.assertEqual("model-a;model-b", updated["model_name"])
        self.assertEqual(created["base_url"], updated["base_url"])

    def test_shrinking_pool_falls_back_to_first_remaining_model(self) -> None:
        """把正在启用的模型从池里删掉时落回新池第一项，而不是拒绝保存。

        否则用户得先切到别的模型、再改池，两步才能完成一次编辑。
        """
        created = self._create(model_name="model-a;model-b", active_model="model-b")

        updated = self.service.update_provider(
            created["id"], created["version"], {"model_name": "model-c;model-d"},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual("model-c", updated["active_model"])

    def test_shrinking_pool_keeps_active_model_when_still_present(self) -> None:
        """改池但启用模型仍在新池内时保持不动，不无谓地跳回第一项。"""
        created = self._create(model_name="model-a;model-b", active_model="model-b")

        updated = self.service.update_provider(
            created["id"], created["version"], {"model_name": "model-b;model-x"},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual("model-b", updated["active_model"])

    def test_resolve_chain_uses_active_model_only(self) -> None:
        """每个厂商只产出一个候选，用的是它当前启用的模型。

        厂商的多个模型是手动备选项而不是自动降级候选：各模型授权额度不同，
        自动轮着调用会让额度以不可预期的方式被消耗掉。厂商之间的降级链保持不变。
        """
        self._create(name="千问", model_name="qwen-vl-max;qwen3-vl-30b", active_model="qwen3-vl-30b")
        self._create(name="公司", base_url="http://10.0.0.2:1234/v1", model_name="local-vlm")

        chain = self.service.resolve_chain("千问;公司")

        self.assertEqual(
            [("千问", "qwen3-vl-30b"), ("公司", "local-vlm")],
            [(item["name"], item["model_name"]) for item in chain],
        )

    def test_resolve_chain_candidate_keeps_provider_fields(self) -> None:
        """候选保留档案的编号、版本与连接参数，只把模型名收敛为启用的那个。"""
        created = self._create(name="千问", model_name="model-a;model-b", active_model="model-b")

        candidate, = self.service.resolve_chain("千问")

        for field in ("id", "name", "version", "base_url", "timeout_seconds", "max_long_edge"):
            self.assertEqual(created[field], candidate[field])
        self.assertEqual("model-b", candidate["model_name"])

    def test_active_model_falls_back_when_column_value_is_unusable(self) -> None:
        """启用模型列为空或指向池外时按池首项执行，而不是让整条链失效。

        存量库在回填前、或有人直接用 sqlite3 改过库时会出现这种取值。
        """
        self.assertEqual(
            "model-a",
            self.service.active_model_of({"model_name": "model-a;model-b", "active_model": ""}),
        )
        self.assertEqual(
            "model-a",
            self.service.active_model_of(
                {"model_name": "model-a;model-b", "active_model": "已删掉的模型"}
            ),
        )

    def test_backfill_migration_activates_first_pool_entry(self) -> None:
        """迁移 0057 把存量档案的启用模型回填为模型池第一项。

        直接执行迁移文件里的语句而不是复述一遍逻辑：`INSTR`、`SUBSTR` 的边界（池里
        只有一个模型时没有分号）正是这条 SQL 唯一容易写错的地方，复述等于不测。
        """
        backfill = next(
            DEFAULT_MIGRATIONS_DIR.glob("0057_*.sql")
        ).read_text(encoding="utf-8")
        with self.database() as connection:
            for name, pool in (("多模型存量", "model-a;model-b"), ("单模型存量", "only-model")):
                connection.execute(
                    "INSERT INTO model_providers (name,base_url,model_name,active_model,"
                    "api_key,api_key_hint,timeout_seconds,max_long_edge,is_enabled,version,"
                    "created_at,updated_at) VALUES (?,?,?,'','','',600,2560,1,1,?,?)",
                    (name, "https://legacy.example.com/v1", pool, "2026-01-01", "2026-01-01"),
                )
            connection.execute(backfill)
            rows = {
                row["name"]: row["active_model"]
                for row in connection.execute(
                    "SELECT name,active_model FROM model_providers"
                ).fetchall()
            }

        self.assertEqual("model-a", rows["多模型存量"])
        self.assertEqual("only-model", rows["单模型存量"])

    def test_list_providers_keeps_raw_model_pool(self) -> None:
        """列表接口同时返回模型池原始串与当前启用模型，页面据此渲染下拉。"""
        self._create(model_name="model-a;model-b", active_model="model-b")

        listed = self.service.list_providers()[0]

        self.assertEqual("model-a;model-b", listed["model_name"])
        self.assertEqual("model-b", listed["active_model"])


class ProviderSecretTestCase(TemporaryDatabaseTestCase):
    """验证密钥既不外泄到读取路径，也不落进审计表。"""

    def setUp(self) -> None:
        """准备应用、厂商服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"]["model_providers"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)
        self.created = self.service.create_provider(
            _provider_values(), self.admin_id, ADMIN_USERNAME
        )

    def test_public_columns_exclude_the_secret(self) -> None:
        """仓储的公开列清单里不能有 api_key。

        这一条钉的是机制而非某个调用点：只要公开列不含密钥，「把整行档案塞进接口响应」
        就不会泄露，而不是靠每个调用方自己记得脱敏。
        """
        self.assertNotIn("api_key", PUBLIC_COLUMNS)
        self.assertIn("api_key_hint", PUBLIC_COLUMNS)

    def test_listing_returns_only_the_hint(self) -> None:
        """列表只给末四位，不给原值。"""
        listed = self.service.list_providers()[0]

        self.assertNotIn("api_key", listed)
        self.assertEqual(SECRET[-4:], listed["api_key_hint"])
        self.assertNotIn(SECRET, json.dumps(listed, ensure_ascii=False))

    def test_secret_is_readable_only_through_the_dedicated_method(self) -> None:
        """密钥只能通过专用方法取到，供执行时现读现传。"""
        self.assertEqual(SECRET, self.service.api_key_for("千问"))

    def test_audit_never_stores_the_secret(self) -> None:
        """厂商审计表里不得出现密钥原值。

        脱敏必须发生在写入前：数据库备份、sqlite3 直连与误导出都会绕过展示层。
        """
        self.service.update_provider(
            self.created["id"], self.created["version"],
            {"api_key": "sk-rotated-secret-value-9999"},
            self.admin_id, ADMIN_USERNAME,
        )

        with self.database() as connection:
            rows = connection.execute(
                "SELECT old_values_json,new_values_json FROM model_provider_audit"
            ).fetchall()
        blob = " ".join(row[0] + row[1] for row in rows)

        self.assertNotIn(SECRET, blob)
        self.assertNotIn("sk-rotated-secret-value-9999", blob)
        self.assertIn(REDACTED_TEXT, blob)

    def test_blank_secret_keeps_the_existing_value(self) -> None:
        """密钥留空表示保持原值。

        页面不回显密钥，若把空串当成「清空」，用户改一次超时就会顺手把密钥抹掉。
        """
        self.service.update_provider(
            self.created["id"], self.created["version"],
            {"api_key": "", "timeout_seconds": 120},
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual(SECRET, self.service.api_key_for("千问"))

    def test_page_does_not_render_the_secret(self) -> None:
        """厂商页面 HTML 里不得出现密钥原值。"""
        with self.app.test_request_context("/admin/providers"):
            from src.server.blueprints.admin import _providers_context

            html = render_template("admin/providers.html", **_providers_context())

        self.assertNotIn(SECRET, html)
        self.assertIn(SECRET[-4:], html)


class SettingsAuditRedactionTestCase(TemporaryDatabaseTestCase):
    """验证配置审计不再把敏感值写成明文。"""

    def setUp(self) -> None:
        """准备应用与配置服务。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.configuration = self.app.extensions["inktime_services"]["configuration"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)
        self.actor = ConfigurationActor(self.admin_id, ADMIN_USERNAME)

    def test_sensitive_key_detection_covers_registry_and_name_markers(self) -> None:
        """敏感判定既看注册表标记，也看键名片段作兜底。"""
        self.assertTrue(is_sensitive_key("API_KEY"))
        self.assertTrue(is_sensitive_key("SECRET_KEY"))
        self.assertTrue(is_sensitive_key("SOME_NEW_TOKEN"))
        self.assertFalse(is_sensitive_key("MODEL_NAME"))

    def test_redact_replaces_only_sensitive_values(self) -> None:
        """脱敏只动敏感键，其余值原样保留。"""
        result = redact_sensitive_values({"API_KEY": "sk-abc", "MODEL_NAME": "m"})

        self.assertEqual(REDACTED_TEXT, result["API_KEY"])
        self.assertEqual("m", result["MODEL_NAME"])

    def test_settings_audit_stores_no_plaintext_secret(self) -> None:
        """写入 API_KEY 后，审计表里查不到原值。"""
        self.configuration.update_batch(
            {"API_KEY": "sk-settings-plain-9876"},
            self.configuration.list_admin_settings()["version"],
            self.actor,
        )

        with self.database() as connection:
            rows = connection.execute(
                "SELECT old_values_json,new_values_json FROM app_settings_audit"
            ).fetchall()
        blob = " ".join(row[0] + row[1] for row in rows)

        self.assertNotIn("sk-settings-plain-9876", blob)
        self.assertIn(REDACTED_TEXT, blob)

    def test_non_sensitive_change_is_still_recorded_in_full(self) -> None:
        """非敏感配置的审计必须仍然可读，脱敏不能一刀切。"""
        self.configuration.update_batch(
            {"MODEL_NAME": "audit-visible-model"},
            self.configuration.list_admin_settings()["version"],
            self.actor,
        )

        with self.database() as connection:
            row = connection.execute(
                "SELECT new_values_json FROM app_settings_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertIn("audit-visible-model", row[0])


class ProviderValidationTestCase(TemporaryDatabaseTestCase):
    """验证字段校验，尤其是会影响后续阶段的两条约束。"""

    def setUp(self) -> None:
        """准备应用与厂商服务。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"]["model_providers"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _expect_rejected(self, **overrides) -> str:
        """断言新建被拒并返回报错文本。"""
        with self.assertRaises(ParameterError) as captured:
            self.service.create_provider(
                _provider_values(**overrides), self.admin_id, ADMIN_USERNAME
            )
        return str(captured.exception)

    def test_name_cannot_contain_the_chain_separator(self) -> None:
        """厂商名不能含分号：分号是降级候选串的分隔符，含它会让路由解析产生歧义。"""
        message = self._expect_rejected(name="千问;豆包")

        self.assertIn("分隔", message)

    def test_base_url_must_be_absolute_http(self) -> None:
        """接口地址必须是 http 或 https 绝对地址。"""
        self._expect_rejected(base_url="dashscope.example.com/v1")
        self._expect_rejected(base_url="")

    def test_required_text_fields_are_enforced(self) -> None:
        """名称与模型名不能为空。"""
        self._expect_rejected(name="   ")
        self._expect_rejected(model_name="")

    def test_numeric_bounds_are_enforced(self) -> None:
        """超时与最长边取值范围与注册表同名配置保持一致。"""
        self._expect_rejected(timeout_seconds=0)
        self._expect_rejected(timeout_seconds="abc")
        self._expect_rejected(max_long_edge=64)
        self._expect_rejected(max_long_edge=99999)

    def test_unknown_field_is_rejected(self) -> None:
        """未知字段直接拒绝，避免悄悄忽略调用方的意图。"""
        with self.assertRaises(ParameterError):
            self.service.create_provider(
                {**_provider_values(), "proxy": "http://x"},
                self.admin_id, ADMIN_USERNAME,
            )

    def test_local_provider_may_have_no_secret(self) -> None:
        """本地模型通常不需要密钥，必须允许留空。"""
        created = self.service.create_provider(
            _provider_values(
                name="本地", base_url="http://127.0.0.1:1234/v1", api_key=""
            ),
            self.admin_id, ADMIN_USERNAME,
        )

        self.assertEqual("", created["api_key_hint"])


class ProviderEndpointTestCase(TemporaryDatabaseTestCase):
    """验证接口地址归一化。"""

    def test_base_url_gets_the_chat_completions_suffix(self) -> None:
        """只填到 /v1 时补上对话补全后缀。"""
        self.assertEqual(
            "https://x.example.com/v1/chat/completions",
            resolve_endpoint("https://x.example.com/v1"),
        )
        self.assertEqual(
            "https://x.example.com/v1/chat/completions",
            resolve_endpoint("https://x.example.com/v1/"),
        )

    def test_full_endpoint_is_used_as_is(self) -> None:
        """已经指向对话补全端点时原样使用。

        仓库里这个值历史上有两种写法：批量脚本把它当完整端点直接 POST，OpenAI 兼容
        SDK 分支把它当 base_url。两种都在用，所以按后缀判断而不是强制一种。
        """
        self.assertEqual(
            "http://127.0.0.1:1234/v1/chat/completions",
            resolve_endpoint("http://127.0.0.1:1234/v1/chat/completions"),
        )


class ProviderConnectivityTestCase(TemporaryDatabaseTestCase):
    """验证连通性测试的成功与失败分支，且失败原因不含密钥。"""

    def setUp(self) -> None:
        """准备应用与厂商服务。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"]["model_providers"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def test_unreachable_endpoint_reports_failure(self) -> None:
        """不可达地址报连接失败，而不是抛异常把页面打成 500。

        端口取 1 是为了确定性失败：它低于非特权端口范围，本机不会有服务监听。
        """
        result = self.service.test_connectivity(
            "http://127.0.0.1:1/v1", "any-model", "sk-should-not-leak"
        )

        self.assertFalse(result["ok"])
        self.assertIn("/chat/completions", result["endpoint"])
        self.assertNotIn("sk-should-not-leak", json.dumps(result, ensure_ascii=False))

    def test_missing_model_is_reported_without_a_request(self) -> None:
        """模型名为空时直接给出结论，不必发请求。"""
        result = self.service.test_connectivity("https://x.example.com/v1", "")

        self.assertFalse(result["ok"])
        self.assertIn("模型名", result["message"])

    def test_blank_model_list_is_reported_without_a_request(self) -> None:
        """只填分隔符与没填等价，同样不发请求。"""
        result = self.service.test_connectivity("https://x.example.com/v1", " ; ")

        self.assertFalse(result["ok"])
        self.assertIn("模型名", result["message"])
        self.assertEqual([], result["models"])

    def test_transport_failure_stops_probing_remaining_models(self) -> None:
        """地址不通时只测第一个模型就停止，不按模型个数成倍等待。

        端口取 1 是确定性失败：剩下的模型必然是同一个结果，继续测只是浪费页面等待时间。
        """
        result = self.service.test_connectivity(
            "http://127.0.0.1:1/v1", "model-a;model-b;model-c"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(["model-a"], [item["model_name"] for item in result["models"]])
        self.assertIn("未测试 2 个模型", result["message"])

    def test_single_model_result_keeps_flat_message(self) -> None:
        """单模型档案的结论措辞保持原样，不因多模型支持而变成汇总格式。"""
        result = self.service.test_connectivity("http://127.0.0.1:1/v1", "only-model")

        self.assertFalse(result["ok"])
        self.assertNotIn("个模型连通", result["message"])
        self.assertEqual(1, len(result["models"]))


class ProviderRouteTestCase(TemporaryDatabaseTestCase):
    """通过真实登录会话验证页面与接口。"""

    def logged_in_client(self):
        """创建应用并完成表单登录，返回应用、客户端与登录后有效的令牌。"""
        app = create_app(self.application_config())
        with app.app_context():
            app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )
        client = app.test_client()
        page = client.get("/admin/login")
        login_token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(login_token)
        response = client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": login_token.group(1),
            },
        )
        self.assertIn(response.status_code, (302, 303))
        listing = client.get("/admin/providers").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', listing)
        self.assertIsNotNone(token)
        return app, client, token.group(1)

    def test_page_renders_with_navigation_entry(self) -> None:
        """厂商页可访问，且侧边导航有入口。"""
        _, client, _ = self.logged_in_client()

        body = client.get("/admin/providers").get_data(as_text=True)

        self.assertIn("模型厂商", body)
        self.assertIn("/admin/providers", body)
        self.assertIn("当前兜底配置", body)

    def test_listing_marks_provider_not_referenced_by_any_route(self) -> None:
        """没有任何用途路由引用时明确标注，并提示档案暂时不会被调用。

        「启用」只表示档案可以被引用，不表示它在干活。不把这件事写在页面上，用户会以为
        建档即生效，然后对着一条根本没接上的档案排查分析为什么没走新模型。
        """
        _, client, token = self.logged_in_client()
        self._create_provider_via_form(client, token)

        body = client.get("/admin/providers").get_data(as_text=True)

        self.assertIn("未被引用", body)
        self.assertIn("都没配", body)

    def test_listing_marks_purposes_using_the_provider(self) -> None:
        """被用途路由引用时列出具体用途，并撤掉「都没配」的提示。"""
        app, client, token = self.logged_in_client()
        self._create_provider_via_form(client, token)
        with app.app_context():
            configuration = app.extensions["inktime_services"]["configuration"]
            configuration.update_batch(
                {"ANALYSIS_PROVIDER": "千问", "PANEL_PROVIDER": "千问"},
                configuration.list_admin_settings()["version"],
                ConfigurationActor(self.create_admin_user("route-admin"), "route-admin"),
            )

        body = client.get("/admin/providers").get_data(as_text=True)

        self.assertIn("照片分析", body)
        self.assertIn("信息面板", body)
        self.assertNotIn("未被引用", body)
        self.assertNotIn("都没配", body)

    def test_listing_offers_edit_entry_for_each_provider(self) -> None:
        """列表页给每条档案一个编辑入口：建完之后必须能改，不能只剩删除重建一条路。"""
        _, client, token = self.logged_in_client()
        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "create", "name": "千问",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen3-vl-30b-a3b-instruct", "api_key": SECRET,
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
            },
        )
        with self.database() as connection:
            row = connection.execute(
                "SELECT id FROM model_providers WHERE name='千问'"
            ).fetchone()

        body = client.get("/admin/providers").get_data(as_text=True)

        self.assertIn(f"/admin/providers/{row['id']}/edit", body)

    def test_edit_page_renders_values_without_revealing_secret(self) -> None:
        """编辑页回显可改字段，但密钥原值不出现在页面里。"""
        _, client, token = self.logged_in_client()
        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "create", "name": "千问",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen-vl-max;qwen3-vl-30b", "api_key": SECRET,
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
            },
        )
        with self.database() as connection:
            row = connection.execute(
                "SELECT id FROM model_providers WHERE name='千问'"
            ).fetchone()

        body = client.get(f"/admin/providers/{row['id']}/edit").get_data(as_text=True)

        # 模型清单逐行渲染：每个模型一个输入框，选哪个启用就在它自己那一行
        self.assertIn('value="qwen-vl-max"', body)
        self.assertIn('value="qwen3-vl-30b"', body)
        self.assertIn('name="model_entry"', body)
        self.assertIn('name="active_model_index"', body)
        self.assertIn("https://dashscope.example.com/compatible-mode/v1", body)
        self.assertNotIn(SECRET, body)

    def test_edit_page_saves_changed_fields_and_keeps_secret(self) -> None:
        """编辑页提交后字段生效，密钥留空表示保持原值而不是清空。"""
        _, client, token = self.logged_in_client()
        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "create", "name": "千问",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen3-vl-30b-a3b-instruct", "api_key": SECRET,
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
            },
        )
        with self.database() as connection:
            row = connection.execute(
                "SELECT id,version FROM model_providers WHERE name='千问'"
            ).fetchone()

        saved = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(row["id"]), "version": str(row["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen-vl-max;qwen-vl-plus",
                "api_key": "", "timeout_seconds": "300",
                "max_long_edge": "1568", "is_enabled": "1",
            },
        )

        self.assertIn(saved.status_code, (302, 303))
        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,timeout_seconds,max_long_edge,api_key,is_enabled "
                "FROM model_providers WHERE id=?",
                (row["id"],),
            ).fetchone()
        self.assertEqual("qwen-vl-max;qwen-vl-plus", after["model_name"])
        self.assertEqual(300, after["timeout_seconds"])
        self.assertEqual(1568, after["max_long_edge"])
        self.assertEqual(1, after["is_enabled"])
        self.assertEqual(SECRET, after["api_key"])

    def test_listing_switches_active_model_through_dropdown(self) -> None:
        """列表页下拉只提交启用模型一个字段就能切换，其余字段不受影响。"""
        _, client, token = self.logged_in_client()
        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "create", "name": "千问",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen-vl-max;qwen3-vl-30b", "api_key": SECRET,
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
            },
        )
        with self.database() as connection:
            row = connection.execute(
                "SELECT id,version,active_model FROM model_providers WHERE name='千问'"
            ).fetchone()
        self.assertEqual("qwen-vl-max", row["active_model"])

        listing = client.get("/admin/providers").get_data(as_text=True)
        self.assertIn('name="active_model"', listing)

        switched = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(row["id"]), "version": str(row["version"]),
                "active_model": "qwen3-vl-30b",
            },
        )

        self.assertIn(switched.status_code, (302, 303))
        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model,timeout_seconds,api_key,is_enabled "
                "FROM model_providers WHERE id=?",
                (row["id"],),
            ).fetchone()
        self.assertEqual("qwen3-vl-30b", after["active_model"])
        self.assertEqual("qwen-vl-max;qwen3-vl-30b", after["model_name"])
        self.assertEqual(600, after["timeout_seconds"])
        self.assertEqual(SECRET, after["api_key"])
        self.assertEqual(1, after["is_enabled"])

    def _create_provider_via_form(self, client, token, **overrides) -> dict:
        """用页面表单建一条档案并返回其编号与版本。"""
        data = {
            "csrf_token": token, "action": "create", "name": "千问",
            "base_url": "https://dashscope.example.com/compatible-mode/v1",
            "model_name": "model-a;model-b", "api_key": SECRET,
            "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
        }
        data.update(overrides)
        client.post("/admin/providers", data=data)
        with self.database() as connection:
            row = connection.execute(
                "SELECT id,version,model_name,active_model FROM model_providers WHERE name=?",
                (data["name"],),
            ).fetchone()
        return dict(row)

    def test_edit_form_assembles_model_pool_from_rows(self) -> None:
        """逐行提交的模型名合成模型池，选中行的序号决定启用哪个。"""
        _, client, token = self.logged_in_client()
        created = self._create_provider_via_form(client, token)

        saved = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(created["id"]), "version": str(created["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_entry": ["model-a", "model-b", "model-c"],
                "active_model_index": "2",
                "api_key": "", "timeout_seconds": "600",
                "max_long_edge": "2560", "is_enabled": "1",
            },
        )

        self.assertIn(saved.status_code, (302, 303))
        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model FROM model_providers WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertEqual("model-a;model-b;model-c", after["model_name"])
        self.assertEqual("model-c", after["active_model"])

    def test_edit_form_row_index_survives_renaming_that_row(self) -> None:
        """选中行的模型名被改掉后仍然是选中的那一行，不会失配到别的模型。

        单选按钮的值是行序号而不是模型名，正是为了这个场景：改文本不需要同步选中值。
        """
        _, client, token = self.logged_in_client()
        created = self._create_provider_via_form(client, token)

        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(created["id"]), "version": str(created["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_entry": ["model-a", "model-b-renamed"],
                "active_model_index": "1",
                "api_key": "", "timeout_seconds": "600",
                "max_long_edge": "2560", "is_enabled": "1",
            },
        )

        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model FROM model_providers WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertEqual("model-a;model-b-renamed", after["model_name"])
        self.assertEqual("model-b-renamed", after["active_model"])

    def test_edit_form_clearing_a_row_removes_that_model(self) -> None:
        """清空某行即删除该模型，且不会让后面行的选中关系错位。

        序号相对未过滤的原始行列表取值，因此清空第一行后选中第三行仍然拿到第三行。
        """
        _, client, token = self.logged_in_client()
        created = self._create_provider_via_form(
            client, token, model_name="model-a;model-b;model-c"
        )

        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(created["id"]), "version": str(created["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_entry": ["", "model-b", "model-c"],
                "active_model_index": "2",
                "api_key": "", "timeout_seconds": "600",
                "max_long_edge": "2560", "is_enabled": "1",
            },
        )

        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model FROM model_providers WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertEqual("model-b;model-c", after["model_name"])
        self.assertEqual("model-c", after["active_model"])

    def test_edit_form_clearing_the_selected_row_falls_back(self) -> None:
        """把选中那行清空时落回剩余清单第一项，而不是保存一个空的启用模型。"""
        _, client, token = self.logged_in_client()
        created = self._create_provider_via_form(client, token)

        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(created["id"]), "version": str(created["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_entry": ["", "model-b"],
                "active_model_index": "0",
                "api_key": "", "timeout_seconds": "600",
                "max_long_edge": "2560", "is_enabled": "1",
            },
        )

        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model FROM model_providers WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertEqual("model-b", after["model_name"])
        self.assertEqual("model-b", after["active_model"])

    def test_edit_form_blank_trailing_row_adds_a_model(self) -> None:
        """末尾预留空行填了就新增，禁用脚本时也能加模型。"""
        _, client, token = self.logged_in_client()
        created = self._create_provider_via_form(client, token)

        client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(created["id"]), "version": str(created["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_entry": ["model-a", "model-b", "model-new"],
                "active_model_index": "0",
                "api_key": "", "timeout_seconds": "600",
                "max_long_edge": "2560", "is_enabled": "1",
            },
        )

        with self.database() as connection:
            after = connection.execute(
                "SELECT model_name,active_model FROM model_providers WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertEqual("model-a;model-b;model-new", after["model_name"])
        self.assertEqual("model-a", after["active_model"])

    def test_edit_page_rejects_unknown_provider(self) -> None:
        """编辑不存在的档案给 404，而不是渲染一张空表单让人填半天再报错。"""
        _, client, _ = self.logged_in_client()

        response = client.get("/admin/providers/999999/edit")

        self.assertEqual(404, response.status_code)

    def test_form_create_then_disable_then_delete(self) -> None:
        """页面表单能完成新建、停用、删除三个动作。"""
        _, client, token = self.logged_in_client()

        created = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "create", "name": "千问",
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen3-vl-30b-a3b-instruct", "api_key": SECRET,
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "1",
            },
        )
        self.assertIn(created.status_code, (302, 303))

        with self.database() as connection:
            row = connection.execute(
                "SELECT id,version,api_key FROM model_providers WHERE name='千问'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(SECRET, row["api_key"])

        disabled = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "update",
                "provider_id": str(row["id"]), "version": str(row["version"]),
                "base_url": "https://dashscope.example.com/compatible-mode/v1",
                "model_name": "qwen3-vl-30b-a3b-instruct",
                "timeout_seconds": "600", "max_long_edge": "2560", "is_enabled": "",
            },
        )
        self.assertIn(disabled.status_code, (302, 303))
        with self.database() as connection:
            after = connection.execute(
                "SELECT is_enabled,version FROM model_providers WHERE id=?",
                (row["id"],),
            ).fetchone()
        self.assertEqual(0, after["is_enabled"])

        deleted = client.post(
            "/admin/providers",
            data={
                "csrf_token": token, "action": "delete",
                "provider_id": str(row["id"]), "version": str(after["version"]),
            },
        )
        self.assertIn(deleted.status_code, (302, 303))
        with self.database() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM model_providers"
            ).fetchone()[0]
        self.assertEqual(0, remaining)

    def test_import_from_settings_creates_a_provider(self) -> None:
        """一键导入把当前兜底配置建成一条档案，免掉数据迁移。"""
        _, client, token = self.logged_in_client()

        response = client.post(
            "/admin/providers",
            data={"csrf_token": token, "action": "import", "name": "当前配置"},
        )

        self.assertIn(response.status_code, (302, 303))
        with self.database() as connection:
            row = connection.execute(
                "SELECT name,base_url,model_name FROM model_providers"
            ).fetchone()
        self.assertEqual("当前配置", row["name"])
        self.assertTrue(row["base_url"])
        self.assertTrue(row["model_name"])

    def test_json_api_lists_without_secret(self) -> None:
        """接口返回不含密钥原值。"""
        _, client, token = self.logged_in_client()
        client.post(
            "/api/admin/providers",
            json={
                "name": "豆包", "base_url": "https://ark.example.com/api/v3",
                "model_name": "doubao-vision", "api_key": SECRET,
                "timeout_seconds": 300, "max_long_edge": 1568, "is_enabled": True,
            },
            headers={"X-CSRFToken": token},
        )

        response = client.get("/api/admin/providers")

        self.assertEqual(200, response.status_code)
        # 响应把中文转义成 \uXXXX，因此中文字段要解析后再断言；密钥是 ASCII，
        # 可以直接在原始报文里搜，这样连转义形式的泄露也一并挡住。
        payload = response.get_json()
        self.assertEqual(["豆包"], [item["name"] for item in payload["data"]])
        self.assertNotIn("api_key", payload["data"][0])
        self.assertEqual(SECRET[-4:], payload["data"][0]["api_key_hint"])
        self.assertNotIn(SECRET, response.get_data(as_text=True))


class LegacyPathUnaffectedTestCase(TemporaryDatabaseTestCase):
    """验证不建任何档案时旧链路完全不受影响。

    这是阶段一「只存不用」的核心承诺：改造上线后即使没人建档，照片分析也照原样工作。
    """

    def setUp(self) -> None:
        """准备应用与相关服务。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            services = self.app.extensions["inktime_services"]
            self.configuration = services["configuration"]
            self.provider_service = services["model_providers"]

    def test_no_provider_means_empty_resolution(self) -> None:
        """空表下解析一律返回空值，调用方据此回退兜底配置。"""
        self.assertEqual([], self.provider_service.list_providers())
        self.assertIsNone(self.provider_service.resolve("千问"))
        self.assertEqual([], self.provider_service.resolve_chain("千问;公司"))
        self.assertEqual("", self.provider_service.api_key_for("千问"))

    def test_analysis_snapshot_keys_are_unchanged(self) -> None:
        """analysis 作用域快照键集合不变：阶段一不动快照结构。

        改了快照键集合会让队列里已认领的任务全部判为 invalid_config_snapshot，
        因此这条约束要在阶段一就钉住，等阶段二再有意识地扩展。
        """
        snapshot = self.configuration.snapshot("analysis")

        self.assertEqual({"version", "settings"}, set(snapshot))
        for key in ("API_URL", "MODEL_NAME", "TIMEOUT", "VLM_MAX_LONG_EDGE"):
            self.assertIn(key, snapshot["settings"])
        self.assertNotIn("API_KEY", snapshot["settings"])

    def test_routing_keys_are_registered_without_changing_legacy_settings_snapshot(self) -> None:
        """路由键可热更新，但旧 settings 精确键集合保持兼容。"""
        for key in ("ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER"):
            self.assertEqual("", self.configuration.get(key))
        snapshot = self.configuration.snapshot("analysis")
        self.assertEqual({"version", "settings"}, set(snapshot))
        self.assertNotIn("ANALYSIS_PROVIDER", snapshot["settings"])
        self.assertNotIn("NARRATION_PROVIDER", snapshot["settings"])
        self.assertEqual([], self.provider_service.referencing_routes("千问"))


class SettingsAuditHistoryRedactionTestCase(TemporaryDatabaseTestCase):
    """验证历史审计脱敏命令：写入路径的修复只管新记录，历史要单独补。"""

    LEGACY_SECRET = "sk-legacy-plaintext-value-4321"

    def setUp(self) -> None:
        """造一条改造之前那种含明文的历史审计行。"""
        super().setUp()
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)
        with self.database() as connection:
            connection.execute(
                "INSERT INTO app_settings_audit(batch_id,old_version,new_version,"
                "changed_keys_json,old_values_json,new_values_json,modified_by_user_id,"
                "modified_by_username,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-batch-0001", 3, 4,
                    '["API_KEY","MODEL_NAME"]',
                    json.dumps(
                        {"API_KEY": self.LEGACY_SECRET, "MODEL_NAME": "old-model"},
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {"API_KEY": "sk-legacy-rotated-9999", "MODEL_NAME": "new-model"},
                        ensure_ascii=False,
                    ),
                    self.admin_id, ADMIN_USERNAME, "2026-08-23T02:27:03+00:00",
                ),
            )

    def _audit_row(self) -> dict:
        """读回那条审计行。"""
        with self.database() as connection:
            row = connection.execute(
                "SELECT * FROM app_settings_audit WHERE batch_id='legacy-batch-0001'"
            ).fetchone()
        return dict(row)

    def test_dry_run_reports_without_writing(self) -> None:
        """默认试运行只统计，不改数据——不可逆写操作应有的默认值。"""
        report = redact_settings_audit_history(self.database_path)

        self.assertEqual(1, len(report["affected"]))
        self.assertFalse(report["applied"])
        self.assertIn(self.LEGACY_SECRET, self._audit_row()["old_values_json"])

    def test_apply_removes_plaintext_and_keeps_metadata(self) -> None:
        """脱敏后明文消失，但时间、版本、变更键与操作人全部保留。

        审计的价值在于「谁在什么时候改了什么」，为了脱敏把整行删掉是过度处置。
        """
        redact_settings_audit_history(self.database_path, apply_changes=True)

        row = self._audit_row()
        blob = row["old_values_json"] + row["new_values_json"]
        self.assertNotIn(self.LEGACY_SECRET, blob)
        self.assertNotIn("sk-legacy-rotated-9999", blob)
        self.assertIn(REDACTED_TEXT, blob)
        self.assertEqual("2026-08-23T02:27:03+00:00", row["created_at"])
        self.assertEqual(ADMIN_USERNAME, row["modified_by_username"])
        self.assertEqual(3, row["old_version"])
        self.assertEqual(4, row["new_version"])
        self.assertEqual('["API_KEY","MODEL_NAME"]', row["changed_keys_json"])

    def test_non_sensitive_values_survive(self) -> None:
        """非敏感值必须留下，否则审计变成一片「已脱敏」、没有任何复盘价值。"""
        redact_settings_audit_history(self.database_path, apply_changes=True)

        row = self._audit_row()
        self.assertIn("old-model", row["old_values_json"])
        self.assertIn("new-model", row["new_values_json"])

    def test_running_twice_reports_nothing_left(self) -> None:
        """幂等：已脱敏的行不该被重复算成受影响。

        否则这个命令永远报「还有 N 行要处理」，看不出到底清干净了没有。
        """
        redact_settings_audit_history(self.database_path, apply_changes=True)

        second = redact_settings_audit_history(self.database_path, apply_changes=True)

        self.assertEqual([], second["affected"])
        self.assertFalse(second["applied"])

    def test_clean_database_reports_nothing(self) -> None:
        """本来就没有明文残留时报告为空。"""
        with self.database() as connection:
            connection.execute("DELETE FROM app_settings_audit")

        report = redact_settings_audit_history(self.database_path)

        self.assertEqual(0, report["scanned"])
        self.assertEqual([], report["affected"])
