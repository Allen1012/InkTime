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
