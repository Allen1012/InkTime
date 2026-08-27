"""站点图标在各类页面与根路径上的可用性回归测试。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase

IMAGES_DIR = Path("src/server/static/images")


class FaviconAssetTestCase(TemporaryDatabaseTestCase):
    """确认图标资源本身有效，且每个带独立 head 的页面都声明了它。"""

    def test_icon_files_are_valid_images(self) -> None:
        """图标必须是真正能被解码的图片。

        仓库里原有的 images/logo.png 其实是被误存成 png 的截断 base64，浏览器
        根本解不开；这条断言就是防止图标再次退化成那种「看着像图片的文本」。
        """
        with Image.open(IMAGES_DIR / "favicon.ico") as icon:
            self.assertEqual("ICO", icon.format)
            self.assertEqual(
                [(16, 16), (32, 32), (48, 48), (64, 64)],
                sorted(icon.info["sizes"]),
                "ICO 必须内含多尺寸，16 用于标签页、48 以上用于书签与桌面快捷方式",
            )

        with Image.open(IMAGES_DIR / "apple-touch-icon.png") as touch_icon:
            self.assertEqual("PNG", touch_icon.format)
            self.assertEqual((180, 180), touch_icon.size)

        svg = (IMAGES_DIR / "favicon.svg").read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn('viewBox="0 0 32 32"', svg)

    def test_public_pages_declare_all_three_icon_variants(self) -> None:
        """公开页面、展示页与错误页都要声明 SVG、ICO 回退与触摸图标。"""
        app = create_app(self.application_config())
        client = app.test_client()

        for path in ("/", "/category", "/search", "/display"):
            with self.subTest(path=path):
                body = client.get(path).get_data(as_text=True)
                self.assertIn('rel="icon"', body)
                self.assertIn("images/favicon.svg", body)
                self.assertIn("images/favicon.ico", body)
                self.assertIn("images/apple-touch-icon.png", body)

    def test_admin_pages_declare_icon(self) -> None:
        """后台页面同样需要图标，登录页在未认证状态下就能验证。"""
        app = create_app(self.application_config())
        body = app.test_client().get("/admin/login").get_data(as_text=True)

        self.assertIn("images/favicon.svg", body)
        self.assertIn("images/favicon.ico", body)

    def test_root_favicon_route_serves_icon(self) -> None:
        """根路径的老约定请求必须返回图标而不是 404。"""
        app = create_app(self.application_config())

        response = app.test_client().get("/favicon.ico")
        # send_from_directory 的响应持有打开的文件句柄，不显式关闭会留下
        # ResourceWarning，把真正需要关注的告警淹掉
        self.addCleanup(response.close)

        self.assertEqual(200, response.status_code)
        self.assertIn("icon", response.headers["Content-Type"])
        self.assertIn("max-age", response.headers.get("Cache-Control", ""))
        # 返回的必须是能解码的 ICO，而不是某个错误页的 HTML
        self.assertEqual(b"\x00\x00\x01\x00", response.get_data()[:4])

    def test_static_icon_endpoints_are_public(self) -> None:
        """图标不能要求登录：登录页自身也要显示它。"""
        app = create_app(self.application_config())
        client = app.test_client()

        for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
            with self.subTest(name=name):
                response = client.get(f"/static/images/{name}")
                self.addCleanup(response.close)
                self.assertEqual(200, response.status_code)
