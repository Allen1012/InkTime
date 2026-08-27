"""照片批量更新的多字段契约、乐观锁与审计回归测试。

改造前这条路径完全没有测试覆盖，而它一次能改动最多 100 张照片的可见性与分析
状态，属于误改代价最高的入口之一。
"""

from __future__ import annotations

import json
import re

from src.server.app import create_app
from src.server.errors import ParameterError
from tests.support import TemporaryDatabaseTestCase

ADMIN_USERNAME = "batch-admin"
ADMIN_PASSWORD = "inktime-batch-password"


class PhotoBatchUpdateServiceTestCase(TemporaryDatabaseTestCase):
    """直接验证服务层的「键存在即要改」契约。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"][
                "admin_photo_management"
            ]
        self.admin_id = self.create_admin_user(ADMIN_USERNAME)

    def _items(self, *photo_ids: int) -> list[dict[str, int]]:
        """按当前库内版本号组装批量项。"""
        return [
            {"id": photo_id, "version": self.read_photo(photo_id)["version"]}
            for photo_id in photo_ids
        ]

    def test_multiple_fields_apply_in_one_submission(self) -> None:
        """一次提交可同时改分类、分析状态与收录状态，且版本只递增一次。"""
        photo_id = self.create_photo("multi.jpg", is_included=1)
        before = self.read_photo(photo_id)

        with self.app.app_context():
            result = self.service.batch_update(
                self._items(photo_id),
                {
                    "category": "家人/旅行",
                    "analysis_status": "pending",
                    "curation": "excluded",
                },
                self.admin_id,
                ADMIN_USERNAME,
            )

        self.assertEqual(1, result["success_count"])
        after = self.read_photo(photo_id)
        self.assertEqual("家人/旅行", after["type"])
        self.assertEqual("pending", after["analysis_status"])
        self.assertEqual(0, after["is_included"])
        self.assertEqual(
            before["version"] + 1,
            after["version"],
            "多字段合并成一次乐观更新，版本只应递增一次",
        )

    def test_omitted_fields_are_left_untouched(self) -> None:
        """未出现在 changes 里的字段必须保持原值。"""
        photo_id = self.create_photo("partial.jpg", is_included=1)
        original = self.read_photo(photo_id)

        with self.app.app_context():
            self.service.batch_update(
                self._items(photo_id),
                {"curation": "excluded"},
                self.admin_id,
                ADMIN_USERNAME,
            )

        after = self.read_photo(photo_id)
        self.assertEqual(0, after["is_included"])
        self.assertEqual(original["type"], after["type"])
        self.assertEqual(original["analysis_status"], after["analysis_status"])

    def test_empty_category_clears_without_touching_other_fields(self) -> None:
        """分类传空字符串表示清空，这是「不修改」靠省略键表达才成立的语义。"""
        photo_id = self.create_photo("clear.jpg")
        with self.app.app_context():
            self.service.batch_update(
                self._items(photo_id),
                {"category": "家人"},
                self.admin_id,
                ADMIN_USERNAME,
            )
            self.assertEqual("家人", self.read_photo(photo_id)["type"])
            self.service.batch_update(
                self._items(photo_id),
                {"category": ""},
                self.admin_id,
                ADMIN_USERNAME,
            )

        self.assertEqual("", self.read_photo(photo_id)["type"])

    def test_empty_changes_is_rejected(self) -> None:
        """一个字段都没给时必须拒绝，避免产生一次什么都不改的版本递增。"""
        photo_id = self.create_photo("noop.jpg")
        with self.app.app_context():
            for changes in ({}, None, "category"):
                with self.subTest(changes=changes):
                    with self.assertRaises(ParameterError):
                        self.service.batch_update(
                            self._items(photo_id),
                            changes,
                            self.admin_id,
                            ADMIN_USERNAME,
                        )

    def test_unknown_field_is_rejected(self) -> None:
        """只允许三个字段，其余一律拒绝，防止批量入口变成任意字段写入。"""
        photo_id = self.create_photo("unknown.jpg")
        with self.app.app_context():
            with self.assertRaisesRegex(ParameterError, "只允许修改"):
                self.service.batch_update(
                    self._items(photo_id),
                    {"memory_score": 100},
                    self.admin_id,
                    ADMIN_USERNAME,
                )

    def test_invalid_value_changes_nothing_at_all(self) -> None:
        """任一字段取值不合法时，整批照片都不能被改动。

        归一化刻意放在事务之前：否则前几张会按合法字段改掉，直到遇到非法值才失败，
        留下一个改了一半的批次。
        """
        first = self.create_photo("valid-first.jpg", is_included=1)
        second = self.create_photo("valid-second.jpg", is_included=1)
        before = {pid: self.read_photo(pid) for pid in (first, second)}

        with self.app.app_context():
            with self.assertRaises(ParameterError):
                self.service.batch_update(
                    self._items(first, second),
                    {"curation": "excluded", "analysis_status": "不存在的状态"},
                    self.admin_id,
                    ADMIN_USERNAME,
                )

        for pid, original in before.items():
            after = self.read_photo(pid)
            self.assertEqual(original["is_included"], after["is_included"])
            self.assertEqual(original["version"], after["version"])

    def test_version_conflict_is_reported_per_item(self) -> None:
        """版本过期的项单独失败，同批次其他项照常成功。"""
        fresh = self.create_photo("fresh.jpg")
        stale = self.create_photo("stale.jpg")
        items = self._items(fresh)
        items.append({"id": stale, "version": self.read_photo(stale)["version"] + 5})

        with self.app.app_context():
            result = self.service.batch_update(
                items, {"category": "风景"}, self.admin_id, ADMIN_USERNAME
            )

        self.assertEqual(1, result["success_count"])
        self.assertEqual(1, result["failure_count"])
        self.assertEqual("conflict", result["failed"][0]["code"])
        self.assertEqual("风景", self.read_photo(fresh)["type"])

    def test_audit_records_one_row_covering_all_changed_fields(self) -> None:
        """一次批量操作在审计里是一条记录，包含全部变更字段。"""
        photo_id = self.create_photo("audited.jpg", is_included=1)

        with self.app.app_context():
            result = self.service.batch_update(
                self._items(photo_id),
                {"category": "宠物", "curation": "excluded"},
                self.admin_id,
                ADMIN_USERNAME,
            )

        with self.database() as connection:
            rows = connection.execute(
                "SELECT action, old_values_json, new_values_json, batch_id "
                "FROM photo_audit_log WHERE photo_id=? ORDER BY id",
                (photo_id,),
            ).fetchall()

        self.assertEqual(1, len(rows), "一次操作不应拆成多条审计记录")
        self.assertEqual("batch_update", rows[0]["action"])
        self.assertEqual(result["batch_id"], rows[0]["batch_id"])
        before = json.loads(rows[0]["old_values_json"])
        after = json.loads(rows[0]["new_values_json"])
        self.assertEqual({"type", "is_included"}, set(before))
        self.assertEqual({"type", "is_included"}, set(after))
        self.assertEqual("宠物", after["type"])
        self.assertEqual(0, after["is_included"])


class PhotoBatchFormTestCase(TemporaryDatabaseTestCase):
    """通过真实登录会话验证批量操作栏的表单契约。"""

    def logged_in_client(self):
        """创建应用与管理员并完成表单登录，返回登录后可用的 CSRF 令牌。

        令牌必须在登录完成后重新取：登录会重建会话，登录页上那个令牌随即失效，
        用它提交会一律得到 400。
        """
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
        self.assertIn(response.status_code, (302, 303), "登录必须成功，否则后续断言无意义")
        listing = client.get("/admin/photos").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', listing)
        self.assertIsNotNone(token, "照片列表页必须带批量表单的 CSRF 令牌")
        return app, client, token.group(1)

    def test_bar_has_no_action_selector_and_defaults_to_no_change(self) -> None:
        """操作栏不再有「先选操作」的下拉，三个字段各自默认不修改。"""
        self.create_photo("bar.jpg")
        _, client, _ = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertNotIn('name="action"', body)
        self.assertNotIn("set_analysis_status", body)
        for name in ('name="category_mode"', 'name="analysis_status"', 'name="curation"'):
            self.assertIn(name, body)
        self.assertEqual(
            3,
            body.count('<option value="">不修改</option>'),
            "分类模式、分析状态、收录状态都要有「不修改」默认项",
        )

    def test_form_applies_several_fields_in_one_post(self) -> None:
        """一次表单提交可同时改分类与收录状态。"""
        photo_id = self.create_photo("form-multi.jpg", is_included=1)
        _, client, token = self.logged_in_client()
        version = self.read_photo(photo_id)["version"]

        response = client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": f"{photo_id}:{version}",
                "category_mode": "set",
                "category": "旅行",
                "curation": "excluded",
                "analysis_status": "",
            },
        )

        self.assertIn(response.status_code, (302, 303))
        after = self.read_photo(photo_id)
        self.assertEqual("旅行", after["type"])
        self.assertEqual(0, after["is_included"])

    def test_clear_mode_empties_category(self) -> None:
        """分类模式选清空时提交空值，与「不修改」区分开。"""
        photo_id = self.create_photo("form-clear.jpg", caption="x")
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET type='待清空' WHERE id=?", (photo_id,)
            )
        _, client, token = self.logged_in_client()
        version = self.read_photo(photo_id)["version"]

        client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": f"{photo_id}:{version}",
                "category_mode": "clear",
            },
        )

        self.assertEqual("", self.read_photo(photo_id)["type"])

    def test_submitting_nothing_leaves_photo_untouched(self) -> None:
        """三个字段都留在「不修改」时不应产生任何改动，也不该报错页。"""
        photo_id = self.create_photo("form-noop.jpg")
        original = self.read_photo(photo_id)
        _, client, token = self.logged_in_client()

        response = client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": f"{photo_id}:{original['version']}",
                "category_mode": "",
                "analysis_status": "",
                "curation": "",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("没有选择要修改的内容", response.get_data(as_text=True))
        after = self.read_photo(photo_id)
        self.assertEqual(original["version"], after["version"])
        self.assertEqual(original["type"], after["type"])

    def test_overwrite_mode_without_text_is_rejected(self) -> None:
        """选了「覆盖为」却没填内容时必须拒绝，不能悄悄当成清空。"""
        photo_id = self.create_photo("form-empty.jpg")
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET type='保持不变' WHERE id=?", (photo_id,)
            )
        _, client, token = self.logged_in_client()
        version = self.read_photo(photo_id)["version"]

        client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": f"{photo_id}:{version}",
                "category_mode": "set",
                "category": "   ",
            },
        )

        self.assertEqual("保持不变", self.read_photo(photo_id)["type"])

    def test_soft_delete_button_still_works_independently(self) -> None:
        """隐藏照片是动作而非字段赋值，改造后必须仍能独立触发。"""
        photo_id = self.create_photo("form-hide.jpg")
        _, client, token = self.logged_in_client()
        version = self.read_photo(photo_id)["version"]

        client.post(
            "/admin/photos/batch",
            data={
                "csrf_token": token,
                "selected": f"{photo_id}:{version}",
                "batch_soft_delete": "1",
            },
        )

        self.assertEqual(1, self.read_photo(photo_id)["is_deleted"])
