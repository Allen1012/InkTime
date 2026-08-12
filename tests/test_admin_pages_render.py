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
        """验证配置页真实响应中包含目录状态表与可折叠分组。"""
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn("照片目录状态", body)
        self.assertIn('class="admin-card settings-group"', body)
        self.assertIn(str(self.image_directory), body)

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
