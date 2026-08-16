"""后台照片任务租约恢复与主动让出的回归测试。"""

from src.server.admin_jobs import AdminJobRepository, JobTransitionError
from tests.support import TemporaryDatabaseTestCase


class AdminJobRecoveryTestCase(TemporaryDatabaseTestCase):
    """使用真实迁移和 AdminJobRepository 验证后台任务状态机。"""

    WORKER_ID = "test-worker"

    def _enqueue_and_claim_analyze_job(
        self, repository: AdminJobRepository, photo_id: int, admin_id: int
    ) -> dict:
        """排队并认领一项真实照片分析任务，返回认领后的任务快照。"""
        queued = repository.enqueue(
            photo_id,
            "analyze_photo",
            admin_id,
            {"is_new_upload": False},
        )
        claimed = repository.claim_next(self.WORKER_ID, lease_seconds=30)
        self.assertIsNotNone(claimed)
        self.assertEqual(queued["id"], claimed["id"])
        return claimed

    def test_unexhausted_analyze_lease_recovers_photo_and_job_to_pending(self) -> None:
        """验证照片版本合法推进后，未耗尽租约恢复仍同步回 pending 并清空错误。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("recover-pending.jpg")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        claimed = self._enqueue_and_claim_analyze_job(repository, photo_id, admin_id)

        with self.database() as connection:
            cursor = connection.execute(
                "UPDATE photo_scores SET caption=?,version=version+1 "
                "WHERE id=? AND analysis_status='running'",
                ("legitimate metadata edit", photo_id),
            )
        self.assertEqual(1, cursor.rowcount)
        advanced_version = self.read_photo(photo_id)["version"]
        self.expire_job_lease(claimed["id"])

        self.assertEqual(1, repository.recover_expired_leases())
        job = self.read_job(claimed["id"])
        photo = self.read_photo(photo_id)
        self.assertEqual("pending", job["status"])
        self.assertEqual("pending", photo["analysis_status"])
        self.assertIsNone(job["error_code"])
        self.assertIsNone(job["error_summary"])
        self.assertIsNone(job["finished_at"])
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertEqual(advanced_version + 1, photo["version"])
        self.assertEqual(photo["version"], job["photo_version"])
        recovery_event = self.read_events(job["id"])[-1]
        self.assertEqual("lease_recovered", recovery_event["event_type"])
        self.assertEqual("lease_expired", recovery_event["reason_code"])

    def test_exhausted_analyze_lease_marks_photo_and_job_failed(self) -> None:
        """验证最大尝试次数为一时，过期恢复完整记录最终失败摘要和结束时间。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("recover-failed.jpg")
        repository = AdminJobRepository(self.database_path, max_attempts=1)
        claimed = self._enqueue_and_claim_analyze_job(repository, photo_id, admin_id)
        self.expire_job_lease(claimed["id"])

        self.assertEqual(1, repository.recover_expired_leases())
        job = self.read_job(claimed["id"])
        photo = self.read_photo(photo_id)
        self.assertEqual("failed", job["status"])
        self.assertEqual("failed", photo["analysis_status"])
        self.assertEqual("max_attempts_exceeded", job["error_code"])
        self.assertEqual("max_attempts_exceeded", photo["analysis_error"])
        self.assertEqual("任务租约过期且已达到最大尝试次数", job["error_summary"])
        self.assertIsNotNone(job["finished_at"])
        self.assertEqual(photo["version"], job["photo_version"])
        recovery_event = self.read_events(job["id"])[-1]
        self.assertEqual("lease_recovered", recovery_event["event_type"])
        self.assertEqual("max_attempts_exceeded", recovery_event["reason_code"])

    def test_succeeded_photo_causes_recovery_version_conflict_without_photo_change(self) -> None:
        """验证照片已明确成功时恢复不覆盖照片，并将旧任务终结为版本冲突。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("recover-conflict.jpg")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        claimed = self._enqueue_and_claim_analyze_job(repository, photo_id, admin_id)
        with self.database() as connection:
            cursor = connection.execute(
                "UPDATE photo_scores SET analysis_status='succeeded',analysis_error=NULL,"
                "caption='completed elsewhere',version=version+1 WHERE id=?",
                (photo_id,),
            )
        self.assertEqual(1, cursor.rowcount)
        photo_before_recovery = self.read_photo(photo_id)
        self.expire_job_lease(claimed["id"])

        self.assertEqual(1, repository.recover_expired_leases())
        self.assertEqual(photo_before_recovery, self.read_photo(photo_id))
        job = self.read_job(claimed["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("photo_version_conflict", job["error_code"])
        self.assertEqual("照片分析状态已变化，任务未恢复", job["error_summary"])
        self.assertIsNotNone(job["finished_at"])
        recovery_event = self.read_events(job["id"])[-1]
        self.assertEqual("lease_recovered", recovery_event["event_type"])
        self.assertEqual("photo_version_conflict", recovery_event["reason_code"])

    def test_analyze_job_cannot_be_deferred_and_state_is_unchanged(self) -> None:
        """验证分析任务主动让出被拒绝，任务、照片、尝试次数和租约均保持不变。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("defer-rejected.jpg")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        claimed = self._enqueue_and_claim_analyze_job(repository, photo_id, admin_id)
        job_before = self.read_job(claimed["id"])
        photo_before = self.read_photo(photo_id)
        events_before = self.read_events(claimed["id"])

        with self.assertRaisesRegex(JobTransitionError, "job_cannot_be_deferred"):
            repository.defer(claimed, self.WORKER_ID, "worker_stopping")

        self.assertEqual(job_before, self.read_job(claimed["id"]))
        self.assertEqual(photo_before, self.read_photo(photo_id))
        self.assertEqual(events_before, self.read_events(claimed["id"]))
        self.assertEqual(1, job_before["attempts"])
        self.assertEqual(self.WORKER_ID, job_before["lease_owner"])
        self.assertIsNotNone(job_before["lease_expires_at"])

    def test_backfill_job_defer_returns_attempt_and_preserves_analysis_fields(self) -> None:
        """验证摘要回填可让出、退还尝试次数、清租约并记录 yielded 事件。"""
        admin_id = self.create_admin_user()
        photo_id = self.create_photo("backfill-yield.jpg", analysis_status="legacy")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        analysis_fields_before = {
            key: self.read_photo(photo_id)[key]
            for key in ("analysis_status", "analysis_error", "caption", "memory_score", "beauty_score")
        }
        queued = repository.enqueue(
            photo_id,
            "backfill_content_hash",
            admin_id,
            {"is_new_upload": False},
            priority=0,
        )
        claimed = repository.claim_next(self.WORKER_ID, lease_seconds=30)
        self.assertIsNotNone(claimed)
        self.assertEqual(queued["id"], claimed["id"])

        self.assertTrue(repository.defer(claimed, self.WORKER_ID, "worker_stopping"))
        job = self.read_job(claimed["id"])
        self.assertEqual("pending", job["status"])
        self.assertEqual(0, job["attempts"])
        self.assertEqual(0, job["progress"])
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_expires_at"])
        photo_after = self.read_photo(photo_id)
        self.assertEqual(
            analysis_fields_before,
            {key: photo_after[key] for key in analysis_fields_before},
        )
        yielded_event = self.read_events(job["id"])[-1]
        self.assertEqual("yielded", yielded_event["event_type"])
        self.assertEqual("worker_stopping", yielded_event["reason_code"])
        self.assertEqual("running", yielded_event["old_status"])
        self.assertEqual("pending", yielded_event["new_status"])


    def test_detail_draft_recovery_never_changes_photo(self) -> None:
        """验证详情页草稿租约恢复只改变任务，不推进照片版本或业务字段。"""
        admin_id = self.create_admin_user("draft-recovery-admin")
        photo_id = self.create_photo("draft-recovery.jpg", analysis_status="succeeded", caption="old")
        repository = AdminJobRepository(self.database_path, max_attempts=1)
        queued = repository.enqueue(
            photo_id,
            "analyze_photo",
            admin_id,
            {
                "source": "admin_photo_detail",
                "result_mode": "draft",
                "schema_version": 1,
                "is_new_upload": False,
            },
        )
        photo_before = self.read_photo(photo_id)
        claimed = repository.claim_next(self.WORKER_ID, lease_seconds=30)
        self.assertEqual(queued["id"], claimed["id"])
        self.assertEqual(photo_before, self.read_photo(photo_id))
        self.expire_job_lease(claimed["id"])

        self.assertEqual(1, repository.recover_expired_leases())

        self.assertEqual(photo_before, self.read_photo(photo_id))
        job = self.read_job(claimed["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("max_attempts_exceeded", job["error_code"])
