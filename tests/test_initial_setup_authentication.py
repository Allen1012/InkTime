"""验证首次管理员初始化令牌与凭据边界。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.security import check_password_hash

from src.server.auth import (
    AuthenticationService,
    InitialSetupConfigurationError,
    InvalidInitialSetupTokenError,
)
from src.server.repositories import AdminUserRepository


class AuthenticationServiceInitialSetupTestCase(unittest.TestCase):
    """验证令牌加载、比较顺序和首管理员密码哈希。"""

    TOKEN = "a" * 24
    PASSWORD = "twelve-chars-password"

    def setUp(self) -> None:
        """创建隔离路径和带仓储接口约束的模拟对象。"""
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="inktime-initial-setup-auth-tests-"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.repository = Mock(spec=AdminUserRepository)
        self.repository.create_first_admin.return_value = 7

    def service(
        self,
        *,
        inline_token: str | None = None,
        token_file: Path | None = None,
    ) -> AuthenticationService:
        """使用隔离路径和指定令牌来源构造认证服务。"""
        return AuthenticationService(
            self.repository,
            self.root / "limiter.db",
            "test-secret-key",
            5,
            300,
            initial_setup_token=inline_token,
            initial_setup_token_file=token_file,
        )

    def test_inline_token_creates_normalized_admin_with_password_hash(self) -> None:
        """正确环境令牌应规范化用户名并只把密码哈希交给仓储。"""
        service = self.service(inline_token=self.TOKEN)

        admin_id = service.create_first_admin(
            "  first-admin  ", self.PASSWORD, self.TOKEN
        )

        self.assertEqual(7, admin_id)
        username, password_hash = self.repository.create_first_admin.call_args.args
        self.assertEqual("first-admin", username)
        self.assertNotEqual(self.PASSWORD, password_hash)
        self.assertTrue(check_password_hash(password_hash, self.PASSWORD))

    def test_missing_token_allows_first_admin_creation(self) -> None:
        """未配置初始化令牌时应允许局域网首位访问者创建管理员。"""
        service = self.service()

        admin_id = service.create_first_admin("first-admin", self.PASSWORD)

        self.assertEqual(7, admin_id)
        self.assertFalse(service.initial_setup_token_required)
        self.repository.create_first_admin.assert_called_once()

    def test_invalid_configured_token_is_rejected_before_password_hash(self) -> None:
        """已配置但不匹配的令牌应拒绝且不计算密码哈希。"""
        service = self.service(inline_token=self.TOKEN)
        self.assertTrue(service.initial_setup_token_required)

        with patch("src.server.auth.generate_password_hash") as generate_hash:
            with self.assertRaises(InvalidInitialSetupTokenError):
                service.create_first_admin(
                    "first-admin", self.PASSWORD, "wrong-token"
                )

        generate_hash.assert_not_called()
        self.repository.create_first_admin.assert_not_called()

    def test_token_source_conflict_and_inline_boundaries_are_rejected(self) -> None:
        """双来源、短令牌和任意空白都应在服务构造时拒绝。"""
        token_file = self.root / "setup-token"
        token_file.write_text(self.TOKEN, encoding="utf-8")

        with self.assertRaises(InitialSetupConfigurationError):
            self.service(inline_token=self.TOKEN, token_file=token_file)
        for invalid in ("a" * 23, "a" * 23 + " ", "a" * 24 + "\n"):
            with self.subTest(invalid_length=len(invalid)):
                with self.assertRaises(InitialSetupConfigurationError):
                    self.service(inline_token=invalid)

        self.service(inline_token=self.TOKEN)

    def test_file_token_accepts_no_newline_or_one_common_newline(self) -> None:
        """密钥文件允许无换行、一个换行或一个回车换行。"""
        for index, suffix in enumerate(("", "\n", "\r\n")):
            with self.subTest(suffix=repr(suffix)):
                self.repository.reset_mock()
                token_file = self.root / f"setup-token-{index}"
                token_file.write_text(self.TOKEN + suffix, encoding="utf-8")
                service = self.service(token_file=token_file)

                service.create_first_admin(
                    "first-admin", self.PASSWORD, self.TOKEN
                )

                self.repository.create_first_admin.assert_called_once()

    def test_invalid_token_files_are_rejected(self) -> None:
        """缺失、目录、非 UTF-8、超限和双换行文件都应拒绝。"""
        missing = self.root / "missing-token"
        directory = self.root / "token-directory"
        directory.mkdir()
        invalid_utf8 = self.root / "invalid-utf8"
        invalid_utf8.write_bytes(b"\xff\xfe")
        oversized = self.root / "oversized"
        oversized.write_bytes(b"a" * 4097)
        double_newline = self.root / "double-newline"
        double_newline.write_text(self.TOKEN + "\n\n", encoding="utf-8")

        for path in (missing, directory, invalid_utf8, oversized, double_newline):
            with self.subTest(path=path.name):
                with self.assertRaises(InitialSetupConfigurationError):
                    self.service(token_file=path)

    def test_admin_credential_boundaries_are_enforced(self) -> None:
        """用户名与密码边界应与普通管理员创建规则完全一致。"""
        service = self.service(inline_token=self.TOKEN)
        invalid_values = (
            ("", self.PASSWORD),
            ("   ", self.PASSWORD),
            ("a" * 129, self.PASSWORD),
            ("first-admin", "a" * 11),
        )
        for username, password in invalid_values:
            with self.subTest(username_length=len(username), password_length=len(password)):
                self.repository.reset_mock()
                with self.assertRaises(ValueError):
                    service.create_first_admin(username, password, self.TOKEN)
                self.repository.create_first_admin.assert_not_called()

        service.create_first_admin("a" * 128, "b" * 12, self.TOKEN)
        self.repository.create_first_admin.assert_called_once()


if __name__ == "__main__":
    unittest.main()
