"""文件大小可读化的回归测试。

后台详情页原本显示 `4630341 字节`，读起来要自己数位数。同一个后台里当时还并存
三种口径：详情页用「字节」、上传页用 MiB/KiB、JS 里又是另一份实现。本用例固定
统一后的口径：1024 进制、B/KB/MB/GB/TB 标签。
"""

from __future__ import annotations

import re

from src.server.app import create_app
from src.server.formatting import readable_size
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class ReadableSizeTestCase(TemporaryDatabaseTestCase):
    """校验换算、进位边界与非法输入。"""

    def test_bytes_below_one_kilobyte_keep_chinese_unit(self) -> None:
        """不足 1 KB 保留中文「字节」：这个量级本来就好读，中文界面里更自然。"""
        self.assertEqual("0 字节", readable_size(0))
        self.assertEqual("1 字节", readable_size(1))
        self.assertEqual("1023 字节", readable_size(1023))

    def test_kilobyte_and_megabyte_boundaries(self) -> None:
        """恰好进位时落到新单位，且整数不带多余的 .0。"""
        self.assertEqual("1 KB", readable_size(1024))
        self.assertEqual("1 MB", readable_size(1048576))
        self.assertEqual("1 GB", readable_size(1073741824))
        self.assertEqual("1 TB", readable_size(1099511627776))

    def test_fractional_sizes_keep_one_decimal(self) -> None:
        """保留一位小数，够看又不啰嗦。"""
        self.assertEqual("1.5 KB", readable_size(1536))
        self.assertEqual("4.4 MB", readable_size(4630341))
        self.assertEqual("2.5 GB", readable_size(2684354560))

    def test_terabyte_is_the_last_tier(self) -> None:
        """超出 TB 不再进位，避免出现未定义的单位。"""
        self.assertEqual("5 TB", readable_size(5 * 1099511627776))

    def test_numeric_string_accepted(self) -> None:
        """数据库或表单可能给出字符串形式的字节数。"""
        self.assertEqual("4.4 MB", readable_size("4630341"))

    def test_invalid_values_return_empty(self) -> None:
        """空值与非法值返回空串，由模板决定显示「未知」。"""
        for value in (None, "", "abc", -1, True, False, [], {}):
            self.assertEqual("", readable_size(value), f"值 {value!r} 应返回空串")

    def test_no_binary_prefix_labels(self) -> None:
        """统一用常用标签，不再出现 MiB/KiB。"""
        for value in (2048, 5242880, 2147483648):
            rendered = readable_size(value)
            self.assertNotIn("iB", rendered)


class PhotoDetailSizeRenderTestCase(TemporaryDatabaseTestCase):
    """校验详情页真的用上了可读大小。"""

    ADMIN_USERNAME = "size-admin"
    ADMIN_PASSWORD = "inktime-readable-size-password"

    def setUp(self) -> None:
        """创建应用与已登录会话。"""
        super().setUp()
        self.application = create_app(self.application_config())
        with self.application.app_context():
            self.application.extensions["inktime_services"]["auth"].create_admin(
                self.ADMIN_USERNAME, self.ADMIN_PASSWORD
            )
        self.client = self.application.test_client()
        body = self.client.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token, "登录页应包含跨站请求伪造令牌")
        response = self.client.post(
            "/admin/login",
            data={
                "username": self.ADMIN_USERNAME,
                "password": self.ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
        )
        self.assertIn(response.status_code, (302, 303))

    def test_detail_page_shows_readable_size(self) -> None:
        """详情页展示 MB 而不是裸字节数。"""
        photo_id = self.create_photo("sized.jpg")
        target = self.image_directory / "sized.jpg"
        target.write_bytes(b"x" * 4630341)

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn("4.4 MB", body)
        self.assertNotIn("4630341", body)
        self.assertNotIn("4630341 字节", body)

    def test_api_keeps_raw_byte_count(self) -> None:
        """接口继续返回原始字节数：格式化是展示层的事，机器读取要精确值。"""
        photo_id = self.create_photo("api-sized.jpg")
        target = self.image_directory / "api-sized.jpg"
        target.write_bytes(b"x" * 4630341)

        with self.application.test_request_context(f"/admin/photos/{photo_id}"):
            service = self.application.extensions["inktime_services"]["admin_photo"]
            detail = service.detail(photo_id)

        self.assertEqual(4630341, detail["file"]["size"])
