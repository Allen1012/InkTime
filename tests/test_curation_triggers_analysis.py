"""验证收录状态与照片分析排队之间的联动。

这是唯一会按张自动产生模型调用费用的路径，误触发的代价是真金白银，因此每个分叉都要
有用例钉死：只在真正发生跃迁时排一次队、已有分析结果的照片不重跑、批量逐张判定、
「新照片默认已收录」模式下不绕过按张数放行，以及照片改回未收录时立刻止损。

改造前 `enqueue_included` 与目录扫描都没有任何测试覆盖，而它们决定钱怎么花。
"""

from __future__ import annotations

import re

from src.configuration import ConfigurationActor
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase

ADMIN_USERNAME = "curation-admin"
ADMIN_PASSWORD = "inktime-curation-password"


class CurationTransitionReportTestCase(TemporaryDatabaseTestCase):
    """验证照片管理服务如实报告收录跃迁，且不在自己的事务里排队。"""

    def setUp(self) -> None:
        """准备应用、照片管理服务与满足外键约束的管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"][
                "admin_photo_management"
            ]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _update(self, photo_id: int, **values) -> dict:
        """按库内当前版本提交一次单张更新。"""
        with self.app.app_context():
            return self.service.update_photo(
                photo_id,
                self.read_photo(photo_id)["version"],
                values,
                self.admin_id,
                ADMIN_USERNAME,
            )

    def test_excluded_to_included_reports_activated(self) -> None:
        """未收录改为已收录且分析状态待分析时，报告为 activated。"""
        photo_id = self.create_photo(
            "activate.jpg", analysis_status="pending", is_included=0
        )

        result = self._update(photo_id, curation="included")

        self.assertEqual("activated", result["curation_transition"])

    def test_failed_photo_include_reports_activated(self) -> None:
        """分析失败的照片重新收录应当允许再排一次，与放行口径一致。"""
        photo_id = self.create_photo(
            "activate-failed.jpg", analysis_status="failed", is_included=0
        )

        result = self._update(photo_id, curation="included")

        self.assertEqual("activated", result["curation_transition"])

    def test_resaving_included_photo_reports_no_transition(self) -> None:
        """已收录照片再次提交收录状态不算跃迁。

        详情页每次保存都会提交收录字段，若按提交值而非跃迁判断，一张已收录且已分析
        成功的照片每改一次分类就会重新排一次付费分析。
        """
        photo_id = self.create_photo(
            "resave.jpg", analysis_status="pending", is_included=1
        )

        result = self._update(photo_id, curation="included", category="家人")

        self.assertIsNone(result["curation_transition"])

    def test_succeeded_photo_include_reports_no_transition(self) -> None:
        """已分析成功的照片改为已收录不该重跑分析。"""
        photo_id = self.create_photo(
            "succeeded.jpg", analysis_status="succeeded", is_included=0
        )

        result = self._update(photo_id, curation="included")

        self.assertIsNone(result["curation_transition"])

    def test_legacy_photo_include_reports_no_transition(self) -> None:
        """历史记录照片已有结果，重跑必须走显式的重新分析入口。"""
        photo_id = self.create_photo(
            "legacy.jpg", analysis_status="legacy", is_included=0
        )

        result = self._update(photo_id, curation="included")

        self.assertIsNone(result["curation_transition"])

    def test_included_to_excluded_reports_deactivated(self) -> None:
        """已收录改为未收录时报告 deactivated，供调用方止损。"""
        photo_id = self.create_photo(
            "deactivate.jpg", analysis_status="pending", is_included=1
        )

        result = self._update(photo_id, curation="excluded")

        self.assertEqual("deactivated", result["curation_transition"])

    def test_untouched_curation_reports_no_transition(self) -> None:
        """完全没提交收录字段时不应报告任何跃迁。"""
        photo_id = self.create_photo(
            "untouched.jpg", analysis_status="pending", is_included=0
        )

        result = self._update(photo_id, category="风景")

        self.assertIsNone(result["curation_transition"])

    def test_no_job_is_created_inside_the_write_transaction(self) -> None:
        """服务层只报告跃迁，绝不在自己的写事务里建任务。

        在照片写事务内再开一个 SQLite 写连接会互相锁死，因此排队必须留给调用方在
        提交之后做。这条用例防止以后有人「顺手」把排队塞回事务里。
        """
        photo_id = self.create_photo(
            "no-inline-job.jpg", analysis_status="pending", is_included=0
        )

        self._update(photo_id, curation="included")

        with self.database() as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM admin_jobs").fetchone()[0]
            )
        self.assertEqual(0, count)

    def test_batch_reports_only_photos_that_actually_transitioned(self) -> None:
        """整批共用一个 changes，但跃迁必须逐张判定。

        按整批的 changes 判断会把「本来就已收录」和「已分析成功」的照片也算成新收录，
        一次提交最多 100 张，等于凭空多花近百次模型调用。
        """
        moved = self.create_photo(
            "batch-moved.jpg", analysis_status="pending", is_included=0
        )
        already = self.create_photo(
            "batch-already.jpg", analysis_status="pending", is_included=1
        )
        finished = self.create_photo(
            "batch-finished.jpg", analysis_status="succeeded", is_included=0
        )
        items = [
            {"id": photo_id, "version": self.read_photo(photo_id)["version"]}
            for photo_id in (moved, already, finished)
        ]

        with self.app.app_context():
            result = self.service.batch_update(
                items, {"curation": "included"}, self.admin_id, ADMIN_USERNAME
            )

        self.assertEqual(3, result["success_count"])
        self.assertEqual([moved], result["curation_activated"])
        self.assertEqual([], result["curation_deactivated"])

    def test_batch_exclude_reports_every_photo_that_left(self) -> None:
        """批量改为未收录时，所有真正离开收录范围的照片都要被报告。"""
        first = self.create_photo(
            "batch-out-1.jpg", analysis_status="pending", is_included=1
        )
        second = self.create_photo(
            "batch-out-2.jpg", analysis_status="pending", is_included=1
        )
        never = self.create_photo(
            "batch-out-3.jpg", analysis_status="pending", is_included=0
        )
        items = [
            {"id": photo_id, "version": self.read_photo(photo_id)["version"]}
            for photo_id in (first, second, never)
        ]

        with self.app.app_context():
            result = self.service.batch_update(
                items, {"curation": "excluded"}, self.admin_id, ADMIN_USERNAME
            )

        self.assertEqual([first, second], result["curation_deactivated"])
        self.assertEqual([], result["curation_activated"])


class CurationAnalysisQueueTestCase(TemporaryDatabaseTestCase):
    """验证排队前复核、待放行统计与改为未收录时的止损。"""

    def setUp(self) -> None:
        """准备应用、照片任务服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.jobs = self.app.extensions["inktime_services"]["photo_jobs"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _job_photo_ids(self) -> list[int]:
        """按创建顺序返回当前全部任务关联的照片编号。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT photo_id FROM admin_jobs ORDER BY id"
            ).fetchall()
        return [int(row["photo_id"]) for row in rows]

    def test_enqueue_curated_skips_photos_that_no_longer_qualify(self) -> None:
        """提交与排队之间照片被改回未收录或已完成时，不能再为它花钱。"""
        eligible = self.create_photo(
            "queue-ok.jpg", analysis_status="pending", is_included=1
        )
        reverted = self.create_photo(
            "queue-reverted.jpg", analysis_status="pending", is_included=0
        )
        finished = self.create_photo(
            "queue-finished.jpg", analysis_status="succeeded", is_included=1
        )

        result = self.jobs.enqueue_curated([eligible, reverted, finished], self.admin_id)

        self.assertEqual(1, result["created"])
        self.assertEqual(2, result["skipped"])
        self.assertEqual([eligible], self._job_photo_ids())

    def test_enqueue_curated_never_creates_a_second_active_job(self) -> None:
        """重复排队同一张照片不会产生第二条活跃任务。"""
        photo_id = self.create_photo(
            "queue-idempotent.jpg", analysis_status="pending", is_included=1
        )

        first = self.jobs.enqueue_curated([photo_id], self.admin_id)
        second = self.jobs.enqueue_curated([photo_id], self.admin_id)

        self.assertEqual(1, first["created"])
        self.assertEqual(0, second["created"])
        self.assertEqual([photo_id], self._job_photo_ids())

    def test_releasable_count_matches_what_release_actually_picks(self) -> None:
        """统计口径必须与放行实际挑选的照片一致，否则页面数字对不上行为。"""
        pending = self.create_photo(
            "rel-pending.jpg", analysis_status="pending", is_included=1
        )
        failed = self.create_photo(
            "rel-failed.jpg", analysis_status="failed", is_included=1
        )
        self.create_photo("rel-succeeded.jpg", analysis_status="succeeded", is_included=1)
        self.create_photo("rel-excluded.jpg", analysis_status="pending", is_included=0)
        self.create_photo(
            "rel-deleted.jpg", analysis_status="pending", is_included=1, is_deleted=1
        )

        self.assertEqual(2, self.jobs.releasable_count())
        released = self.jobs.enqueue_included(self.admin_id, 500)

        self.assertEqual(2, released["created"])
        self.assertEqual(sorted([pending, failed]), sorted(self._job_photo_ids()))

    def test_releasable_count_drops_after_release(self) -> None:
        """放行过的照片必须离开待放行集合，否则页面数字永远不下降。"""
        self.create_photo("rel-drop.jpg", analysis_status="pending", is_included=1)

        self.assertEqual(1, self.jobs.releasable_count())
        self.jobs.enqueue_included(self.admin_id, 1)

        self.assertEqual(0, self.jobs.releasable_count())

    def test_excluding_photo_cancels_its_pending_analysis(self) -> None:
        """改为未收录后要立刻撤销待执行任务，否则钱照花、结果照样不展示。"""
        photo_id = self.create_photo(
            "cancel-pending.jpg", analysis_status="pending", is_included=1
        )
        self.jobs.enqueue_curated([photo_id], self.admin_id)
        job_id = self._job_photo_ids() and self._latest_job_id()

        result = self.jobs.cancel_active_analysis(photo_id, self.admin_id)

        self.assertEqual(1, result["canceled"])
        self.assertEqual("canceled", self.read_job(job_id)["status"])
        self.assertEqual("curation_excluded", self.read_job(job_id)["error_code"])

    def test_cancel_survives_the_version_bump_from_excluding(self) -> None:
        """收录变更本身会推进照片版本，撤销必须按当前版本收口而不是任务持有的旧版本。

        直接复用按任务版本收口的 `cancel` 会在这里抛版本冲突，这条用例正是为了钉住
        那个坑：先排队、再改收录（版本 +1）、最后撤销。
        """
        photo_id = self.create_photo(
            "cancel-after-bump.jpg", analysis_status="pending", is_included=1
        )
        self.jobs.enqueue_curated([photo_id], self.admin_id)
        job_id = self._latest_job_id()
        with self.app.app_context():
            self.app.extensions["inktime_services"]["admin_photo_management"].update_photo(
                photo_id,
                self.read_photo(photo_id)["version"],
                {"curation": "excluded"},
                self.admin_id,
                ADMIN_USERNAME,
            )

        result = self.jobs.cancel_active_analysis(photo_id, self.admin_id)

        self.assertEqual(1, result["canceled"])
        self.assertEqual("canceled", self.read_job(job_id)["status"])

    def test_cancel_is_a_no_op_when_nothing_is_queued(self) -> None:
        """没有活跃任务时撤销必须安静返回零，而不是报错。"""
        photo_id = self.create_photo(
            "cancel-none.jpg", analysis_status="succeeded", is_included=1
        )

        result = self.jobs.cancel_active_analysis(photo_id, self.admin_id)

        self.assertEqual({"canceled": 0, "cancel_requested": 0}, result)

    def _latest_job_id(self) -> int:
        """返回最近创建的任务编号。"""
        with self.database() as connection:
            row = connection.execute(
                "SELECT id FROM admin_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        return int(row["id"])


class LibraryScanCurationTestCase(TemporaryDatabaseTestCase):
    """验证目录扫描按配置决定新照片收录状态，且两种取值都不自动排队。"""

    def setUp(self) -> None:
        """准备应用、扫描服务、配置服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            services = self.app.extensions["inktime_services"]
            self.scan_service = services["library_scan"]
            self.configuration = services["configuration"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)
        self.actor = ConfigurationActor(self.admin_id, ADMIN_USERNAME)

    def _set_mode(self, value: str) -> None:
        """把新照片默认收录状态改为指定取值。"""
        self.configuration.update_batch(
            {"NEW_PHOTO_CURATION": value},
            self.configuration.list_admin_settings()["version"],
            self.actor,
        )

    def _add_image(self, name: str) -> None:
        """在扫描根目录下放一个可被识别为照片的文件。"""
        (self.image_directory / name).write_bytes(b"not-a-real-jpeg-but-scannable")

    def test_default_mode_registers_new_photos_as_excluded(self) -> None:
        """默认取值下新照片为未收录，等人工挑选后由收录动作触发分析。"""
        self._add_image("scan-default.jpg")

        result = self.scan_service.scan(self.admin_id)

        self.assertEqual(1, result["registered"])
        self.assertEqual(0, result["is_included"])
        with self.database() as connection:
            row = connection.execute(
                "SELECT is_included FROM photo_scores"
            ).fetchone()
        self.assertEqual(0, int(row["is_included"]))

    def test_included_mode_registers_new_photos_as_included(self) -> None:
        """配成默认已收录时新照片直接进入收录范围。"""
        self._set_mode("included")
        self._add_image("scan-included.jpg")

        result = self.scan_service.scan(self.admin_id)

        self.assertEqual(1, result["registered"])
        self.assertEqual(1, result["is_included"])
        with self.database() as connection:
            row = connection.execute(
                "SELECT is_included FROM photo_scores"
            ).fetchone()
        self.assertEqual(1, int(row["is_included"]))

    def test_scan_never_enqueues_analysis_in_either_mode(self) -> None:
        """扫描在两种取值下都不排队分析，否则「默认已收录」就成了无闸门付费通道。"""
        self._set_mode("included")
        self._add_image("scan-no-job.jpg")

        self.scan_service.scan(self.admin_id)

        with self.database() as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM admin_jobs").fetchone()[0]
            )
        self.assertEqual(0, count)

    def test_unrecognised_mode_falls_back_to_excluded(self) -> None:
        """无法识别的取值按未收录处理：认错成已收录会让整批照片直接进候选池。"""
        self._add_image("scan-fallback.jpg")
        with self.app.app_context():
            self.scan_service.configuration_service = None

            result = self.scan_service.scan(self.admin_id)

        self.assertEqual(0, result["is_included"])


