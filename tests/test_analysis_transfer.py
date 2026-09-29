"""跨环境迁移分析结果的导出与导入。

这条路径的价值全在「不再花一次模型额度」，因此最关键的断言不是字段搬对了，而是
**导入后不存在任何分析任务**：一旦漏建成 pending，工作进程会照常把 token 烧掉，
而现象只是「账单又涨了」，极难归因。

测试用两个独立临时数据库真实模拟两套环境：源环境走真实上传落盘、真实导出，目标
环境走真实导入，不构造假包、不打桩仓储。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from src.migrations import migrate_database
from src.server.admin_jobs import (
    TRANSFER_PACKAGE_FORMAT,
    AdminJobRepository,
    UploadService,
    UploadValidationError,
)
from src.server.app import create_app
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase

ADMIN_USERNAME = "transfer-admin"
ADMIN_PASSWORD = "inktime-transfer-password"

# 模拟源环境分析完成后写回的结果，取值刻意与夹具默认值不同，便于断言真的搬过来了
ANALYSIS_RESULT = {
    "caption": "傍晚的江边，两个人并肩走过栈桥",
    "type": "人物/旅行",
    "memory_score": 91.5,
    "beauty_score": 84.0,
    "reason": "有明确的人物互动与地点特征",
    "side_caption": "那天风很大，话很少",
    "exif_city": "厦门",
    "exif_make": "FUJIFILM",
    "exif_model": "X-T5",
    "exif_iso": 640,
    "date_source": "exif",
    "raw_json": '{"debug":"不应被导出"}',
}


def _photo_bytes(width: int = 48, height: int = 32, color: tuple[int, int, int] = (90, 120, 60)) -> bytes:
    """生成一张可被真实解码的小 JPEG。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


class _FakeUpload:
    """模拟 Werkzeug FileStorage 的最小上传对象。"""

    def __init__(self, filename: str, payload: bytes) -> None:
        """保存文件名与可重复读取的字节流。"""
        self.filename = filename
        self.stream = io.BytesIO(payload)


