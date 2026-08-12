"""阶段二配置热生效（构造时冻结改为方法内取值）的临时数据库测试。"""

from __future__ import annotations

import io
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

from PIL import Image

from src.configuration import ConfigurationActor, ConfigurationService
from src.server.admin_jobs import AdminJobRepository, AnalysisWorker, UploadValidationError
from src.server.app import create_app
from src.server.errors import ResourceNotFoundError
from tests.support import TemporaryDatabaseTestCase


class _RecordingStop:
    """替换工作器停止事件，记录每轮等待秒数并让循环只跑一轮。"""

    def __init__(self) -> None:
        """初始化等待记录与停止标记。"""
        self.waits: list[Any] = []
        self._set = False

    def reset(self) -> None:
        """清空记录并允许循环再跑一轮。"""
        self.waits.clear()
        self._set = False

    def is_set(self) -> bool:
        """返回是否已请求停止。"""
        return self._set

    def set(self) -> None:
        """请求停止。"""
        self._set = True

    def wait(self, timeout: Any = None) -> bool:
        """记录本轮等待秒数并立即结束循环。"""
        self.waits.append(timeout)
        self._set = True
        return True


def _jpeg_bytes(size: int = 64) -> bytes:
    """生成可被真实解码的最小 JPEG 负载。"""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (120, 40, 40)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class _FakeUpload:
    """模拟 Werkzeug FileStorage 的最小上传对象。"""

    def __init__(self, filename: str, payload: bytes) -> None:
        """保存文件名与可重复读取的字节流。"""
        self.filename = filename
        self.stream = io.BytesIO(payload)


