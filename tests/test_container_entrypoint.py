"""验证通用容器入口的跨进程锁、失败门禁和命令转交。"""

import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import container_entrypoint
from src.migrations import assert_current_schema


def _prepare_database_worker(database_path: str, start_event, result_queue) -> None:
    """等待统一起跑信号后在独立进程中准备同一个数据库。"""
    try:
        if not start_event.wait(timeout=10):
            result_queue.put(("error", "start timeout"))
            return
        applied = container_entrypoint._prepare_database(Path(database_path))
        result_queue.put(("ok", len(applied)))
    except Exception as error:  # pragma: no cover - 失败内容由父进程断言
        result_queue.put(("error", f"{type(error).__name__}: {error}"))


class ContainerEntrypointTestCase(unittest.TestCase):
    """验证容器入口在数据库就绪前绝不转交服务命令。"""

    def setUp(self) -> None:
        """创建不接触仓库数据的临时路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="inktime-container-entrypoint-tests-"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.database_path = self.root / "data" / "photos.db"

    def test_concurrent_prepare_initializes_database_once(self) -> None:
        """两个进程并发准备同一空路径时只能有一个执行迁移。"""
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_prepare_database_worker,
                args=(str(self.database_path), start_event, result_queue),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=60)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail("容器入口并发测试进程超时")
            self.assertEqual(0, process.exitcode)

        results = [result_queue.get(timeout=5) for _ in processes]
        self.assertCountEqual([("ok", 52), ("ok", 0)], results)
        assert_current_schema(self.database_path)
        lock_path = self.database_path.with_name("photos.db.initialize.lock")
        self.assertEqual(0o600, stat.S_IMODE(lock_path.stat().st_mode))

    def test_main_prepares_directories_and_database_before_forwarding_command(self) -> None:
        """目标命令只能在运行目录和数据库准备成功后原样转交。"""
        events: list[object] = []

        def prepare_directories(path: Path) -> None:
            events.append(("prepare-directories", path))

        def prepare_database(path: Path) -> list[str]:
            events.append(("prepare-database", path))
            return []

        def execute(command) -> None:
            events.append(("execute", list(command)))

        with patch.dict(os.environ, {"DB_PATH": str(self.database_path)}), patch.object(
            container_entrypoint,
            "_prepare_runtime_directories",
            side_effect=prepare_directories,
        ), patch.object(
            container_entrypoint, "_prepare_database", side_effect=prepare_database
        ), patch.object(container_entrypoint, "_execute", side_effect=execute):
            container_entrypoint.main(["python", "-V"])

        self.assertEqual(
            [
                ("prepare-directories", self.database_path.resolve()),
                ("prepare-database", self.database_path.resolve()),
                ("execute", ["python", "-V"]),
            ],
            events,
        )

    def test_prepare_runtime_directories_creates_configured_paths(self) -> None:
        """零配置入口应创建数据库、输出和全部照片根目录。"""
        primary = self.root / "photos"
        secondary = self.root / "archive"
        output = self.root / "rendered"
        with patch.dict(
            os.environ,
            {
                "IMAGE_DIR": f"{primary};{secondary}",
                "BIN_OUTPUT_DIR": str(output),
            },
        ):
            container_entrypoint._prepare_runtime_directories(self.database_path)

        for directory in (self.database_path.parent, primary, secondary, output):
            self.assertTrue(directory.is_dir())

    def test_execute_replaces_process_with_exact_command(self) -> None:
        """命令转交应使用首项作为可执行文件并保留完整参数。"""
        with patch("src.container_entrypoint.os.execvp") as execvp:
            container_entrypoint._execute(["python", "-m", "src.server.run_server"])

        execvp.assert_called_once_with(
            "python", ["python", "-m", "src.server.run_server"]
        )

    def test_empty_command_exits_before_database_preparation(self) -> None:
        """空目标命令应在任何数据库操作前失败。"""
        with patch.object(container_entrypoint, "_prepare_database") as prepare:
            with self.assertRaisesRegex(SystemExit, "容器启动命令不能为空"):
                container_entrypoint.main([])

        prepare.assert_not_called()

    def test_prepare_failure_does_not_forward_command(self) -> None:
        """数据库准备失败时应返回非零状态且不执行目标命令。"""
        with patch.dict(os.environ, {"DB_PATH": str(self.database_path)}), patch.object(
            container_entrypoint, "_prepare_runtime_directories"
        ), patch.object(
            container_entrypoint,
            "_prepare_database",
            side_effect=RuntimeError("database rejected"),
        ), patch.object(container_entrypoint, "_execute") as execute:
            with self.assertRaises(SystemExit) as raised:
                container_entrypoint.main(["python", "-V"])

        self.assertEqual(1, raised.exception.code)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
