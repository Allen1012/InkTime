"""照片详情页「重新生成」入口的测试。

起因：一张照片分析连续失败三次后，任务页的「重试」按钮被 `attempts < max_attempts`
条件禁用，界面上再无任何补救入口；而重新分析与重写文案的能力其实早就存在，只是只有
JSON 接口、没有页面入口。
"""

from __future__ import annotations

import re

from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


ADMIN_USERNAME = "regen-admin"
ADMIN_PASSWORD = "inktime-regenerate-password"


class PhotoRegenerateTestCase(TemporaryDatabaseTestCase):
    """校验重新分析与重写文案的页面入口。"""

    def setUp(self) -> None:
        """创建应用、真实管理员并完成登录。"""
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )
        self.client = self.app.test_client()
        response = self.client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": self.csrf_token("/admin/login"),
            },
        )
        self.assertIn(response.status_code, (302, 303))

    def csrf_token(self, path: str) -> str:
        """从指定页面取跨站请求伪造令牌。

        登录会清空并重建会话，登录页上的令牌随即失效，因此登录后的写请求必须重新取。
        """
        body = self.client.get(path).get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token, f"{path} 应包含跨站请求伪造令牌")
        return token.group(1)

    def jobs_for(self, photo_id: int) -> list[dict]:
        """读取指定照片的后台任务。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT job_type,status,max_attempts FROM admin_jobs WHERE photo_id=? "
                "ORDER BY id",
                (photo_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def post(self, path: str, page: str):
        """带当前页面令牌发起写请求。"""
        return self.client.post(path, data={"csrf_token": self.csrf_token(page)})

    def test_failed_photo_can_be_reanalyzed_from_detail_page(self) -> None:
        """验证尝试次数用尽的失败照片仍能通过详情页重新排队分析。"""
        photo_id = self.create_photo("failed.jpg", analysis_status="failed")
        page = f"/admin/photos/{photo_id}"

        response = self.post(f"{page}/reanalyze", page)

        self.assertIn(response.status_code, (302, 303))
        jobs = self.jobs_for(photo_id)
        self.assertEqual(1, len(jobs))
        self.assertEqual("analyze_photo", jobs[0]["job_type"])
        self.assertEqual("pending", jobs[0]["status"])

    def test_narration_can_be_regenerated_from_detail_page(self) -> None:
        """验证对文案不满意时可只重写文案，不重跑整套分析。"""
        photo_id = self.create_photo("ok.jpg", analysis_status="succeeded")
        page = f"/admin/photos/{photo_id}"

        response = self.post(f"{page}/regenerate-narration", page)

        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(
            ["generate_narration"],
            [job["job_type"] for job in self.jobs_for(photo_id)],
        )

    def test_detail_page_exposes_both_entries(self) -> None:
        """验证详情页渲染出两个重新生成入口。"""
        photo_id = self.create_photo("shown.jpg", analysis_status="failed")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn(f"/admin/photos/{photo_id}/reanalyze", body)
        self.assertIn(f"/admin/photos/{photo_id}/regenerate-narration", body)
        self.assertIn("重新分析全部", body)
        self.assertIn("只重写文案", body)

    def test_duplicate_request_does_not_create_second_job(self) -> None:
        """验证重复点击不会重复排队，避免白白消耗模型额度。"""
        photo_id = self.create_photo("dup.jpg", analysis_status="failed")
        page = f"/admin/photos/{photo_id}"

        for _ in range(2):
            self.post(f"{page}/reanalyze", page)

        self.assertEqual(1, len(self.jobs_for(photo_id)))

    def test_jobs_page_links_to_photo_when_attempts_exhausted(self) -> None:
        """验证任务页对尝试次数用尽的失败任务给出照片详情页入口。"""
        photo_id = self.create_photo("exhausted.jpg", analysis_status="failed")
        admin_id = self.create_admin_user("jobs-admin")
        with self.database() as connection:
            connection.execute(
                "INSERT INTO admin_jobs (job_type,status,payload_json,priority,progress,"
                "created_by,photo_id,photo_version,attempts,max_attempts,cancel_requested,"
                "created_at,updated_at,error_code) VALUES "
                "('analyze_photo','failed','{}',100,0,?,?,1,3,3,0,?,?,'RuntimeError')",
                (
                    admin_id,
                    photo_id,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        body = self.client.get("/admin/jobs").get_data(as_text=True)

        self.assertIn("去重新生成", body)
        self.assertIn(f"/admin/photos/{photo_id}", body)