class AnalysisTransferTestCase(TemporaryDatabaseTestCase):
    """基类的临时库充当源环境，另建一个独立库充当目标环境。"""

    def setUp(self) -> None:
        """准备源环境与目标环境两套完整应用。"""
        super().setUp()
        self.source_admin_id = self.create_admin_user(ADMIN_USERNAME)
        self.source_app = create_app(self.application_config())
        self.source_services = self.source_app.extensions["inktime_services"]

        # 目标环境：独立数据库与独立照片目录，避免两套环境互相看见对方的文件
        self.target_database = (self.temporary_path / "target" / "target.db").resolve()
        self.target_images = (self.temporary_path / "target-images").resolve()
        self.target_output = (self.temporary_path / "target-output").resolve()
        self.target_images.mkdir(parents=True)
        self.target_output.mkdir(parents=True)
        migrate_database(self.target_database)
        self.target_admin_id = self._create_target_admin()
        target_config = dict(self.application_config())
        target_config.update(
            {
                "DB_PATH": self.target_database,
                "IMAGE_DIR": self.target_images,
                "BIN_OUTPUT_DIR": self.target_output,
            }
        )
        self.target_app = create_app(target_config)
        self.target_services = self.target_app.extensions["inktime_services"]

    def _create_target_admin(self) -> int:
        """在目标环境建一个管理员，导入审计的外键需要它。"""
        import sqlite3

        connection = sqlite3.connect(self.target_database)
        try:
            cursor = connection.execute(
                "INSERT INTO admin_users (username,password_hash,created_at,updated_at) "
                "VALUES (?,?,?,?)",
                (ADMIN_USERNAME, "not-a-real-password-hash", TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def _target_rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """在目标环境数据库上执行只读查询。"""
        import sqlite3

        connection = sqlite3.connect(self.target_database)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]
        finally:
            connection.close()

    def _source_upload(self, filename: str, payload: bytes) -> dict[str, Any]:
        """在源环境真实上传一张照片，返回落库结果。"""
        with self.source_app.app_context():
            result = self.source_services["uploads"].upload(
                [_FakeUpload(filename, payload)], self.source_admin_id
            )
        self.assertEqual(1, result["counts"]["accepted"], result)
        return result["items"][0]

    def _mark_source_analyzed(self, photo_id: int) -> None:
        """把源环境照片标记为分析成功并写入结果，模拟真实分析完成。"""
        columns = ",".join(f"{column}=?" for column in ANALYSIS_RESULT)
        with self.database() as connection:
            connection.execute(
                f"UPDATE photo_scores SET analysis_status='succeeded',{columns} WHERE id=?",
                (*ANALYSIS_RESULT.values(), photo_id),
            )
            # 上传时建的分析任务在真实流程里会被工作进程消费掉，这里直接闭合
            connection.execute(
                "UPDATE admin_jobs SET status='succeeded',finished_at=? WHERE photo_id=?",
                (TEST_TIMESTAMP, photo_id),
            )

    def _export_bundle(self) -> tuple[bytes, dict[str, Any]]:
        """按源环境当前已分析照片生成真实 ZIP 完整迁移包。"""
        with self.database() as connection:
            photo_ids = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM photo_scores WHERE analysis_status='succeeded' "
                    "AND content_sha256 IS NOT NULL AND is_deleted=0 ORDER BY id"
                ).fetchall()
            ]
        with self.source_app.app_context():
            repository = self.source_services["photo_jobs"].repository
            records, invalid = repository.export_selected_analysis_records(photo_ids)
            self.assertEqual([], invalid)
            archive_path, manifest = self.source_services["uploads"].create_analysis_bundle(
                records,
                {
                    "format": TRANSFER_PACKAGE_FORMAT,
                    "source_environment": "source.example",
                    "exported_at": TEST_TIMESTAMP,
                    "environment_fingerprint": {"analysis_prompt": "same"},
                },
            )
        try:
            return archive_path.read_bytes(), manifest
        finally:
            archive_path.unlink(missing_ok=True)

    def _target_import_bundle(self, payload: bytes) -> dict[str, Any]:
        """在目标环境导入一个完整迁移包。"""
        with self.target_app.app_context():
            return self.target_services["uploads"].import_analysis_bundle(
                _FakeUpload("transfer.zip", payload),
                self.target_admin_id,
                ADMIN_USERNAME,
            )

    @staticmethod
    def _rewrite_bundle(
        payload: bytes,
        *,
        manifest_update: Any | None = None,
        extra_members: dict[str, bytes | zipfile.ZipInfo] | None = None,
        photo_update: Any | None = None,
    ) -> bytes:
        """重写测试归档中的清单或成员，用于构造篡改与攻击输入。"""
        source = io.BytesIO(payload)
        with zipfile.ZipFile(source, "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        manifest = json.loads(members["manifest.json"].decode("utf-8"))
        if manifest_update is not None:
            manifest_update(manifest)
        members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        if photo_update is not None:
            photo_update(members, manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
            for name, content in (extra_members or {}).items():
                if isinstance(content, zipfile.ZipInfo):
                    archive.writestr(content, b"target")
                else:
                    archive.writestr(name, content)
        return output.getvalue()

    def _prepare_source_photo(
        self, filename: str = "river.jpg", color: tuple[int, int, int] = (90, 120, 60)
    ) -> tuple[dict[str, Any], bytes]:
        """在源环境上传并标记为已分析，返回落库项与落盘文件字节。

        颜色可变是必需的：上传按内容摘要去重，同一批测试里造第二张照片必须给不同
        像素内容，否则会被正确地识别为重复而拿不到新记录。
        """
        payload = _photo_bytes(color=color)
        item = self._source_upload(filename, payload)
        self._mark_source_analyzed(int(item["photo_id"]))
        stored_bytes = Path(item["path"]).read_bytes()
        return item, stored_bytes

    def test_complete_bundle_round_trip_includes_photo_and_creates_no_job(self) -> None:
        """完整包应同时搬照片和结果，目标环境不需要再手动选择照片。"""
        source_item, stored_bytes = self._prepare_source_photo()
        payload, manifest = self._export_bundle()

        self.assertEqual(1, manifest["count"])
        self.assertEqual("zip", manifest["container"])
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            self.assertEqual(
                {"manifest.json", manifest["records"][0]["photo_file"]},
                set(archive.namelist()),
            )
            self.assertEqual(
                stored_bytes, archive.read(manifest["records"][0]["photo_file"])
            )

        result = self._target_import_bundle(payload)

        self.assertEqual(1, result["counts"]["imported"], result)
        rows = self._target_rows("SELECT * FROM photo_scores")
        self.assertEqual(1, len(rows))
        imported = rows[0]
        self.assertEqual("succeeded", imported["analysis_status"])
        self.assertIsNone(imported["analysis_error"])
        for column, expected in ANALYSIS_RESULT.items():
            if column != "raw_json":
                self.assertEqual(expected, imported[column], f"{column} 未按导出值写入")
        self.assertIsNone(imported["raw_json"], "模型调试原文不应跨环境搬运")
        self.assertEqual(stored_bytes, Path(imported["path"]).read_bytes())
        self.assertNotEqual(source_item["path"], imported["path"])
        self.assertTrue(Path(imported["path"]).is_relative_to(self.target_images))
        self.assertEqual(
            [], self._target_rows("SELECT id FROM admin_jobs"),
            "完整包导入绝不能创建分析任务",
        )

    def test_complete_bundle_import_is_idempotent(self) -> None:
        """同一个完整包重复导入只保留一张照片。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        first = self._target_import_bundle(payload)
        second = self._target_import_bundle(payload)

        self.assertEqual(1, first["counts"]["imported"])
        self.assertEqual(1, second["counts"]["duplicate"])
        self.assertEqual(1, len(self._target_rows("SELECT id FROM photo_scores")))

    def test_complete_bundle_removes_published_files_when_database_write_fails(self) -> None:
        """数据库失败必须补偿删除已发布照片，不能留下无记录孤儿文件。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()
        repository = self.target_services["uploads"].repository
        original = repository.create_imported_photos

        def fail_database(*_args: Any, **_kwargs: Any) -> Any:
            """模拟照片已经发布后数据库事务失败。"""
            raise RuntimeError("injected database failure")

        repository.create_imported_photos = fail_database
        try:
            with self.assertRaisesRegex(RuntimeError, "injected database failure"):
                self._target_import_bundle(payload)
        finally:
            repository.create_imported_photos = original

        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))
        self.assertEqual([], list(self.target_images.rglob("*.jpg")))
        self.assertEqual([], list(self.target_images.rglob("*.inktime-upload.tmp")))

    def test_complete_bundle_rejects_tampered_photo_without_side_effects(self) -> None:
        """照片字节与清单摘要不一致时整包拒绝，不入库也不留文件。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        def tamper(members: dict[str, bytes], manifest: dict[str, Any]) -> None:
            """替换照片成员，但保留清单里的原摘要。"""
            members[manifest["records"][0]["photo_file"]] = _photo_bytes(
                color=(220, 10, 10)
            )

        changed = self._rewrite_bundle(payload, photo_update=tamper)
        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("摘要与清单不一致", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))
        self.assertEqual([], list(self.target_images.rglob("*.jpg")))
        self.assertEqual([], list(self.target_images.rglob("*.inktime-upload.tmp")))

    def test_complete_bundle_rejects_path_traversal_member(self) -> None:
        """ZIP 路径穿越成员必须在读取清单前就被拒绝。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()
        changed = self._rewrite_bundle(
            payload, extra_members={"../outside.jpg": _photo_bytes()}
        )

        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("不安全的成员路径", str(captured.exception))
        self.assertFalse((self.temporary_path / "outside.jpg").exists())
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_complete_bundle_rejects_symbolic_link_member(self) -> None:
        """符号链接成员不能进入导入流程，即使它没有被清单引用。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()
        link = zipfile.ZipInfo("photos/link.jpg")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        changed = self._rewrite_bundle(payload, extra_members={"ignored": link})

        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("符号链接", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_complete_bundle_rejects_unreferenced_regular_file(self) -> None:
        """归档里多出来的普通文件也要拒绝，不能把清单当作不完整白名单。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()
        changed = self._rewrite_bundle(
            payload, extra_members={"photos/unreferenced.jpg": _photo_bytes()}
        )

        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("清单未引用", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_complete_bundle_rejects_invalid_score(self) -> None:
        """越界评分不能以 succeeded 状态写入目标库。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        def break_score(manifest: dict[str, Any]) -> None:
            """把回忆分改成越界值。"""
            manifest["records"][0]["memory_score"] = 101

        changed = self._rewrite_bundle(payload, manifest_update=break_score)
        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("0 到 100", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_complete_bundle_rejects_incomplete_analysis_record(self) -> None:
        """缺生成文案的记录不能冒充 succeeded 导入目标环境。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        def remove_caption(manifest: dict[str, Any]) -> None:
            """删掉必填 caption 字段，模拟被修改的清单。"""
            del manifest["records"][0]["caption"]

        changed = self._rewrite_bundle(payload, manifest_update=remove_caption)
        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("缺少必填字段", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_complete_bundle_records_audit_entry(self) -> None:
        """每张完整包导入照片都要留审计，标明结果不是本机模型产生的。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        self._target_import_bundle(payload)

        audits = self._target_rows("SELECT * FROM photo_audit_log ORDER BY id")
        self.assertEqual(1, len(audits))
        self.assertEqual("import_analysis", audits[0]["action"])
        self.assertEqual(ADMIN_USERNAME, audits[0]["admin_username"])
        recorded = json.loads(audits[0]["new_values_json"])
        self.assertEqual("source.example", recorded["source"])
        self.assertEqual(64, len(str(recorded["content_sha256"])))

    def test_complete_bundle_rejects_unsupported_package_format(self) -> None:
        """完整包只接受自己认识的清单版本，不猜测未知结构。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        def change_format(manifest: dict[str, Any]) -> None:
            """把包结构版本改成目标环境不认识的未来版本。"""
            manifest["format"] = TRANSFER_PACKAGE_FORMAT + 1

        changed = self._rewrite_bundle(payload, manifest_update=change_format)
        with self.assertRaises(UploadValidationError) as captured:
            self._target_import_bundle(changed)

        self.assertIn("不支持的导出包版本", str(captured.exception))
        self.assertEqual([], self._target_rows("SELECT id FROM photo_scores"))

    def test_selected_export_reports_photos_that_are_not_exportable(self) -> None:
        """所选导出只接受 succeeded、有摘要且未隐藏的照片。"""
        item, _stored = self._prepare_source_photo()
        legacy = self.create_photo("legacy.jpg", analysis_status="legacy")
        pending = self.create_photo("pending.jpg", analysis_status="pending")
        hashless = self.create_photo("hashless.jpg", analysis_status="succeeded")
        deleted = int(
            self._prepare_source_photo("deleted.jpg", color=(210, 40, 160))[0]["photo_id"]
        )
        with self.database() as connection:
            connection.execute("UPDATE photo_scores SET is_deleted=1 WHERE id=?", (deleted,))

        with self.source_app.app_context():
            records, invalid = self.source_services[
                "photo_jobs"
            ].repository.export_selected_analysis_records(
                [int(item["photo_id"]), legacy, pending, hashless, deleted]
            )

        self.assertEqual({legacy, pending, hashless, deleted}, set(invalid))
        self.assertEqual(1, len(records))
        exported_columns = set(records[0])
        for column in ("raw_json", "width", "height", "orientation", "id"):
            self.assertNotIn(column, exported_columns, f"{column} 不该进入清单字段")
        self.assertIn("path", exported_columns, "path 只供服务端读取照片，写包时会剔除")

    def test_complete_bundle_file_attributes_come_from_local_file(self) -> None:
        """尺寸与方向由目标环境解码得出，不采信被篡改的清单值。"""
        self._prepare_source_photo()
        payload, _manifest = self._export_bundle()

        def add_fake_attributes(manifest: dict[str, Any]) -> None:
            """向清单塞入明显错误的文件属性。"""
            manifest["records"][0].update(
                {"width": 9999, "height": 1, "orientation": "square"}
            )

        changed = self._rewrite_bundle(payload, manifest_update=add_fake_attributes)
        self._target_import_bundle(changed)

        imported = self._target_rows("SELECT * FROM photo_scores")[0]
        with Image.open(imported["path"]) as image:
            width, height = image.size
        self.assertEqual(width, imported["width"])
        self.assertEqual(height, imported["height"])
        self.assertEqual("landscape", imported["orientation"])


class TransferPageTestCase(TemporaryDatabaseTestCase):
    """验证照片管理导出与上传页面导入的完整迁移闭环。"""

    def setUp(self) -> None:
        """准备应用。登录走真实表单流程，与其他后台页面测试一致。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )

    def _logged_in_client(self) -> Any:
        """完成带跨站请求伪造令牌的真实表单登录，返回登录后的客户端。"""
        client = self.app.test_client()
        form_page = client.get("/admin/login")
        self.assertEqual(200, form_page.status_code)
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', form_page.get_data(as_text=True)
        )
        self.assertIsNotNone(token, "登录表单必须包含跨站请求伪造令牌")
        response = client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
        )
        self.assertIn(response.status_code, (302, 303), "登录必须成功，否则后续断言无意义")
        return client

    def _create_exportable(
        self,
        filename: str,
        color: tuple[int, int, int],
        *,
        width: int = 48,
    ) -> int:
        """创建一张文件、摘要和必填分析结果都完整的可导出照片。"""
        payload = _photo_bytes(width=width, color=color)
        path = self.image_directory / filename
        path.write_bytes(payload)
        photo_id = self.create_photo(filename, analysis_status="succeeded")
        digest = hashlib.sha256(payload).hexdigest()
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET content_sha256=?,original_filename=?,reason=? WHERE id=?",
                (digest, filename, "测试导出", photo_id),
            )
        return photo_id

    def test_upload_page_contains_bundle_import_and_no_standalone_migration_nav(self) -> None:
        """完整包导入归入上传照片页，侧边栏不再保留重复迁移入口。"""
        response = self._logged_in_client().get("/admin/photos/upload")

        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn('name="bundle"', body)
        self.assertIn("从其他环境导入", body)
        self.assertIn("不会创建分析任务", body)
        self.assertNotIn('href="/admin/photos/transfer"', body)
        self.assertNotIn("分析结果迁移</span>", body)

    def test_complete_bundle_api_accepts_single_zip_file(self) -> None:
        """上传照片页的导入接口只需一个由照片管理页导出的 ZIP 文件。"""
        photo_id = self._create_exportable("api-bundle.jpg", (70, 20, 120))
        client = self._logged_in_client()
        photos_page = client.get("/admin/photos")
        photos_token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"',
            photos_page.get_data(as_text=True),
        )
        self.assertIsNotNone(photos_token)
        export_response = client.post(
            "/admin/photos/export-analysis-selected",
            data={
                "csrf_token": photos_token.group(1),
                "selected": f"{photo_id}:{self.read_photo(photo_id)['version']}",
            },
        )
        try:
            payload = export_response.get_data()
        finally:
            export_response.close()
        upload_page = client.get("/admin/photos/upload")
        upload_token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"',
            upload_page.get_data(as_text=True),
        )
        self.assertIsNotNone(upload_token)

        response = client.post(
            "/api/admin/photos/import-analysis",
            data={
                "csrf_token": upload_token.group(1),
                "bundle": (io.BytesIO(payload), "transfer.zip"),
            },
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )

        self.assertEqual(201, response.status_code)
        body = json.loads(response.get_data(as_text=True))
        # 同库回导按摘要识别为重复，说明 ZIP 已经过完整解析而没有创建第二条记录
        self.assertEqual(1, body["data"]["counts"]["duplicate"])
        self.assertEqual(0, body["data"]["counts"]["imported"])

    def test_photo_management_can_export_only_selected_photos(self) -> None:
        """照片管理页勾选哪些，完整包就只包含哪些，不受全库其他照片影响。"""
        first = self._create_exportable("selected-a.jpg", (20, 70, 110))
        second = self._create_exportable("selected-b.jpg", (120, 30, 90))
        self._create_exportable("not-selected.jpg", (70, 160, 20))
        client = self._logged_in_client()
        page = client.get("/admin/photos")
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(token)

        response = client.post(
            "/admin/photos/export-analysis-selected",
            data={
                "csrf_token": token.group(1),
                "selected": [
                    f"{first}:{self.read_photo(first)['version']}",
                    f"{second}:{self.read_photo(second)['version']}",
                ],
            },
        )
        try:
            self.assertEqual(200, response.status_code)
            with zipfile.ZipFile(io.BytesIO(response.get_data()), "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(2, manifest["count"])
            self.assertEqual("photo_management", manifest["selection"])
            self.assertEqual(
                {"selected-a.jpg", "selected-b.jpg"},
                {record["original_filename"] for record in manifest["records"]},
            )
        finally:
            response.close()

    def test_selected_export_rejects_unanalyzed_photo_without_partial_package(self) -> None:
        """所选照片混入未分析项时整批拒绝，不能悄悄少导出一张。"""
        exportable = self._create_exportable("ready.jpg", (20, 60, 140))
        pending = self.create_photo("pending.jpg", analysis_status="pending")
        client = self._logged_in_client()
        page = client.get("/admin/photos")
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(token)

        response = client.post(
            "/admin/photos/export-analysis-selected",
            data={
                "csrf_token": token.group(1),
                "selected": [
                    f"{exportable}:{self.read_photo(exportable)['version']}",
                    f"{pending}:{self.read_photo(pending)['version']}",
                ],
            },
            headers={"Accept": "application/json"},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn(
            "尚未分析成功或缺少内容摘要", response.get_data(as_text=True)
        )

    def test_import_requires_a_package_file(self) -> None:
        """上传照片页没选完整包时要明确拒绝。"""
        client = self._logged_in_client()
        page = client.get("/admin/photos/upload")
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(token, "完整包导入表单必须带跨站请求伪造令牌")

        response = client.post(
            "/api/admin/photos/import-analysis",
            data={"csrf_token": token.group(1)},
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )

        self.assertEqual(400, response.status_code)
        body = json.loads(response.get_data(as_text=True))
        self.assertEqual("invalid_parameter", body["error"]["code"])
        self.assertIn("完整迁移包", body["error"]["message"])

    def test_import_without_csrf_token_is_rejected(self) -> None:
        """导入是写接口，必须受跨站请求伪造保护。"""
        response = self._logged_in_client().post(
            "/api/admin/photos/import-analysis",
            data={"bundle": (io.BytesIO(b"not-a-zip"), "transfer.zip")},
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("csrf_failed", response.get_data(as_text=True))

    def test_anonymous_access_is_protected_and_old_page_is_removed(self) -> None:
        """上传和所选导出受认证保护，旧独立迁移页面不再存在。"""
        client = self.app.test_client()

        self.assertIn(client.get("/admin/photos/upload").status_code, (302, 303))
        self.assertIn(
            client.post("/admin/photos/export-analysis-selected").status_code,
            (302, 303),
        )
        self.assertEqual(404, client.get("/admin/photos/transfer").status_code)
        self.assertEqual(404, client.get("/admin/photos/export-analysis").status_code)
