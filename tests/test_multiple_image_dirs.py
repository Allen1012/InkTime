"""多照片目录在扫描、上传、回收站与公开接口上的行为测试。"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from src.configuration import (
    IMAGE_DIR_SEPARATOR,
    ConfigurationActor,
    ConfigurationValidationError,
    like_prefix,
)
from src.server.app import create_app
from src.server.errors import PermissionDeniedError, ResourceNotFoundError
from tests.support import TemporaryDatabaseTestCase


class _FakeUpload:
    """模拟 Werkzeug FileStorage 的最小上传对象。"""

    def __init__(self, filename: str, payload: bytes) -> None:
        """保存文件名与字节流。"""
        self.filename = filename
        self.stream = io.BytesIO(payload)


def _write_jpeg(path: Path, size: int = 48) -> None:
    """在指定位置写入可被真实解码的 JPEG 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (size, size), (30, 90, 150)).save(path, format="JPEG", quality=90)


def _jpeg_bytes(size: int = 48) -> bytes:
    """生成可被真实解码的 JPEG 负载。"""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (90, 30, 150)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class MultipleImageDirectoriesTestCase(TemporaryDatabaseTestCase):
    """校验主目录与附加目录在各条链路上的一致行为。"""

    def setUp(self) -> None:
        """准备主目录、附加目录与已登录管理员身份。"""
        super().setUp()
        self.secondary_directory = (self.temporary_path / "nas").resolve()
        self.secondary_directory.mkdir(parents=True)
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")

    def multi_directory_config(self) -> dict:
        """返回同时配置两个照片目录的应用配置。"""
        config = self.application_config()
        config["IMAGE_DIR"] = IMAGE_DIR_SEPARATOR.join(
            (str(self.image_directory), str(self.secondary_directory))
        )
        return config

    def create_photo_in(
        self, directory: Path, filename: str, *, analysis_status: str = "succeeded"
    ) -> int:
        """在指定目录写入真实文件并登记照片记录。"""
        path = (directory / filename).resolve()
        _write_jpeg(path)
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,caption,type,memory_score,beauty_score,"
                "exif_datetime,analysis_status,is_deleted,created_at,updated_at,version) "
                "VALUES (?,?,?,?,?,?,?,0,?,?,1)",
                (
                    str(path), f"caption-{filename}", "family", 90.0, 80.0,
                    "2024:05:01 09:00:00", analysis_status,
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                ),
            )
            return int(cursor.lastrowid)

    def test_application_normalizes_multiple_directories(self) -> None:
        """验证启动时把多目录规范化为绝对路径列表，主目录在最前。"""
        app = create_app(self.multi_directory_config())
        self.assertEqual(
            (self.image_directory, self.secondary_directory), app.config["IMAGE_DIRS"]
        )
        self.assertEqual(
            IMAGE_DIR_SEPARATOR.join(
                (str(self.image_directory), str(self.secondary_directory))
            ),
            app.config["IMAGE_DIR"],
        )

    def test_single_directory_behavior_is_unchanged(self) -> None:
        """验证不含分号的配置与改造前完全一致。"""
        app = create_app(self.application_config())
        self.assertEqual((self.image_directory,), app.config["IMAGE_DIRS"])

    def test_scan_registers_photos_from_every_directory(self) -> None:
        """验证扫描收录全部目录的照片，并跳过各根自己的回收站。"""
        _write_jpeg(self.image_directory / "primary.jpg")
        _write_jpeg(self.secondary_directory / "album" / "secondary.jpg")
        _write_jpeg(self.image_directory / ".trash" / "1" / "deleted-primary.jpg")
        _write_jpeg(self.secondary_directory / ".trash" / "2" / "deleted-secondary.jpg")

        app = create_app(self.multi_directory_config())
        result = app.extensions["inktime_services"]["library_scan"].scan(self.user_id)

        self.assertEqual(2, result["discovered"])
        self.assertEqual(2, result["registered"])
        with self.database() as connection:
            paths = {
                str(row["path"])
                for row in connection.execute("SELECT path FROM photo_scores")
            }
        self.assertEqual(
            {
                str(self.image_directory / "primary.jpg"),
                str(self.secondary_directory / "album" / "secondary.jpg"),
            },
            paths,
        )

    def test_uploads_always_land_in_primary_directory(self) -> None:
        """验证上传只写主目录，附加目录不被写入。"""
        app = create_app(self.multi_directory_config())
        result = app.extensions["inktime_services"]["uploads"].upload(
            [_FakeUpload("new.jpg", _jpeg_bytes())], self.user_id
        )

        self.assertEqual(1, result["counts"]["accepted"])
        stored = Path(result["items"][0]["path"]).resolve()
        self.assertTrue(stored.is_relative_to(self.image_directory))
        self.assertEqual([], list(self.secondary_directory.rglob("*.jpg")))

    def test_photo_in_secondary_directory_is_publicly_readable(self) -> None:
        """验证附加目录的照片可通过公开接口浏览。"""
        photo_id = self.create_photo_in(self.secondary_directory, "visible.jpg")
        app = create_app(self.multi_directory_config())
        client = app.test_client()

        listing = client.get("/api/photos?page=1&limit=10").get_json()
        self.assertIn(photo_id, [item["id"] for item in listing["data"]["items"]])
        full = client.get(
            "/api/photo/full",
            query_string={"path": str(self.secondary_directory / "visible.jpg")},
        )
        self.assertEqual(200, full.status_code)

    def test_soft_delete_and_restore_use_the_owning_root_trash(self) -> None:
        """验证附加目录照片移入本根回收站并可恢复，回收站不跨根。"""
        photo_id = self.create_photo_in(self.secondary_directory, "movable.jpg")
        app = create_app(self.multi_directory_config())
        lifecycle = app.extensions["inktime_services"]["photo_lifecycle"]

        lifecycle.soft_delete(photo_id, 1, self.user_id, "test-admin")

        row = self.read_photo(photo_id)
        trashed = Path(str(row["trash_path"])).resolve()
        self.assertTrue(trashed.is_relative_to(self.secondary_directory / ".trash"))
        self.assertTrue(trashed.is_file())
        self.assertFalse((self.secondary_directory / "movable.jpg").exists())
        # 主目录回收站只会因为锁文件而存在，不应出现该照片的回收站目录。
        self.assertFalse((self.image_directory / ".trash" / str(photo_id)).exists())

        lifecycle.restore(photo_id, int(row["version"]), self.user_id, "test-admin")

        restored = Path(str(self.read_photo(photo_id)["path"])).resolve()
        self.assertEqual(self.secondary_directory / "movable.jpg", restored)
        self.assertTrue(restored.is_file())
        self.assertFalse(trashed.exists())

    def test_trash_files_are_not_reachable_through_public_media(self) -> None:
        """验证任意根的回收站文件都无法通过公开媒体接口访问。"""
        app = create_app(self.multi_directory_config())
        media = app.extensions["inktime_services"]["media"]
        for root in (self.image_directory, self.secondary_directory):
            hidden = root / ".trash" / "9" / "secret.jpg"
            _write_jpeg(hidden)
            with self.subTest(root=root):
                with self.assertRaises(ResourceNotFoundError):
                    media.resolve_photo(str(hidden))

    def test_path_outside_every_directory_is_rejected(self) -> None:
        """验证不属于任何已配置目录的路径被拒绝。"""
        outside = self.temporary_path / "outside" / "x.jpg"
        _write_jpeg(outside)
        app = create_app(self.multi_directory_config())
        media = app.extensions["inktime_services"]["media"]
        with self.assertRaises(PermissionDeniedError):
            media.resolve_photo(str(outside))

    def test_purge_only_touches_the_owning_root_trash(self) -> None:
        """验证永久删除只清理照片所在根的回收站文件。"""
        photo_id = self.create_photo_in(self.secondary_directory, "purge-me.jpg")
        keeper_id = self.create_photo_in(self.image_directory, "keep-me.jpg")
        app = create_app(self.multi_directory_config())
        lifecycle = app.extensions["inktime_services"]["photo_lifecycle"]

        lifecycle.soft_delete(photo_id, 1, self.user_id, "test-admin")
        lifecycle.soft_delete(keeper_id, 1, self.user_id, "test-admin")
        target = Path(str(self.read_photo(photo_id)["trash_path"])).resolve()
        keeper = Path(str(self.read_photo(keeper_id)["trash_path"])).resolve()

        lifecycle.purge(
            photo_id,
            int(self.read_photo(photo_id)["version"]),
            self.user_id,
            "test-admin",
            f"永久删除 {photo_id}",
        )

        self.assertFalse(target.exists())
        self.assertTrue(keeper.is_file())
        with self.database() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM photo_scores WHERE id=?", (photo_id,)
            ).fetchone()[0]
        self.assertEqual(0, remaining)

    def test_batch_analysis_script_covers_every_directory(self) -> None:
        """验证批量分析脚本的缺失同步条件覆盖全部目录并能识别不可用目录。"""
        import importlib
        import os

        module_name = "src.analysis.analyze_photos_docker"
        raw = IMAGE_DIR_SEPARATOR.join(
            (str(self.image_directory), str(self.secondary_directory))
        )
        previous = os.environ.get("IMAGE_DIR")
        os.environ["IMAGE_DIR"] = raw
        try:
            module = importlib.reload(importlib.import_module(module_name))
            self.assertEqual(
                (self.image_directory, self.secondary_directory), module.IMAGE_DIRS
            )
            clause, params = module._image_dir_prefix_clause()
            self.assertEqual(2, clause.count("LIKE"))
            self.assertEqual(
                [
                    like_prefix(self.image_directory),
                    like_prefix(self.secondary_directory),
                ],
                params,
            )
            self.assertEqual([], module.unavailable_image_dirs())

            os.environ["IMAGE_DIR"] = IMAGE_DIR_SEPARATOR.join(
                (str(self.image_directory), str(self.temporary_path / "not-mounted"))
            )
            module = importlib.reload(module)
            self.assertEqual(
                [self.temporary_path / "not-mounted"], module.unavailable_image_dirs()
            )
        finally:
            if previous is None:
                os.environ.pop("IMAGE_DIR", None)
            else:
                os.environ["IMAGE_DIR"] = previous
            importlib.reload(importlib.import_module(module_name))

    def test_image_directory_status_reports_availability_and_counts(self) -> None:
        """验证目录状态汇总主目录标记、可用性与各目录照片数。"""
        self.create_photo_in(self.image_directory, "one.jpg")
        self.create_photo_in(self.secondary_directory, "two.jpg")
        self.create_photo_in(self.secondary_directory, "three.jpg")
        app = create_app(self.multi_directory_config())
        lifecycle = app.extensions["inktime_services"]["photo_lifecycle"]

        status = lifecycle.image_directory_status()

        self.assertEqual(
            [str(self.image_directory), str(self.secondary_directory)],
            [item["path"] for item in status],
        )
        self.assertEqual([True, False], [item["primary"] for item in status])
        self.assertEqual([1, 2], [item["active_photos"] for item in status])
        self.assertEqual([0, 0], [item["trashed_photos"] for item in status])
        self.assertTrue(all(item["exists"] and item["writable"] for item in status))

    def test_image_directory_status_marks_missing_directory(self) -> None:
        """验证配置中的目录消失后状态表把它标为不存在，而不是让页面报错。"""
        app = create_app(self.multi_directory_config())
        lifecycle = app.extensions["inktime_services"]["photo_lifecycle"]
        self.secondary_directory.rmdir()

        status = lifecycle.image_directory_status()

        self.assertFalse(status[1]["exists"])
        self.assertFalse(status[1]["writable"])
        self.assertEqual(0, status[1]["active_photos"])

    def test_image_directory_status_counts_trashed_photos_per_root(self) -> None:
        """验证回收站计数按目录归属统计。"""
        photo_id = self.create_photo_in(self.secondary_directory, "gone.jpg")
        app = create_app(self.multi_directory_config())
        lifecycle = app.extensions["inktime_services"]["photo_lifecycle"]
        lifecycle.soft_delete(photo_id, 1, self.user_id, "test-admin")

        status = lifecycle.image_directory_status()

        self.assertEqual([0, 0], [item["active_photos"] for item in status])
        self.assertEqual([0, 1], [item["trashed_photos"] for item in status])

    def test_settings_page_shows_directory_status_and_collapsible_groups(self) -> None:
        """验证配置页展示目录状态表，并把每个分组渲染为可折叠区块。"""
        from flask import render_template

        from src.server.blueprints.admin import _settings_context

        self.create_photo_in(self.secondary_directory, "shown.jpg")
        app = create_app(self.multi_directory_config())
        with app.test_request_context("/admin/settings"):
            html = render_template("admin/settings.html", **_settings_context())

        self.assertIn("照片目录状态", html)
        self.assertIn(str(self.secondary_directory), html)
        self.assertIn("主目录", html)
        self.assertIn("附加", html)
        self.assertIn('role="tablist"', html)
        self.assertIn('id="settings-panel-model"', html)
        self.assertIn('name="IMAGE_DIR"', html)

    def test_saving_nested_directories_is_rejected(self) -> None:
        """验证在后台保存嵌套目录被拒绝且不落库。"""
        app = create_app(self.multi_directory_config())
        configuration = app.extensions["inktime_services"]["configuration"]
        nested = self.image_directory / "private"
        nested.mkdir()
        version = configuration.list_admin_settings()["version"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            configuration.update_batch(
                {
                    "IMAGE_DIR": IMAGE_DIR_SEPARATOR.join(
                        (str(self.image_directory), str(nested))
                    )
                },
                version,
                self.actor,
            )

        self.assertIn("嵌套", captured.exception.errors["IMAGE_DIR"])
        self.assertEqual(version, configuration.list_admin_settings()["version"])

    def test_saving_missing_directory_is_rejected(self) -> None:
        """验证保存不存在的目录被拒绝。"""
        app = create_app(self.multi_directory_config())
        configuration = app.extensions["inktime_services"]["configuration"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            configuration.update_batch(
                {"IMAGE_DIR": str(self.temporary_path / "not-mounted")},
                configuration.list_admin_settings()["version"],
                self.actor,
            )

        self.assertIn("不存在", captured.exception.errors["IMAGE_DIR"])

    def test_adding_directory_online_takes_effect_without_restart(self) -> None:
        """验证在线新增照片目录后，同一进程立即接受该目录的照片。"""
        app = create_app(self.application_config())
        configuration = app.extensions["inktime_services"]["configuration"]
        media = app.extensions["inktime_services"]["media"]
        added = self.secondary_directory / "later.jpg"
        _write_jpeg(added)

        with self.assertRaises(PermissionDeniedError):
            media.resolve_photo(str(added))

        configuration.update_batch(
            {
                "IMAGE_DIR": IMAGE_DIR_SEPARATOR.join(
                    (str(self.image_directory), str(self.secondary_directory))
                )
            },
            configuration.list_admin_settings()["version"],
            self.actor,
        )

        self.assertEqual(added.resolve(), media.resolve_photo(str(added)))
        self.assertEqual(
            (self.image_directory, self.secondary_directory),
            app.extensions["inktime_services"]["library_scan"].image_dirs,
        )
