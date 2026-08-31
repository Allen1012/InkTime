"""验证通用容器入口的跨进程锁、失败门禁和命令转交。"""

import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import container_entrypoint
from src import migrations
from src.database import write_transaction
from src.database_backup import collect_baseline
from src.migrations import (
    DEFAULT_MIGRATIONS_DIR,
    EXPECTED_SCHEMA_VERSIONS,
    SCHEMA_TARGET_VERSION,
    assert_current_schema,
    migrate_database,
    pending_migration_versions,
)


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
        # 引用目标版本而不是写死条数，避免每次新增迁移都要来改这一行
        self.assertCountEqual([("ok", SCHEMA_TARGET_VERSION), ("ok", 0)], results)
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


class AutoMigrateOnStartTestCase(unittest.TestCase):
    """验证启动期自动升级开关的边界、备份强制性与默认严格行为。"""

    def setUp(self) -> None:
        """准备一个落后于当前代码的已有数据库。"""
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="inktime-auto-migrate-tests-"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.database_path = self.root / "data" / "photos.db"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def _create_outdated_database(self, missing_count: int = 2) -> list[int]:
        """只用迁移文件子集建库，真实复现落后若干版本的旧数据库。

        直接删除迁移台账记录会留下已生效的 DDL，导致补齐时重复建列，
        无法代表真实旧库；因此这里把「旧代码」的迁移目录与版本常量一起
        替换，建出的库在结构和台账两侧都真的停在旧版本。

        Args:
            missing_count: 相对当前代码缺少的末尾迁移数量。

        Returns:
            升序排列的缺失迁移版本列表。
        """
        target = len(EXPECTED_SCHEMA_VERSIONS) - missing_count
        subset_directory = self.root / f"migrations-{target}"
        subset_directory.mkdir(parents=True, exist_ok=True)
        for source in sorted(DEFAULT_MIGRATIONS_DIR.glob("*.sql"))[:target]:
            shutil.copy2(source, subset_directory / source.name)

        with patch.object(migrations, "SCHEMA_TARGET_VERSION", target), patch.object(
            migrations, "EXPECTED_SCHEMA_VERSIONS", tuple(range(1, target + 1))
        ), patch.object(
            migrations, "DEFAULT_MIGRATIONS_DIR", subset_directory
        ), patch.object(
            migrations, "assert_current_schema", lambda path: None
        ):
            migrate_database(self.database_path, migrations_dir=subset_directory)

        return list(EXPECTED_SCHEMA_VERSIONS[target:])

    def test_disabled_switch_rejects_outdated_database(self) -> None:
        """默认关闭时落后的数据库必须拒绝启动且不产生备份。"""
        self._create_outdated_database()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTO_MIGRATE_ON_START", None)
            with self.assertRaisesRegex(RuntimeError, "迁移版本必须恰好完整"):
                container_entrypoint._prepare_database(self.database_path)

        self.assertFalse((self.database_path.parent / "backups").exists())

    def test_enabled_switch_backs_up_then_upgrades(self) -> None:
        """开启后应先生成可校验的备份，再把数据库补齐到最新结构。"""
        removed = self._create_outdated_database()
        with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": "true"}):
            applied = container_entrypoint._prepare_database(self.database_path)

        self.assertEqual(len(removed), len(applied))
        assert_current_schema(self.database_path)

        backups = sorted((self.database_path.parent / "backups").glob("photos-*.db"))
        baselines = sorted(
            (self.database_path.parent / "backups").glob("photos-*.baseline.json")
        )
        self.assertEqual(1, len(backups))
        self.assertEqual(1, len(baselines))
        backup_baseline = collect_baseline(backups[0])
        self.assertEqual("ok", backup_baseline["integrity_check"])
        recorded = json.loads(baselines[0].read_text(encoding="utf-8"))
        self.assertEqual(str(backups[0]), recorded["backup_database"])
        self.assertEqual(
            backup_baseline["photo_identity_sha256"],
            recorded["photo_identity_sha256"],
        )

    def test_backup_failure_aborts_upgrade_without_touching_database(self) -> None:
        """备份失败必须中止升级，数据库结构保持原样。"""
        removed = self._create_outdated_database()
        with patch.dict(
            os.environ, {"AUTO_MIGRATE_ON_START": "true"}
        ), patch.object(
            container_entrypoint,
            "create_backup",
            side_effect=RuntimeError("backup device full"),
        ):
            with self.assertRaisesRegex(RuntimeError, "backup device full"):
                container_entrypoint._prepare_database(self.database_path)

        self.assertEqual(removed, pending_migration_versions(self.database_path))

    def test_enabled_switch_skips_backup_when_already_current(self) -> None:
        """结构已是最新时不应产生任何备份，避免每次重启堆积文件。"""
        migrate_database(self.database_path)
        with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": "true"}):
            applied = container_entrypoint._prepare_database(self.database_path)

        self.assertEqual([], applied)
        self.assertFalse((self.database_path.parent / "backups").exists())

    def test_enabled_switch_rejects_unknown_migration_version(self) -> None:
        """台账含未知版本属于分叉历史，自动升级也必须拒绝且不备份。"""
        migrate_database(self.database_path)
        with write_transaction(self.database_path) as connection:
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (9999, "from_future", "0" * 64, "2026-01-01T00:00:00+00:00"),
            )
        with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": "true"}):
            with self.assertRaisesRegex(RuntimeError, "未知的迁移版本"):
                container_entrypoint._prepare_database(self.database_path)

        self.assertFalse((self.database_path.parent / "backups").exists())

    def test_enabled_switch_initializes_brand_new_database(self) -> None:
        """全新库无需备份，开关开启时也应一次迁移到最新。"""
        with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": "true"}):
            applied = container_entrypoint._prepare_database(self.database_path)

        self.assertEqual(len(EXPECTED_SCHEMA_VERSIONS), len(applied))
        assert_current_schema(self.database_path)
        self.assertFalse((self.database_path.parent / "backups").exists())

    def test_enabled_switch_rejects_empty_database_file(self) -> None:
        """零字节数据库是挂载或中断异常，自动升级不得覆盖。"""
        self.database_path.touch()
        with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": "true"}):
            with self.assertRaisesRegex(RuntimeError, "数据库文件为空"):
                container_entrypoint._prepare_database(self.database_path)

    def test_switch_accepts_documented_true_values_only(self) -> None:
        """开关只接受文档约定的真值，其余一律视为关闭。"""
        for raw in ("1", "true", "TRUE", " yes ", "on"):
            with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": raw}):
                self.assertTrue(container_entrypoint._auto_migrate_enabled(), raw)
        for raw in ("", "0", "false", "no", "off", "auto"):
            with patch.dict(os.environ, {"AUTO_MIGRATE_ON_START": raw}):
                self.assertFalse(container_entrypoint._auto_migrate_enabled(), raw)

    def test_backup_directory_honours_explicit_configuration(self) -> None:
        """显式配置的备份目录优先于数据库同级默认目录。"""
        custom = self.root / "elsewhere" / "db-backups"
        with patch.dict(os.environ, {"DB_BACKUP_DIR": str(custom)}):
            self.assertEqual(
                custom.resolve(),
                container_entrypoint._backup_directory(self.database_path),
            )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_BACKUP_DIR", None)
            self.assertEqual(
                self.database_path.parent / "backups",
                container_entrypoint._backup_directory(self.database_path),
            )


if __name__ == "__main__":
    unittest.main()
