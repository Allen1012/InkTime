"""六个后台页面在登录后的真实渲染回归测试。"""

from __future__ import annotations

import re

from src.configuration import ConfigurationActor
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


ADMIN_USERNAME = "regression-admin"
ADMIN_PASSWORD = "inktime-regression-password"


class AdminPagesRenderTestCase(TemporaryDatabaseTestCase):
    """用真实登录会话确认后台六个页面都能正常渲染。"""

    def logged_in_client(self):
        """创建应用、真实管理员账号并完成带跨站请求伪造令牌的表单登录。"""
        app = create_app(self.application_config())
        with app.app_context():
            app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )
        client = app.test_client()
        form_page = client.get("/admin/login")
        self.assertEqual(200, form_page.status_code)
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', form_page.get_data(as_text=True)
        )
        self.assertIsNotNone(token, "登录表单必须包含跨站请求伪造令牌")
        response = client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        return app, client

    def test_all_admin_pages_render(self) -> None:
        """验证概览、照片、上传、回收站、任务与配置页均返回 200。"""
        self.create_photo("rendered.jpg")
        _, client = self.logged_in_client()

        for path in (
            "/admin",
            "/admin/photos",
            "/admin/photos/upload",
            "/admin/trash",
            "/admin/jobs",
            "/admin/settings",
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(200, response.status_code)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertGreater(len(response.get_data(as_text=True)), 500)

    def test_settings_page_contains_stage_four_sections(self) -> None:
        """验证配置页真实响应中包含目录状态表与分类标签面板。"""
        app, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn("照片目录状态", body)
        self.assertIn('role="tablist"', body)
        self.assertIn('id="settings-panel-model"', body)
        self.assertIn(str(self.image_directory), body)
        self.assertIn("设备下载地址", body)
        self.assertIn(
            f'/static/inktime/{app.config["DOWNLOAD_KEY"]}/latest.bin',
            body,
        )
        self.assertIn('id="device-download-url"', body)
        self.assertIn("readonly", body)
        self.assertIn('id="copy-device-download-url"', body)

    def test_settings_page_shows_display_window_summary_and_presets(self) -> None:
        """验证配置页展示生效时间段摘要、次数估算与常用预设按钮。"""
        app, client = self.logged_in_client()
        with app.app_context():
            configuration = app.extensions["inktime_services"]["configuration"]
            configuration.update_batch(
                {
                    "DISPLAY_ACTIVE_WINDOWS": "Mon-Fri@09:00-22:30",
                    "DISPLAY_ROTATE_MODE": "hourly",
                },
                configuration.list_admin_settings()["version"],
                ConfigurationActor(self.create_admin_user("window-admin"), "window-admin"),
            )

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn("展示生效时间段", body)
        self.assertIn("周一至周五 09:00 到 22:30", body)
        self.assertIn("周六、周日 全天休息", body)
        # 次数估算要能暴露区间右开的影响
        self.assertIn("周一 14 次", body)
        self.assertIn("周六 0 次", body)
        self.assertIn("settings-preset", body)
        self.assertIn('data-value="Mon-Fri@09:00-22:30"', body)
        self.assertIn("js/admin-settings.js", body)
        self.assertIn('id="setting-DISPLAY_ACTIVE_WINDOWS"', body)


    def test_settings_audit_has_its_own_tab(self) -> None:
        """验证配置审计是独立标签，面板里不含配置控件，也不再多包一层信息块。"""
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn('id="settings-tab-audit"', body)
        self.assertIn("配置审计", body)
        panel_at = body.find('id="settings-panel-audit"')
        self.assertNotEqual(-1, panel_at, "审计必须有自己的面板")
        panel = body[panel_at : body.find("</section>", panel_at)]
        self.assertNotIn("settings-field", panel)
        # 面板本身已经是卡片，审计内容不再套 .settings-info-block 第二层框。
        self.assertNotIn("settings-info-block", panel)
        # 纯记录标签不显示项数角标，避免出现「0」。
        audit_tab = body[body.find('id="settings-tab-audit"') :]
        audit_tab = audit_tab[: audit_tab.find("</button>")]
        self.assertNotIn("settings-tab-count", audit_tab)

    def test_settings_info_blocks_are_flat_inside_panels(self) -> None:
        """验证只读信息块是面板的直接兄弟，一个面板最多一块，不存在嵌套两层框。"""
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        # 仍需标题与说明的三处信息块保留 .settings-info-block，样式已改为纯分隔线。
        self.assertEqual(3, body.count('class="settings-info-block"'))
        for title in ("设备下载地址", "照片目录状态", "展示生效时间段"):
            self.assertIn(title, body)
        panel_starts = [
            match.start()
            for match in re.finditer(r'id="settings-panel-[a-z]+"', body)
        ]
        self.assertEqual(6, len(panel_starts))
        for start in panel_starts:
            panel = body[start : body.find("</section>", start)]
            self.assertLessEqual(
                panel.count('class="settings-info-block"'),
                1,
                "单个面板里最多一块只读信息块，多于一块说明出现了嵌套或重复",
            )

    def test_settings_form_round_trip_saves_across_all_tabs(self) -> None:
        """验证按页面渲染出的表单整体回提，可同时保存分布在不同标签的配置项。

        分类标签只做面板显隐，全部可编辑项始终位于同一个表单内，因此这里直接把渲染
        出来的控件原样回提，确认不会因为某个标签「没被点开」而漏掉字段或触发校验失败。
        """
        app, client = self.logged_in_client()
        configuration = app.extensions["inktime_services"]["configuration"]

        page = client.get("/admin/settings").get_data(as_text=True)
        form = dict(re.findall(r'name="([A-Za-z_]+)"[^>]*value="([^"]*)"', page))
        self.assertIn("expected_version", form)
        self.assertIn("csrf_token", form)
        # select 控件的当前值不在 value 属性里，需要从 selected 选项取。
        for key, options in re.findall(
            r'<select id="setting-([A-Z_]+)" name="[A-Z_]+">(.*?)</select>', page, re.S
        ):
            selected = re.search(r'value="([^"]*)" selected', options)
            self.assertIsNotNone(selected, f"{key} 必须有一个选中项")
            form[key] = selected.group(1)

        # 跨三个不同标签各改一项：模型与分析、展示与天气、上传与任务。
        form.update(
            {"TIMEOUT": "480", "DISPLAY_MIN_SCORE": "55.5", "JOB_MAX_ATTEMPTS": "2"}
        )
        before = configuration.list_admin_settings()
        before_version = before["version"]
        secret_before = next(
            item for item in before["settings"] if item["key"] == "API_KEY"
        )["configured"]

        response = client.post("/admin/settings", data=form, follow_redirects=False)

        self.assertIn(response.status_code, (302, 303))
        state = configuration.list_admin_settings()
        self.assertGreater(state["version"], before_version)
        values = {item["key"]: item.get("value") for item in state["settings"]}
        self.assertEqual(480, values["TIMEOUT"])
        self.assertEqual(55.5, values["DISPLAY_MIN_SCORE"])
        self.assertEqual(2, values["JOB_MAX_ATTEMPTS"])
        # 密钥控件回提的是空串，表示保持原值，不应被清空也不应被写入新值。
        secret_after = next(
            item for item in state["settings"] if item["key"] == "API_KEY"
        )["configured"]
        self.assertEqual(secret_before, secret_after)