class CurationHttpFlowTestCase(TemporaryDatabaseTestCase):
    """通过真实登录会话验证后台页面上的完整联动。"""

    def logged_in_client(self):
        """创建应用并完成表单登录，返回应用、客户端与登录后有效的令牌。"""
        app = create_app(self.application_config())
        with app.app_context():
            app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )
        client = app.test_client()
        page = client.get("/admin/login")
        login_token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True)
        )
        self.assertIsNotNone(login_token)
        response = client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": login_token.group(1),
            },
        )
        self.assertIn(
            response.status_code, (302, 303), "登录必须成功，否则后续断言无意义"
        )
        listing = client.get("/admin/photos").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', listing)
        self.assertIsNotNone(token, "照片列表页必须带批量表单的 CSRF 令牌")
        return app, client, token.group(1)

    def _set_mode(self, app, value: str) -> None:
        """把新照片默认收录状态改为指定取值。"""
        with app.app_context():
            configuration = app.extensions["inktime_services"]["configuration"]
            configuration.update_batch(
                {"NEW_PHOTO_CURATION": value},
                configuration.list_admin_settings()["version"],
                ConfigurationActor(1, ADMIN_USERNAME),
            )

    def _batch_curate(self, client, token: str, photo_ids, value: str):
        """对给定照片提交一次批量收录状态变更。"""
        return client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": [
                    f"{photo_id}:{self.read_photo(photo_id)['version']}"
                    for photo_id in photo_ids
                ],
                "category_mode": "",
                "analysis_status": "",
                "curation": value,
            },
        )

    def _job_photo_ids(self) -> list[int]:
        """按创建顺序返回当前全部任务关联的照片编号。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT photo_id FROM admin_jobs ORDER BY id"
            ).fetchall()
        return [int(row["photo_id"]) for row in rows]

    def test_batch_include_enqueues_analysis_in_auto_mode(self) -> None:
        """默认未收录模式下，批量改为已收录会直接排队分析。"""
        photo_id = self.create_photo(
            "http-include.jpg", analysis_status="pending", is_included=0
        )
        _, client, token = self.logged_in_client()

        response = self._batch_curate(client, token, [photo_id], "included")

        self.assertIn(response.status_code, (302, 303))
        self.assertEqual([photo_id], self._job_photo_ids())

    def test_batch_include_does_not_bypass_release_gate_in_included_mode(self) -> None:
        """默认已收录模式下，收录动作不排队，付费闸门仍是按张数放行。"""
        photo_id = self.create_photo(
            "http-gate.jpg", analysis_status="pending", is_included=0
        )
        app, client, token = self.logged_in_client()
        self._set_mode(app, "included")

        self._batch_curate(client, token, [photo_id], "included")

        self.assertEqual(1, self.read_photo(photo_id)["is_included"])
        self.assertEqual([], self._job_photo_ids())

    def test_batch_exclude_cancels_queued_analysis(self) -> None:
        """批量改为未收录会撤销刚排上的任务。"""
        photo_id = self.create_photo(
            "http-exclude.jpg", analysis_status="pending", is_included=0
        )
        _, client, token = self.logged_in_client()
        self._batch_curate(client, token, [photo_id], "included")
        self.assertEqual([photo_id], self._job_photo_ids())

        self._batch_curate(client, token, [photo_id], "excluded")

        with self.database() as connection:
            row = connection.execute(
                "SELECT status,error_code FROM admin_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual("canceled", row["status"])
        self.assertEqual("curation_excluded", row["error_code"])

    def test_release_control_shows_remaining_count_and_caps_input(self) -> None:
        """放行控件要显示剩余待放行张数，并把输入上限收到该张数。"""
        for index in range(3):
            self.create_photo(
                f"release-{index}.jpg", analysis_status="pending", is_included=1
            )
        _, client, _ = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertIn("待放行 3", body)
        self.assertIn('max="3"', body)
        self.assertIn('placeholder="1-3"', body)

    def test_release_control_is_hidden_when_nothing_can_be_released(self) -> None:
        """没有可放行照片时不展示放行控件，避免给出一个必然无效的入口。"""
        self.create_photo(
            "release-none.jpg", analysis_status="succeeded", is_included=1
        )
        _, client, _ = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        # 不能断言 name="limit" 整体缺失：筛选栏的「每页」用的也是这个字段名，
        # 这里只针对放行控件自己的标签与提示文案。
        self.assertNotIn("放行分析", body)
        self.assertNotIn("待放行", body)
        self.assertNotIn("本次放行分析的照片张数", body)

    def test_release_control_reappears_for_backlog_in_auto_mode(self) -> None:
        """自动模式下仍要保留放行入口，用于收拾存量与分析失败的照片。"""
        self.create_photo(
            "release-backlog.jpg", analysis_status="failed", is_included=1
        )
        _, client, _ = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertIn("待放行 1", body)

    def test_batch_bar_marks_auto_analyze_mode_for_confirmation(self) -> None:
        """批量表单要下发当前模式，脚本据此决定是否二次确认。"""
        self.create_photo("confirm-flag.jpg", is_included=0)
        app, client, _ = self.logged_in_client()

        auto_body = client.get("/admin/photos").get_data(as_text=True)
        self._set_mode(app, "included")
        gated_body = client.get("/admin/photos").get_data(as_text=True)

        self.assertIn('data-auto-analyze="true"', auto_body)
        self.assertIn("已收录（立即排队分析）", auto_body)
        self.assertIn('data-auto-analyze="false"', gated_body)
        self.assertIn("已收录（可按张数放行分析）", gated_body)


class UploadCurationTestCase(TemporaryDatabaseTestCase):
    """验证后台上传一律登记为已收录，且不受新照片默认收录配置影响。"""

    def setUp(self) -> None:
        """准备应用、照片任务仓储、配置服务与管理员。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            services = self.app.extensions["inktime_services"]
            self.repository = services["photo_jobs"].repository
            self.configuration = services["configuration"]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _upload(self, name: str, digest: str) -> dict:
        """登记一张上传照片并返回结果项。"""
        path = self.image_directory / name
        path.write_bytes(b"uploaded-bytes")
        results = self.repository.create_uploaded_photos_and_jobs(
            [
                {
                    "path": str(path),
                    "original_filename": name,
                    "content_sha256": digest,
                    "original_metadata": {},
                }
            ],
            self.admin_id,
        )
        return results[0]

    def test_uploaded_photo_is_included_and_queued(self) -> None:
        """上传本身就是收录决定：改动前是「分析了但未收录」，钱花了却不展示。"""
        result = self._upload("upload-included.jpg", "a" * 64)

        self.assertEqual("accepted", result["status"])
        photo = self.read_photo(int(result["photo_id"]))
        self.assertEqual(1, photo["is_included"])
        self.assertEqual("pending", photo["analysis_status"])
        self.assertIsNotNone(result["job_id"])

    def test_upload_ignores_new_photo_curation_setting(self) -> None:
        """把新照片默认收录状态配成未收录也不该让上传退化成不分析、不展示。"""
        self.configuration.update_batch(
            {"NEW_PHOTO_CURATION": "excluded"},
            self.configuration.list_admin_settings()["version"],
            ConfigurationActor(self.admin_id, ADMIN_USERNAME),
        )

        result = self._upload("upload-ignores-config.jpg", "b" * 64)

        self.assertEqual(1, self.read_photo(int(result["photo_id"]))["is_included"])

    def test_uploaded_photo_is_not_double_counted_as_releasable(self) -> None:
        """上传已经自带任务，不该再出现在待放行统计里。"""
        self._upload("upload-not-releasable.jpg", "c" * 64)

        self.assertEqual(0, self.repository.releasable_count())
