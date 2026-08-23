"""验证容器首次启动时的新库与异常路径判定。"""

import tempfile
import unittest
from pathlib import Path

from src.migrations import assert_current_schema, initialize_database_if_new


class DatabaseInitializationTestCase(unittest.TestCase):
    """验证首次初始化只创建不存在的数据库。"""

    def setUp(self) -> None:
        """为每个用例准备完全隔离且初始不存在的数据库路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="inktime-database-initialization-tests-"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.database_path = self.root / "photos.db"

    def test_missing_database_is_migrated(self) -> None:
        """数据库不存在时应完整迁移到当前版本。"""
        applied = initialize_database_if_new(self.database_path)

        self.assertEqual(52, len(applied))
        self.assertTrue(self.database_path.is_file())
        self.assertGreater(self.database_path.stat().st_size, 0)
        assert_current_schema(self.database_path)

    def test_existing_current_database_is_only_checked(self) -> None:
        """当前版本数据库再次初始化时应只检查且不重复迁移。"""
        initialize_database_if_new(self.database_path)

        applied = initialize_database_if_new(self.database_path)

        self.assertEqual([], applied)
        assert_current_schema(self.database_path)

    def test_zero_byte_database_is_rejected_without_overwrite(self) -> None:
        """零字节数据库应保留现场并拒绝自动覆盖。"""
        self.database_path.touch()

        with self.assertRaisesRegex(RuntimeError, "数据库文件为空"):
            initialize_database_if_new(self.database_path)

        self.assertEqual(0, self.database_path.stat().st_size)

    def test_non_regular_database_path_is_rejected(self) -> None:
        """目录等非普通文件路径不得被迁移器替换。"""
        self.database_path.mkdir()

        with self.assertRaisesRegex(RuntimeError, "不是普通文件"):
            initialize_database_if_new(self.database_path)

        self.assertTrue(self.database_path.is_dir())


if __name__ == "__main__":
    unittest.main()
