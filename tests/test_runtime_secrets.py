"""验证零配置运行时密钥的持久化、覆盖优先级和失败门禁。"""

import stat

from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


class RuntimeSecretsTestCase(TemporaryDatabaseTestCase):
    """使用真实应用工厂验证数据库同目录密钥文件。"""

    def zero_config(self) -> dict:
        """返回清空两项安全密钥的隔离应用配置。"""
        config = self.application_config()
        config.update({"SECRET_KEY": "", "DOWNLOAD_KEY": ""})
        return config

    def test_missing_secrets_are_generated_once_and_reused(self) -> None:
        """首次启动应生成 0600 文件，后续启动必须复用完全相同的值。"""
        first = create_app(self.zero_config())
        second = create_app(self.zero_config())

        self.assertGreaterEqual(len(first.config["SECRET_KEY"]), 24)
        self.assertGreaterEqual(len(first.config["DOWNLOAD_KEY"]), 24)
        self.assertEqual(first.config["SECRET_KEY"], second.config["SECRET_KEY"])
        self.assertEqual(first.config["DOWNLOAD_KEY"], second.config["DOWNLOAD_KEY"])
        for filename in (".inktime-secret-key", ".inktime-download-key"):
            path = self.database_path.parent / filename
            self.assertTrue(path.is_file())
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_explicit_secrets_take_precedence_without_creating_files(self) -> None:
        """显式环境覆盖应保持原值，且不额外写自动密钥文件。"""
        config = self.application_config()
        config["DOWNLOAD_KEY"] = "explicit-download-key-123456789"

        application = create_app(config)

        self.assertEqual(config["SECRET_KEY"], application.config["SECRET_KEY"])
        self.assertEqual(config["DOWNLOAD_KEY"], application.config["DOWNLOAD_KEY"])
        self.assertFalse((self.database_path.parent / ".inktime-secret-key").exists())
        self.assertFalse((self.database_path.parent / ".inktime-download-key").exists())

    def test_invalid_persisted_secret_fails_closed(self) -> None:
        """损坏的持久化密钥不得静默重生并改变会话或设备地址。"""
        secret_path = self.database_path.parent / ".inktime-secret-key"
        secret_path.write_text("too-short", encoding="utf-8")
        secret_path.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "SECRET_KEY 持久化文件内容无效"):
            create_app(self.zero_config())


if __name__ == "__main__":
    import unittest

    unittest.main()
