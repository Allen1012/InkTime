"""验证后台配置页的字节类字段按 MiB 填写，而存储与接口仍用字节。

`67108864` 这种数字对人没有体感，改一次上限要先自己数位数。但配置键的单位不能跟着改：
`UPLOAD_MAX_BYTES` 被 `.env`、Docker Compose、派生的 `MAX_CONTENT_LENGTH` 和上传服务
多处引用，把键的语义改成 MiB 会让现有部署里那行字节数被静默误解。

因此换算只发生在页面这一层。这组用例钉住三件事：页面显示与填写用 MiB、提交后落库是字节、
以及换算来回不产生精度漂移——后者最隐蔽，截断显示会让「保存一次无关配置」悄悄改掉体积上限。
"""

from __future__ import annotations

import re

from flask import render_template

from src.configuration import (
    SETTING_REGISTRY,
    ConfigurationActor,
    format_display_number,
)
from src.server.app import create_app
from src.server.blueprints.admin import _parse_settings_form, _settings_context
from tests.support import TemporaryDatabaseTestCase

ADMIN_USERNAME = "units-admin"
ADMIN_PASSWORD = "inktime-units-password"
MEBIBYTE = 1048576
# 带显示单位的字节类配置项，与注册表里的声明一一对应。
BYTE_SETTING_KEYS = ("UPLOAD_MAX_BYTES", "UPLOAD_TARGET_BYTES")


class DisplayNumberTestCase(TemporaryDatabaseTestCase):
    """验证显示值的格式化不引入精度损失。"""

    def test_whole_numbers_lose_the_decimal_point(self) -> None:
        """64 MiB 要显示成 64，而不是 64.0。"""
        self.assertEqual("64", format_display_number(67108864 / MEBIBYTE))
        self.assertEqual("0", format_display_number(0))

    def test_fractional_values_round_trip_exactly(self) -> None:
        """非整数 MiB 必须精确往返。

        刻度是 2 的幂，整数字节除以刻度可被浮点精确表示，所以这里不允许四舍五入：
        把 0.1953125 截断成 0.195 再乘回去是 204472，与原值 204800 差了 328 字节，
        表现为「保存一次无关配置，压缩目标体积自己变了」。
        """
        text = format_display_number(204800 / MEBIBYTE)

        self.assertEqual("0.1953125", text)
        self.assertEqual(204800, round(float(text) * MEBIBYTE))


class ByteSettingRegistryTestCase(TemporaryDatabaseTestCase):
    """验证注册表对字节类配置项的声明完整且自洽。"""

    def test_byte_settings_declare_mebibyte_display_unit(self) -> None:
        """两个字节类配置项都要声明 MiB 显示单位与对应刻度。"""
        for key in BYTE_SETTING_KEYS:
            definition = SETTING_REGISTRY[key]
            self.assertEqual("MiB", definition.display_unit, key)
            self.assertEqual(MEBIBYTE, definition.display_scale, key)

    def test_no_setting_declares_a_scale_without_a_unit(self) -> None:
        """有刻度就必须有单位：否则页面会显示一个换算过却没标单位的数字。"""
        mismatched = sorted(
            key
            for key, definition in SETTING_REGISTRY.items()
            if definition.display_scale > 1 and not definition.display_unit
        )
        self.assertEqual([], mismatched)

    def test_field_names_no_longer_say_bytes(self) -> None:
        """字段名不该再以「字节数」结尾，否则与页面上填的 MiB 自相矛盾。"""
        for key in BYTE_SETTING_KEYS:
            self.assertNotIn("字节", SETTING_REGISTRY[key].name, key)


class ByteSettingRenderTestCase(TemporaryDatabaseTestCase):
    """验证配置页把字节值渲染成 MiB，并同时给出精确字节数。"""

    def _field_markup(self, key: str) -> str:
        """渲染配置页并截取指定配置项的那一段标记。"""
        app = create_app(self.application_config())
        with app.test_request_context("/admin/settings"):
            html = render_template("admin/settings.html", **_settings_context())
        start = html.find(f'data-setting-key="{key}"')
        self.assertNotEqual(-1, start, f"配置页必须渲染出 {key}")
        return html[start : html.find("</label>", start)]

    def test_input_value_is_in_mebibytes_not_bytes(self) -> None:
        """输入框里应该是 64，而不是 67108864。"""
        field = self._field_markup("UPLOAD_MAX_BYTES")

        value = re.search(r'name="UPLOAD_MAX_BYTES"[^>]*value="([^"]+)"', field)
        self.assertIsNotNone(value)
        self.assertEqual("64", value.group(1))
        self.assertNotIn("67108864", value.group(1))

    def test_unit_label_is_rendered_and_associated_for_screen_readers(self) -> None:
        """单位要显示出来，并通过 aria-describedby 关联，读屏用户同样需要知道单位。"""
        field = self._field_markup("UPLOAD_MAX_BYTES")

        self.assertIn('aria-describedby="setting-UPLOAD_MAX_BYTES-unit"', field)
        self.assertIn('id="setting-UPLOAD_MAX_BYTES-unit">MiB<', field)

    def test_hint_gives_both_readable_size_and_exact_bytes(self) -> None:
        """提示要同时给出常用读法与精确字节数：环境变量和接口用的是字节。"""
        field = self._field_markup("UPLOAD_MAX_BYTES")

        self.assertIn("settings-unit-hint", field)
        self.assertIn("64 MB", field)
        self.assertIn("67108864 字节", field)
        self.assertIn("环境变量与接口按字节取值", field)

    def test_bounds_are_scaled_to_the_display_unit(self) -> None:
        """上下界也要换算，否则 max 会是 104857600 而输入框里是 64。"""
        field = self._field_markup("UPLOAD_MAX_BYTES")

        self.assertIn('max="100"', field)
        self.assertNotIn('max="104857600"', field)

    def test_step_allows_fractions_so_existing_values_stay_valid(self) -> None:
        """必须允许小数：固定步长会让环境变量设进来的非整数 MiB 被浏览器判成非法。"""
        field = self._field_markup("UPLOAD_TARGET_BYTES")

        self.assertIn('step="any"', field)

    def test_settings_without_a_unit_keep_the_plain_input(self) -> None:
        """无单位的数值项渲染方式不变，不该被顺带改掉。"""
        field = self._field_markup("TIMEOUT")

        self.assertIn('value="600"', field)
        self.assertNotIn("settings-unit-field", field)
        self.assertNotIn("settings-unit-hint", field)


