"""后台任务失败重试退避的测试。

起因：模型接口瞬时返回 400，三次尝试在 3 秒内烧光，第三次还撞上 429 限流——那次
限流基本是自己打出来的。`claim_next` 原先只看 `status` 与 `attempts`，没有时间门禁，
失败任务会被下一圈循环立刻重新认领。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.server.admin_jobs import AdminJobRepository
from tests.support import TemporaryDatabaseTestCase


def _iso(moment: datetime) -> str:
    """按仓储写入格式生成时间戳字符串。"""
    return moment.isoformat(timespec="seconds")


class RetryBackoffTestCase(TemporaryDatabaseTestCase):
    """校验失败任务在退避时长内不可再认领。"""

    def setUp(self) -> None:
        """准备管理员、照片与仓储。"""
        super().setUp()
        self.user_id = self.create_admin_user()
        self.photo_id = self.create_photo("retry.jpg")

    def repository(self, backoff: int = 30) -> AdminJobRepository:
        """构造带指定退避时长的仓储。"""
        return AdminJobRepository(self.database_path, 3, retry_backoff_seconds=backoff)

    def age_job(self, job_id: int, seconds: int) -> None:
        """把任务的更新时间改到指定秒数之前，模拟时间流逝。"""
        moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        with self.database() as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET updated_at=? WHERE id=?", (_iso(moment), job_id)
            )
        self.assertEqual(1, cursor.rowcount)

    def test_fresh_job_is_claimable_immediately(self) -> None:
        """验证从未失败的任务不受退避影响。"""
        repository = self.repository()
        repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})

        self.assertIsNotNone(repository.claim_next("worker-1"))

    def test_failed_job_is_not_claimable_within_backoff(self) -> None:
        """验证刚失败的任务在退避时长内不会被立刻重新认领。"""
        repository = self.repository(backoff=30)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        claimed = repository.claim_next("worker-1")
        repository.fail_attempt(claimed, "worker-1", "RuntimeError")

        self.assertIsNone(repository.claim_next("worker-1"))
        self.assertEqual("pending", self.read_job(int(job["id"]))["status"])

    def test_failed_job_becomes_claimable_after_backoff(self) -> None:
        """验证超过退避时长后可以重新认领。"""
        repository = self.repository(backoff=30)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        claimed = repository.claim_next("worker-1")
        repository.fail_attempt(claimed, "worker-1", "RuntimeError")

        self.age_job(int(job["id"]), 31)

        self.assertIsNotNone(repository.claim_next("worker-1"))

    def test_backoff_grows_with_attempts(self) -> None:
        """验证退避随尝试次数指数增长，避免对上游持续加压。"""
        repository = self.repository(backoff=30)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        job_id = int(job["id"])

        # 第一次失败：退避 30 秒
        repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")
        self.age_job(job_id, 31)
        # 第二次失败：退避应为 60 秒，因此 31 秒后仍不可认领
        repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")
        self.age_job(job_id, 31)
        self.assertIsNone(repository.claim_next("worker-1"))

        self.age_job(job_id, 61)
        self.assertIsNotNone(repository.claim_next("worker-1"))

    def test_zero_backoff_keeps_immediate_retry(self) -> None:
        """验证退避配置为零时保持原有的立即重试行为。"""
        repository = self.repository(backoff=0)
        repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")

        self.assertIsNotNone(repository.claim_next("worker-1"))

    def test_exhausted_job_stays_failed(self) -> None:
        """验证用尽尝试次数的任务不再被认领，与退避无关。"""
        repository = self.repository(backoff=0)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        for _ in range(3):
            repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")

        self.assertIsNone(repository.claim_next("worker-1"))
        self.assertEqual("failed", self.read_job(int(job["id"]))["status"])

    def test_backoff_reads_configuration_dynamically(self) -> None:
        """验证退避时长按当前生效配置读取，可在线调整。"""

        class _Configuration:
            """最小配置服务替身。"""

            def __init__(self, value: int) -> None:
                self.value = value

            def get(self, key: str) -> int:
                assert key == "JOB_RETRY_BACKOFF_SECONDS"
                return self.value

        configuration = _Configuration(90)
        repository = AdminJobRepository(
            self.database_path, 3, configuration_service=configuration
        )
        self.assertEqual(90, repository.retry_backoff_seconds)

        configuration.value = 5
        self.assertEqual(5, repository.retry_backoff_seconds)

    def test_manual_retry_refreshes_config_snapshot(self) -> None:
        """验证人工重试重新固化配置快照，改完配置点重试就能用上新值。

        实际踩坑：分析模型被配成文生图模型导致任务失败，改回正确模型后点「重试」
        依旧失败——因为快照固化在首次认领时，重试沿用旧快照。而使用者对「重试」的
        预期就是「用现在的状态再来一次」。任务的历史仍由 admin_job_events 保留。
        """
        snapshots = {"MODEL_NAME": "wrong-model"}

        def provider(scope: str, connection: object) -> tuple[int, str]:
            import json

            return 1, json.dumps({"version": 1, "settings": dict(snapshots)})

        repository = AdminJobRepository(
            self.database_path, 3, snapshot_provider=provider, retry_backoff_seconds=0
        )
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        job_id = int(job["id"])
        claimed = repository.claim_next("worker-1")
        self.assertIn("wrong-model", self.read_job(job_id)["config_snapshot_json"])
        repository.fail_attempt(claimed, "worker-1", "RuntimeError")
        # 尝试次数未用尽却已判定失败，是真实存在的状态（例如 photo_version_conflict）；
        # 尝试次数用尽的任务按设计不可重试，只能从照片详情页新建任务。
        with self.database() as connection:
            connection.execute(
                "UPDATE admin_jobs SET status='failed' WHERE id=?", (job_id,)
            )

        snapshots["MODEL_NAME"] = "correct-model"
        repository.retry(job_id, self.user_id)

        self.assertIsNone(self.read_job(job_id)["started_at"])
        repository.claim_next("worker-1")
        self.assertIn("correct-model", self.read_job(job_id)["config_snapshot_json"])

    def test_automatic_retry_keeps_original_snapshot(self) -> None:
        """验证自动重试仍沿用原快照，避免一张照片分析途中换掉模型。"""
        snapshots = {"MODEL_NAME": "first-model"}

        def provider(scope: str, connection: object) -> tuple[int, str]:
            import json

            return 1, json.dumps({"version": 1, "settings": dict(snapshots)})

        repository = AdminJobRepository(
            self.database_path, 3, snapshot_provider=provider, retry_backoff_seconds=0
        )
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        job_id = int(job["id"])
        repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")

        snapshots["MODEL_NAME"] = "second-model"
        repository.claim_next("worker-1")

        self.assertIn("first-model", self.read_job(job_id)["config_snapshot_json"])

    def test_manual_retry_bypasses_backoff(self) -> None:
        """验证人工重试立即可认领，不受退避约束。

        退避的目的是防止自动重试把上游打爆；管理员主动点重试是明确意图，让人干等
        三十秒没有道理。实现上靠 retry() 清空 error_code 与自动重排队区分开。
        """
        repository = self.repository(backoff=30)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        job_id = int(job["id"])
        for _ in range(2):
            repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")
            self.age_job(job_id, 3600)
        repository.fail_attempt(repository.claim_next("worker-1"), "worker-1", "E")
        self.assertEqual("failed", self.read_job(job_id)["status"])

        # 用尽次数的任务无法重试，先放开一次尝试次数模拟未用尽的情形
        with self.database() as connection:
            connection.execute(
                "UPDATE admin_jobs SET attempts=1 WHERE id=?", (job_id,)
            )
        repository.retry(job_id, self.user_id)

        self.assertIsNone(self.read_job(job_id)["error_code"])
        self.assertIsNotNone(repository.claim_next("worker-1"))

    def test_failure_detail_is_stored_for_display(self) -> None:
        """验证失败详情写入 error_summary，后台页面可直接看到真实原因。"""
        repository = self.repository(backoff=0)
        job = repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        claimed = repository.claim_next("worker-1")

        repository.fail_attempt(
            claimed,
            "worker-1",
            "RuntimeError",
            detail="模型接口请求失败: HTTP 400 invalid_parameter_error\nmessages 无效",
        )

        stored = self.read_job(int(job["id"]))
        self.assertEqual("RuntimeError", stored["error_code"])
        self.assertIn("HTTP 400", stored["error_summary"])
        # 详情压成单行，避免破坏表格排版
        self.assertNotIn("\n", stored["error_summary"])

    def test_detail_is_truncated(self) -> None:
        """验证过长详情被截断，不把上游长响应整段写进数据库。"""
        repository = self.repository(backoff=0)
        repository.enqueue(self.photo_id, "generate_narration", self.user_id, {})
        claimed = repository.claim_next("worker-1")

        repository.fail_attempt(
            claimed, "worker-1", "RuntimeError", detail="x" * 900
        )

        summary = self.read_job(int(claimed["id"]))["error_summary"]
        self.assertEqual(300, len(summary))
