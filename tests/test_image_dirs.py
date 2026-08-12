"""多照片目录配置解析的单元测试。

按阶段三要求先写测试再写实现：嵌套检测是本次改造中安全性最关键的一条校验，
一旦父子目录同时被配置，子目录回收站里的文件对父目录来说会变成「合法的活动
区路径」，已删除照片可能通过公开接口泄露。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.configuration import parse_image_dirs


class ParseImageDirsTestCase(unittest.TestCase):
    """校验分号分隔多目录的解析、去重、嵌套拒绝与存在性检查。"""

    def setUp(self) -> None:
        """创建隔离的临时目录树。"""
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="inktime-dirs-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.first = self.root / "pic"
        self.second = self.root / "nas"
        self.nested = self.first / "private"
        for path in (self.first, self.second, self.nested):
            path.mkdir(parents=True)

    def test_single_directory_keeps_previous_behavior(self) -> None:
        """验证无分号时解析为单个根目录。"""
        self.assertEqual((self.first,), parse_image_dirs(str(self.first)))

    def test_multiple_directories_keep_order_with_primary_first(self) -> None:
        """验证多目录按配置顺序返回，第一个是写入用的主目录。"""
        raw = f"{self.first};{self.second}"
        parsed = parse_image_dirs(raw)
        self.assertEqual((self.first, self.second), parsed)
        self.assertEqual(self.first, parsed[0])

    def test_blank_segments_are_ignored(self) -> None:
        """验证多余分号与空白段被忽略，不影响解析结果。"""
        raw = f" {self.first} ;; {self.second} ;"
        self.assertEqual((self.first, self.second), parse_image_dirs(raw))

    def test_duplicate_directories_are_deduplicated(self) -> None:
        """验证重复目录在解析后自动去重且保留首次出现顺序。"""
        raw = f"{self.first};{self.second};{self.first}/.;{self.first}"
        self.assertEqual((self.first, self.second), parse_image_dirs(raw))

    def test_relative_directory_resolves_against_base(self) -> None:
        """验证相对路径按给定基准目录解析为绝对路径。"""
        self.assertEqual(
            (self.first,), parse_image_dirs("pic", base_dir=self.root)
        )

    def test_nested_directories_are_rejected_in_both_orders(self) -> None:
        """验证父子目录同时配置时无论顺序都被拒绝。"""
        for raw in (
            f"{self.first};{self.nested}",
            f"{self.nested};{self.first}",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as captured:
                    parse_image_dirs(raw)
                self.assertIn("嵌套", str(captured.exception))

    def test_symlinked_directory_inside_another_root_is_rejected(self) -> None:
        """验证通过符号链接绕过嵌套检测的写法同样被拒绝。"""
        link = self.root / "link-to-private"
        link.symlink_to(self.nested, target_is_directory=True)
        with self.assertRaises(ValueError) as captured:
            parse_image_dirs(f"{self.first};{link}")
        self.assertIn("嵌套", str(captured.exception))

    def test_empty_configuration_is_rejected(self) -> None:
        """验证空值或仅分隔符的配置被拒绝。"""
        for raw in ("", "   ", ";", ";;"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_image_dirs(raw)

    def test_filesystem_root_is_rejected(self) -> None:
        """验证文件系统根目录不允许作为照片目录。"""
        with self.assertRaises(ValueError) as captured:
            parse_image_dirs("/")
        self.assertIn("根目录", str(captured.exception))

    def test_missing_directory_is_rejected_only_when_existence_required(self) -> None:
        """验证存在性检查按需开启，读取路径不因目录暂时不可用而中断。"""
        raw = f"{self.first};{self.root / 'not-mounted'}"
        self.assertEqual(2, len(parse_image_dirs(raw)))
        with self.assertRaises(ValueError) as captured:
            parse_image_dirs(raw, require_existing=True)
        self.assertIn("不存在", str(captured.exception))

    def test_file_path_is_rejected_when_existence_required(self) -> None:
        """验证指向普通文件的配置在写入校验时被拒绝。"""
        target = self.root / "a-file.txt"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError) as captured:
            parse_image_dirs(str(target), require_existing=True)
        self.assertIn("不是目录", str(captured.exception))

    @unittest.skipIf(os.geteuid() == 0, "root 用户会绕过目录权限检查")
    def test_unreadable_directory_is_rejected_when_existence_required(self) -> None:
        """验证不可读目录在写入校验时被拒绝。"""
        blocked = self.root / "blocked"
        blocked.mkdir()
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o700)
        with self.assertRaises(ValueError) as captured:
            parse_image_dirs(str(blocked), require_existing=True)
        self.assertIn("不可读", str(captured.exception))

    def test_path_like_value_is_accepted(self) -> None:
        """验证直接传入 Path 对象与传入字符串等价。"""
        self.assertEqual((self.first,), parse_image_dirs(self.first))
