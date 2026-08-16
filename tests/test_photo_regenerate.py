"""照片详情页待确认生成结果及正式任务入口的回归测试。"""

from __future__ import annotations

import json
import math
import re

from src.server.admin_jobs import AdminJobRepository
from src.server.app import create_app
from src.server.errors import ParameterError
from tests.support import TemporaryDatabaseTestCase


ADMIN_USERNAME = "regen-admin"
ADMIN_PASSWORD = "inktime-regenerate-password"
DRAFT_MARKERS = {
    "source": "admin_photo_detail",
    "result_mode": "draft",
    "schema_version": 1,
    "is_new_upload": False,
}


class PhotoRegenerateTestCase(TemporaryDatabaseTestCase):
    """校验详情页草稿任务、确认保存和正式任务语义隔离。"""

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
        """从指定页面读取当前会话的跨站请求伪造令牌。"""
        body = self.client.get(path).get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token, f"{path} 应包含跨站请求伪造令牌")
        return token.group(1)

    def jobs_for(self, photo_id: int) -> list[dict]:
        """读取指定照片的完整后台任务行。"""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_jobs WHERE photo_id=? ORDER BY id", (photo_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def post_detail(self, photo_id: int, action: str, *, json_response: bool = False):
        """带详情页令牌提交生成请求，可选请求 JSON 响应。"""
        page = f"/admin/photos/{photo_id}"
        headers = {"Accept": "application/json"} if json_response else None
        return self.client.post(
            f"{page}/{action}",
            data={"csrf_token": self.csrf_token(page)},
            headers=headers,
        )

    @staticmethod
    def business_state(photo: dict) -> dict:
        """提取草稿任务绝不能直接修改的照片业务字段和版本。"""
        fields = (
            "version", "analysis_status", "analysis_error", "caption", "side_caption",
            "memory_score", "beauty_score", "reason", "type",
        )
        return {field: photo[field] for field in fields}

    def test_detail_enqueue_is_draft_and_does_not_change_photo(self) -> None:
        """页面排队前后照片版本、状态和业务字段保持不变。"""
        photo_id = self.create_photo("failed.jpg", analysis_status="failed", caption="old")
        before = self.business_state(self.read_photo(photo_id))

        response = self.post_detail(photo_id, "reanalyze")

        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(before, self.business_state(self.read_photo(photo_id)))
        job = self.jobs_for(photo_id)[0]
        self.assertEqual("analyze_photo", job["job_type"])
        self.assertEqual("pending", job["status"])
        self.assertEqual(DRAFT_MARKERS, json.loads(job["payload_json"]))
        self.assertEqual(before["version"], job["photo_version"])

    def test_accept_json_returns_safe_latest_draft(self) -> None:
        """详情页生成请求返回二百零二和安全任务视图，而非原始数据库行。"""
        photo_id = self.create_photo("json.jpg", analysis_status="succeeded")

        response = self.post_detail(photo_id, "regenerate-narration", json_response=True)

        self.assertEqual(202, response.status_code)
        payload = response.get_json()
        self.assertEqual("ok", payload["status"])
        self.assertEqual(
            {"id", "status", "job_type", "progress", "error_code", "error_summary", "result"},
            set(payload["data"]),
        )
        self.assertEqual("generate_narration", payload["data"]["job_type"])
        self.assertNotIn("payload_json", payload["data"])

    def test_claim_and_complete_store_whitelisted_draft_without_photo_update(self) -> None:
        """认领和完成完整分析草稿只写任务结果白名单。"""
        photo_id = self.create_photo("complete.jpg", analysis_status="failed", caption="old")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        admin_id = self.create_admin_user("draft-worker-admin")
        repository.enqueue(photo_id, "analyze_photo", admin_id, DRAFT_MARKERS)
        before = self.business_state(self.read_photo(photo_id))

        claimed = repository.claim_next("draft-worker", lease_seconds=30)
        self.assertIsNotNone(claimed)
        self.assertEqual(before, self.business_state(self.read_photo(photo_id)))
        self.assertTrue(repository.complete(
            claimed,
            "draft-worker",
            {
                "type": "旅行/风景",
                "caption": "new caption",
                "side_caption": "new narration",
                "memory_score": 83,
                "beauty_score": 91.5,
                "reason": "new reason",
                "secret": "must-not-leak",
            },
        ))

        self.assertEqual(before, self.business_state(self.read_photo(photo_id)))
        job = self.jobs_for(photo_id)[0]
        stored = json.loads(job["result_json"])
        self.assertEqual(
            {"category", "caption", "side_caption", "memory_score", "beauty_score", "reason", "analysis_status"},
            set(stored["fields"]),
        )
        self.assertNotIn("secret", stored["fields"])
        latest = repository.latest_draft(photo_id)
        self.assertEqual("succeeded", latest["status"])
        self.assertEqual(stored, latest["result"])

    def test_narration_draft_result_contains_only_side_caption(self) -> None:
        """旁白草稿只暴露旁白字段且不修改照片。"""
        photo_id = self.create_photo("narration.jpg", analysis_status="succeeded")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        admin_id = self.create_admin_user("narration-worker-admin")
        repository.enqueue(photo_id, "generate_narration", admin_id, DRAFT_MARKERS)
        before = self.business_state(self.read_photo(photo_id))
        claimed = repository.claim_next("narration-worker", lease_seconds=30)

        self.assertTrue(repository.complete(
            claimed,
            "narration-worker",
            {"side_caption": "new side", "caption": "must-not-leak"},
        ))
        self.assertEqual(before, self.business_state(self.read_photo(photo_id)))
        result = json.loads(self.jobs_for(photo_id)[0]["result_json"])
        self.assertEqual({"side_caption": "new side"}, result["fields"])

    def test_get_draft_returns_latest_current_version_task(self) -> None:
        """查询端点返回当前照片版本下两类草稿中的最新任务。"""
        photo_id = self.create_photo("latest.jpg", analysis_status="succeeded")
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        admin_id = self.create_admin_user("latest-admin")
        analysis = repository.enqueue(photo_id, "analyze_photo", admin_id, DRAFT_MARKERS)
        claimed = repository.claim_next("latest-worker", lease_seconds=30)
        self.assertTrue(repository.complete(claimed, "latest-worker", {"type": "日常"}))
        narration = repository.enqueue(photo_id, "generate_narration", admin_id, DRAFT_MARKERS)
        with self.database() as connection:
            connection.execute(
                "UPDATE admin_jobs SET status='failed',error_code='RuntimeError',"
                "error_summary='生成失败',finished_at=updated_at WHERE id=?",
                (narration["id"],),
            )

        response = self.client.get(f"/api/admin/photos/{photo_id}/draft")

        self.assertEqual(200, response.status_code)
        data = response.get_json()["data"]
        self.assertEqual(narration["id"], data["id"])
        self.assertEqual("failed", data["status"])
        self.assertNotEqual(analysis["id"], data["id"])

    def test_formal_json_api_keeps_formal_semantics(self) -> None:
        """既有正式 JSON 接口仍推进照片版本和分析状态。"""
        photo_id = self.create_photo("formal.jpg", analysis_status="failed")
        page = f"/admin/photos/{photo_id}"

        response = self.client.post(
            f"/api/admin/photos/{photo_id}/reanalyze",
            data={"csrf_token": self.csrf_token(page)},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(202, response.status_code)
        photo = self.read_photo(photo_id)
        self.assertEqual("pending", photo["analysis_status"])
        self.assertEqual(2, photo["version"])
        payload = json.loads(self.jobs_for(photo_id)[0]["payload_json"])
        self.assertNotIn("result_mode", payload)

    def test_active_formal_job_returns_clear_conflict_for_draft(self) -> None:
        """正式同类任务活跃时，详情页草稿请求返回清晰冲突而非服务器错误。"""
        photo_id = self.create_photo("conflict.jpg", analysis_status="failed")
        page = f"/admin/photos/{photo_id}"
        token = self.csrf_token(page)
        self.client.post(
            f"/api/admin/photos/{photo_id}/reanalyze",
            data={"csrf_token": token},
            headers={"Accept": "application/json"},
        )

        response = self.client.post(
            f"{page}/reanalyze",
            data={"csrf_token": self.csrf_token(page)},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual("active_formal_job_exists", response.get_json()["error"]["code"])

    def test_active_draft_blocks_formal_and_other_draft_modes(self) -> None:
        """活跃待确认任务阻止正式任务和另一类待确认任务并发。"""
        photo_id = self.create_photo("draft-conflict.jpg", analysis_status="succeeded")
        page = f"/admin/photos/{photo_id}"
        repository = AdminJobRepository(self.database_path, max_attempts=3)
        admin_id = self.create_admin_user("draft-conflict-admin")
        repository.enqueue(photo_id, "analyze_photo", admin_id, DRAFT_MARKERS)
        before = self.business_state(self.read_photo(photo_id))

        formal_response = self.client.post(
            f"/api/admin/photos/{photo_id}/reanalyze",
            data={"csrf_token": self.csrf_token(page)},
            headers={"Accept": "application/json"},
        )
        narration_response = self.client.post(
            f"{page}/regenerate-narration",
            data={"csrf_token": self.csrf_token(page)},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(409, formal_response.status_code)
        self.assertEqual("active_draft_job_exists", formal_response.get_json()["error"]["code"])
        self.assertEqual(409, narration_response.status_code)
        self.assertEqual("active_draft_job_exists", narration_response.get_json()["error"]["code"])
        self.assertEqual(before, self.business_state(self.read_photo(photo_id)))
        self.assertEqual(1, len(self.jobs_for(photo_id)))

    def test_score_fields_save_and_reject_invalid_numbers(self) -> None:
        """评分可保存空值或有限区间数字，并拒绝布尔值、文本和非有限值。"""
        photo_id = self.create_photo("scores.jpg", analysis_status="succeeded")
        page = f"/admin/photos/{photo_id}"
        response = self.client.post(
            page,
            data={
                "csrf_token": self.csrf_token(page),
                "version": "1",
                "caption": "caption",
                "side_caption": "side",
                "memory_score": "12.5",
                "beauty_score": "87",
                "reason": "reason",
                "exif_city": "city",
                "category": "日常",
                "date_taken": "2024-01-01T12:00:00",
                "analysis_status": "succeeded",
            },
        )
        self.assertIn(response.status_code, (302, 303))
        photo = self.read_photo(photo_id)
        self.assertEqual(12.5, photo["memory_score"])
        self.assertEqual(87.0, photo["beauty_score"])

        service = self.app.extensions["inktime_services"]["admin_photo_management"]
        for invalid in (True, "12", math.nan, math.inf, -1, 101):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ParameterError):
                    service.update_photo(
                        photo_id, photo["version"], {"memory_score": invalid}, 1, "admin"
                    )

    def test_detail_page_contains_draft_controls_and_exact_labels(self) -> None:
        """详情页渲染合法独立表单、评分字段、同排操作栏与草稿脚本。"""
        photo_id = self.create_photo("shown.jpg", analysis_status="failed")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn(">EXIF 元数据</h2>", body)
        self.assertIn('id="photo-edit-form"', body)
        self.assertRegex(body, r'id="photo-save-button"[^>]*disabled')
        self.assertIn('id="analysis-draft-status"', body)
        self.assertEqual(2, body.count("data-analysis-draft-button"))
        self.assertIn('class="photo-action-bar"', body)
        self.assertIn('name="memory_score"', body)
        self.assertIn('name="beauty_score"', body)
        self.assertIn(">重新分析</button>", body)
        self.assertNotIn("重新分析全部", body)
        self.assertIn("js/admin-photo-detail.js", body)
        self.assertIn(f'data-draft-url="/api/admin/photos/{photo_id}/draft"', body)

    def test_duplicate_draft_request_does_not_create_second_job(self) -> None:
        """重复点击详情页生成按钮不会重复排队。"""
        photo_id = self.create_photo("duplicate.jpg", analysis_status="failed")
        for _ in range(2):
            self.post_detail(photo_id, "reanalyze")
        self.assertEqual(1, len(self.jobs_for(photo_id)))