class ByteSettingFormTestCase(TemporaryDatabaseTestCase):
    """验证表单提交的 MiB 值换算回字节后落库。"""

    def setUp(self) -> None:
        """准备应用、配置服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.configuration = self.app.extensions["inktime_services"][
                "configuration"
            ]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)
        self.actor = ConfigurationActor(self.admin_id, ADMIN_USERNAME)

    def _form_data(self, **overrides: str) -> dict[str, str]:
        """按页面契约构造一份完整表单，即回填显示值而不是字节值。"""
        with self.app.test_request_context("/admin/settings"):
            state = _settings_context()["state"]
        form = {"expected_version": str(state["version"])}
        for item in state["settings"]:
            if not item["editable"] or item["sensitive"]:
                continue
            value = item["display_value"]
            form[item["key"]] = (
                "true" if value is True else "false" if value is False else str(value)
            )
        form.update(overrides)
        return form

    def _submit(self, **overrides: str) -> dict:
        """提交一次配置表单并返回落库后的两个字节类配置值。"""
        form = self._form_data(**overrides)
        with self.app.test_request_context(
            "/admin/settings", method="POST", data=form
        ):
            version, changes = _parse_settings_form()
            self.configuration.update_batch(changes, version, self.actor)
        return self.configuration.get_many(BYTE_SETTING_KEYS)

    def test_mebibyte_input_is_stored_as_bytes(self) -> None:
        """填 32 应当落库为 33554432 字节。"""
        stored = self._submit(UPLOAD_MAX_BYTES="32")

        self.assertEqual(32 * MEBIBYTE, stored["UPLOAD_MAX_BYTES"])

    def test_fractional_mebibyte_input_is_supported(self) -> None:
        """压缩目标常需要小于 1 MiB，填 0.5 应当落库为 524288 字节。"""
        stored = self._submit(UPLOAD_TARGET_BYTES="0.5")

        self.assertEqual(524288, stored["UPLOAD_TARGET_BYTES"])

    def test_zero_still_disables_compression(self) -> None:
        """零表示不压缩，换算不能把它变成别的值。"""
        stored = self._submit(UPLOAD_TARGET_BYTES="0")

        self.assertEqual(0, stored["UPLOAD_TARGET_BYTES"])

    def test_stored_value_stays_an_integer_number_of_bytes(self) -> None:
        """落库必须是整数字节，浮点字节数会在后续 bounded_int 与派生计算里出问题。"""
        stored = self._submit(UPLOAD_TARGET_BYTES="0.3")

        self.assertIsInstance(stored["UPLOAD_TARGET_BYTES"], int)
        self.assertEqual(round(0.3 * MEBIBYTE), stored["UPLOAD_TARGET_BYTES"])

    def test_saving_an_unrelated_field_does_not_drift_byte_values(self) -> None:
        """改一个无关配置不能顺带改掉体积上限，即使当前值不是整数 MiB。

        这是换算方案最容易出的隐蔽 bug：显示时截断、提交时乘回去，每保存一次就漂一点。
        """
        self.configuration.update_batch(
            {"UPLOAD_TARGET_BYTES": 204800},
            self.configuration.list_admin_settings()["version"],
            self.actor,
        )

        stored = self._submit(TIMEOUT="480")

        self.assertEqual(204800, stored["UPLOAD_TARGET_BYTES"])
        self.assertEqual(480, self.configuration.get("TIMEOUT"))

    def test_json_api_still_takes_bytes(self) -> None:
        """接口是机器入口，保持字节这一基准单位，不跟着页面换算。"""
        # setUp 已用夹具插入 ADMIN_USERNAME，这里换一个用户名建可登录账号，避免撞唯一约束
        login_username = f"{ADMIN_USERNAME}-login"
        with self.app.app_context():
            self.app.extensions["inktime_services"]["auth"].create_admin(
                login_username, ADMIN_PASSWORD
            )
        client = self.app.test_client()
        page = client.get("/admin/login")
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(token)
        client.post(
            "/admin/login",
            data={
                "username": login_username,
                "password": ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
        )
        version = self.configuration.list_admin_settings()["version"]
        # 后台接口对写请求强制校验跨站请求伪造令牌，JSON 请求体只能走请求头带令牌
        settings_page = client.get("/admin/settings").get_data(as_text=True)
        api_token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', settings_page
        )
        self.assertIsNotNone(api_token)

        response = client.patch(
            "/api/admin/settings",
            json={
                "expected_version": version,
                "changes": {"UPLOAD_MAX_BYTES": 20971520},
            },
            headers={"X-CSRFToken": api_token.group(1)},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(20971520, self.configuration.get("UPLOAD_MAX_BYTES"))