class HotReloadTestCase(TemporaryDatabaseTestCase):
    """在同一进程内修改配置并验证无需重启即生效。"""

    def setUp(self) -> None:
        """创建应用、管理员与配置服务引用。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.app = create_app(self.application_config())
        self.services = self.app.extensions["inktime_services"]
        self.configuration = self.services["configuration"]

    def change(self, **values: Any) -> None:
        """以当前版本提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def test_upload_limits_take_effect_without_restart(self) -> None:
        """验证改上传上限后同一上传服务实例立即使用新值。"""
        uploads = self.services["uploads"]
        self.assertEqual(10, uploads.max_files)
        # 手机原图常有四五十兆，默认单文件上限为 64 MiB
        self.assertEqual(64 * 1024 * 1024, uploads.max_bytes)
        self.assertEqual(80_000_000, uploads.max_pixels)

        self.change(UPLOAD_MAX_FILES=2, UPLOAD_MAX_BYTES=4096, UPLOAD_MAX_PIXELS=1024)

        self.assertEqual(2, uploads.max_files)
        self.assertEqual(4096, uploads.max_bytes)
        self.assertEqual(1024, uploads.max_pixels)

    def test_lowered_upload_limits_reject_real_upload(self) -> None:
        """验证实测上传按当前配置拒绝，且错误信息使用当前上限。"""
        uploads = self.services["uploads"]
        payload = _jpeg_bytes()

        accepted = uploads.upload([_FakeUpload("first.jpg", payload)], self.user_id)
        self.assertEqual(1, accepted["counts"]["accepted"])

        self.change(UPLOAD_MAX_FILES=1)
        with self.assertRaises(UploadValidationError) as batch_error:
            uploads.upload(
                [_FakeUpload("a.jpg", payload), _FakeUpload("b.jpg", payload)], self.user_id
            )
        self.assertEqual("每批最多上传 1 张图片", str(batch_error.exception))

        self.change(UPLOAD_MAX_BYTES=128)
        with self.assertRaises(UploadValidationError) as size_error:
            uploads.upload([_FakeUpload("big.jpg", payload)], self.user_id)
        self.assertEqual("单张图片不能超过 128 字节", str(size_error.exception))

        self.change(UPLOAD_MAX_BYTES=64 * 1024 * 1024, UPLOAD_MAX_PIXELS=16)
        with self.assertRaises(UploadValidationError) as pixel_error:
            uploads.upload([_FakeUpload("wide.jpg", payload)], self.user_id)
        self.assertEqual("解码后图片像素不能超过 16", str(pixel_error.exception))

    def test_max_content_length_follows_upload_limits_per_request(self) -> None:
        """验证请求期钩子把派生的请求体上限同步为当前配置。"""
        client = self.app.test_client()
        self.assertEqual(200, client.get("/").status_code)
        self.assertEqual(
            10 * 64 * 1024 * 1024 + 1024 * 1024, self.app.config["MAX_CONTENT_LENGTH"]
        )

        self.change(UPLOAD_MAX_FILES=2, UPLOAD_MAX_BYTES=1024 * 1024)

        self.assertEqual(200, client.get("/").status_code)
        self.assertEqual(2 * 1024 * 1024 + 1024 * 1024, self.app.config["MAX_CONTENT_LENGTH"])

    def test_job_max_attempts_applies_to_new_jobs(self) -> None:
        """验证改任务尝试次数后新建任务使用新值，旧任务不变。"""
        repository = self.services["photo_jobs"].repository
        photo_id = self.create_photo("attempts.jpg")
        first = repository.enqueue(photo_id, "generate_narration", self.user_id, {})
        self.assertEqual(3, int(self.read_job(int(first["id"]))["max_attempts"]))

        self.change(JOB_MAX_ATTEMPTS=1)

        self.assertEqual(1, repository.max_attempts)
        second_photo = self.create_photo("attempts-2.jpg")
        second = repository.enqueue(second_photo, "generate_narration", self.user_id, {})
        self.assertEqual(1, int(self.read_job(int(second["id"]))["max_attempts"]))
        self.assertEqual(3, int(self.read_job(int(first["id"]))["max_attempts"]))

    def test_trash_retention_days_is_read_per_call(self) -> None:
        """验证回收站保留天数改完即被生命周期服务采用。"""
        lifecycle = self.services["photo_lifecycle"]
        self.assertEqual(30, lifecycle.retention_days)

        self.change(TRASH_RETENTION_DAYS=7)

        self.assertEqual(7, lifecycle.retention_days)

    def test_cleanup_preview_follows_retention_days(self) -> None:
        """验证改保留天数后过期清理预览的截止时间与命中结果随之变化。"""
        lifecycle = self.services["photo_lifecycle"]
        photo_id = self.create_photo("deleted.jpg", is_deleted=1)
        deleted_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
            timespec="seconds"
        )
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET deleted_at=?,original_path=? WHERE id=?",
                (deleted_at, str(self.image_directory / "deleted.jpg"), photo_id),
            )

        # 默认保留 30 天，10 天前删除的照片尚未到期。
        self.assertEqual(0, lifecycle.cleanup_preview(limit=10)["total"])

        self.change(TRASH_RETENTION_DAYS=5)

        expired = lifecycle.cleanup_preview(limit=10)
        self.assertEqual(1, expired["total"])
        self.assertEqual([photo_id], [item["id"] for item in expired["items"]])

    def test_poll_interval_is_read_inside_worker_loop(self) -> None:
        """验证工作循环每轮按当前配置等待，而不是启动时固定的间隔。"""
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, original_term)
        self.addCleanup(signal.signal, signal.SIGINT, original_int)

        worker_configuration = ConfigurationService(self.database_path, environment={})
        worker = AnalysisWorker(
            AdminJobRepository(
                self.database_path, 3, configuration_service=worker_configuration
            ),
            lambda *_args, **_kwargs: {},
            lambda *_args, **_kwargs: "",
            configuration_service=worker_configuration,
        )
        recorder = _RecordingStop()
        worker._stop = recorder

        worker.run_forever()
        self.assertEqual([2.0], recorder.waits)

        self.change(JOB_POLL_SECONDS=0.25)
        recorder.reset()
        worker.run_forever()
        self.assertEqual([0.25], recorder.waits)

    def test_browse_switches_react_immediately(self) -> None:
        """验证两个浏览开关在同一服务实例上即时生效。"""
        files = self.services["files"]
        with self.assertRaises(ResourceNotFoundError):
            files.browse("")

        self.change(ENABLE_FILE_BROWSER=True, ENABLE_REVIEW_WEBUI=True)
        self.assertIsInstance(files.browse(""), str)

        self.change(ENABLE_REVIEW_WEBUI=False)
        with self.assertRaises(ResourceNotFoundError):
            files.browse("")

    def test_worker_timing_follows_configuration_across_processes(self) -> None:
        """验证独立工作进程的配置服务能感知 Web 侧改动并收敛非法组合。"""
        worker_configuration = ConfigurationService(self.database_path, environment={})
        worker = AnalysisWorker(
            AdminJobRepository(
                self.database_path, 3, configuration_service=worker_configuration
            ),
            lambda *_args, **_kwargs: {},
            lambda *_args, **_kwargs: "",
            configuration_service=worker_configuration,
        )
        self.assertEqual(120, worker.lease_seconds)
        self.assertEqual(30, worker.renew_seconds)
        self.assertEqual(2.0, worker.poll_seconds)

        # 提交值必须与 Web 进程当前有效值不同，否则会被「无实际变化」过滤掉。
        self.change(JOB_LEASE_SECONDS=40, JOB_RENEW_SECONDS=11, JOB_POLL_SECONDS=0.5)

        self.assertEqual(40, worker.lease_seconds)
        self.assertEqual(11, worker.renew_seconds)
        self.assertEqual(0.5, worker.poll_seconds)

        # 注册表无法表达跨项约束，续租不小于租约时必须兜底收敛，否则心跳永不续租。
        self.change(JOB_RENEW_SECONDS=90)
        self.assertEqual(39, worker.renew_seconds)

    def test_worker_side_change_is_visible_to_web_services(self) -> None:
        """验证工作进程侧写入的配置立即被 Web 服务实例读取到。"""
        worker_configuration = ConfigurationService(self.database_path, environment={})
        worker_configuration.update_batch(
            {"TRASH_RETENTION_DAYS": 3},
            worker_configuration.list_admin_settings()["version"],
            self.actor,
        )

        self.assertEqual(3, self.services["photo_lifecycle"].retention_days)
        self.assertEqual(3, self.configuration.get("TRASH_RETENTION_DAYS"))
