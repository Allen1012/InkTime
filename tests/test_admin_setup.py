"""验证首次管理员网页设置的安全状态机。"""

import re
from unittest.mock import patch

from werkzeug.security import check_password_hash

from src.server.app import create_app
from src.server.repositories import FirstAdminAlreadyCreatedError
from tests.support import TemporaryDatabaseTestCase


class AdminSetupPageTestCase(TemporaryDatabaseTestCase):
    """使用真实迁移数据库和 Flask 客户端验证首次设置流程。"""

    TOKEN = "setup-token-abcdefghijklmnopqrstuvwxyz"
    PASSWORD = "correct-horse-battery-staple"

    def setUp(self) -> None:
        """创建启用一次性初始化令牌的隔离应用。"""
        super().setUp()
        config = self.application_config()
        config.update(
            {
                "INITIAL_SETUP_TOKEN": self.TOKEN,
                "INITIAL_SETUP_TOKEN_FILE": "",
            }
        )
        self.app = create_app(config)
        self.client = self.app.test_client()

    def csrf_token(self, path: str) -> str:
        """读取指定页面并提取当前客户端会话的跨站请求伪造令牌。"""
        response = self.client.get(path)
        self.assertEqual(200, response.status_code)
        match = re.search(
            rb'name="csrf_token"[^>]*value="([^"]+)"', response.data
        )
        self.assertIsNotNone(match)
        return match.group(1).decode("utf-8")

    def admin_rows(self):
        """返回当前数据库中的管理员账号与密码哈希。"""
        with self.database() as connection:
            return connection.execute(
                "SELECT username,password_hash FROM admin_users ORDER BY id"
            ).fetchall()

    def post_setup(
        self,
        *,
        username: str = "first-admin",
        password: str | None = None,
        confirm_password: str | None = None,
        setup_token: str | None = None,
        include_csrf: bool = True,
    ):
        """按真实表单字段提交首次设置请求。"""
        password = self.PASSWORD if password is None else password
        confirm_password = password if confirm_password is None else confirm_password
        setup_token = self.TOKEN if setup_token is None else setup_token
        data = {
            "username": username,
            "password": password,
            "confirm_password": confirm_password,
            "setup_token": setup_token,
        }
        if include_csrf:
            data["csrf_token"] = self.csrf_token("/admin/setup")
        return self.client.post("/admin/setup", data=data, follow_redirects=False)

    def test_get_setup_and_login_guide_are_available_without_admins(self) -> None:
        """空管理员表应开放设置页并在登录页展示入口。"""
        setup_response = self.client.get("/admin/setup")
        login_response = self.client.get("/admin/login")

        self.assertEqual(200, setup_response.status_code)
        self.assertIn(b'name="confirm_password"', setup_response.data)
        self.assertIn(b'name="setup_token"', setup_response.data)
        self.assertIn(b'name="csrf_token"', setup_response.data)
        self.assertIn("开始首次设置".encode("utf-8"), login_response.data)
        self.assertNotIn("INITIAL_SETUP_TOKEN", self.app.config)
        self.assertNotIn("INITIAL_SETUP_TOKEN_FILE", self.app.config)

    def test_post_without_csrf_is_rejected(self) -> None:
        """缺少跨站请求伪造令牌时不得进入业务创建逻辑。"""
        response = self.post_setup(include_csrf=False)

        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.admin_rows())

    def test_password_confirmation_mismatch_is_rejected(self) -> None:
        """两次密码不一致时应返回字段错误且不创建管理员。"""
        response = self.post_setup(confirm_password=self.PASSWORD + "-different")

        self.assertEqual(400, response.status_code)
        self.assertIn("两次输入的密码不一致".encode("utf-8"), response.data)
        self.assertEqual([], self.admin_rows())

    def test_wrong_setup_token_is_rejected_without_sensitive_echo(self) -> None:
        """错误令牌应返回 403，且响应不得回显令牌或密码。"""
        wrong_token = "wrong-setup-token-value-123456"

        response = self.post_setup(setup_token=wrong_token)

        self.assertEqual(403, response.status_code)
        self.assertNotIn(wrong_token.encode("utf-8"), response.data)
        self.assertNotIn(self.PASSWORD.encode("utf-8"), response.data)
        self.assertEqual([], self.admin_rows())

    def test_concurrent_creation_error_closes_setup(self) -> None:
        """前置检查后若其他请求已创建管理员，应按永久关闭返回 404。"""
        authentication_service = self.app.extensions["inktime_services"]["auth"]
        with patch.object(authentication_service, "has_admins", return_value=False), patch.object(
            authentication_service,
            "create_first_admin",
            side_effect=FirstAdminAlreadyCreatedError("already created"),
        ):
            response = self.post_setup()

        self.assertEqual(404, response.status_code)
        self.assertEqual([], self.admin_rows())

    def test_success_creates_hashed_admin_then_permanently_closes_setup(self) -> None:
        """成功创建后应可登录，且设置入口不依赖令牌永久关闭。"""
        response = self.post_setup(username="  first-admin  ")

        self.assertEqual(302, response.status_code)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))
        rows = self.admin_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("first-admin", rows[0]["username"])
        self.assertNotEqual(self.PASSWORD, rows[0]["password_hash"])
        self.assertTrue(check_password_hash(rows[0]["password_hash"], self.PASSWORD))

        self.assertEqual(404, self.client.get("/admin/setup").status_code)
        login_page = self.client.get("/admin/login")
        self.assertNotIn("开始首次设置".encode("utf-8"), login_page.data)
        login_csrf = self.csrf_token("/admin/login")
        closed_post = self.client.post(
            "/admin/setup",
            data={
                "csrf_token": login_csrf,
                "username": "second-admin",
                "password": self.PASSWORD,
                "confirm_password": self.PASSWORD,
                "setup_token": self.TOKEN,
            },
        )
        self.assertEqual(404, closed_post.status_code)

        login_response = self.client.post(
            "/admin/login",
            data={
                "csrf_token": login_csrf,
                "username": "first-admin",
                "password": self.PASSWORD,
                "next": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(302, login_response.status_code)
        self.assertTrue(login_response.headers["Location"].endswith("/admin"))


if __name__ == "__main__":
    unittest.main()
