"""照片管理页默认排序为「添加时间从新到旧」的回归测试。

这一项曾被误认为已经生效：早前的改动只把添加时间选项挪到下拉最前面，默认值仍是
拍摄时间，视觉上很像默认已改。用例同时守住下拉选中态与实际行数顺序，避免再退回。
"""

from __future__ import annotations

from src.server.blueprints.admin import _DEFAULT_PHOTO_SORT
from tests.support import TemporaryDatabaseTestCase
from tests.test_admin_pages_render import AdminLoginMixin


class AdminPhotosDefaultSortTestCase(AdminLoginMixin, TemporaryDatabaseTestCase):
    """验证不带 sort 参数访问照片列表时按添加时间倒序。"""

    def _set_created_at(self, photo_id: int, created_at: str) -> None:
        """直接改写入库时间，夹具默认给所有照片同一个时间戳、区分不出先后。"""
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET created_at=? WHERE id=?", (created_at, photo_id)
            )

    def test_default_sort_is_added_newest(self) -> None:
        """默认排序常量与下拉选中态都必须是 added_newest。"""
        self.assertEqual("added_newest", _DEFAULT_PHOTO_SORT)
        self.create_photo("only.jpg")
        _, client = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertIn('<option value="added_newest" selected>', body)
        self.assertNotIn('<option value="latest" selected>', body)

    def test_file_size_sort_options_are_available(self) -> None:
        """排序下拉应提供文件大小从大到小和从小到大两个方向。"""
        self.create_photo("only.jpg")
        _, client = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertIn('<option value="file_size_desc"', body)
        self.assertIn('>文件大小从大到小</option>', body)
        self.assertIn('<option value="file_size_asc"', body)
        self.assertIn('>文件大小从小到大</option>', body)

    def test_recently_added_precedes_recently_taken(self) -> None:
        """拍摄顺序与添加顺序相反时，默认列表以添加时间为准。"""
        # 早拍晚入库：拍摄时间最旧，但最后一个进库，默认应排在最前。
        late_added = self.create_photo("old-shot-new-import.jpg", date_taken="2019:03:05 08:00:00")
        # 晚拍早入库：拍摄时间最新，但先进库，默认应排在后面。
        early_added = self.create_photo("new-shot-old-import.jpg", date_taken="2026:03:05 08:00:00")
        self._set_created_at(late_added, "2026-09-12 10:00:00")
        self._set_created_at(early_added, "2026-01-02 10:00:00")
        _, client = self.logged_in_client()

        body = client.get("/admin/photos").get_data(as_text=True)

        self.assertLess(
            body.index("old-shot-new-import.jpg"),
            body.index("new-shot-old-import.jpg"),
            "默认排序应把最近添加的照片放在前面",
        )

        # 显式传 latest 时仍按拍摄时间，确认改默认值没有把该选项一起改坏。
        shot_order = client.get("/admin/photos?sort=latest").get_data(as_text=True)
        self.assertLess(
            shot_order.index("new-shot-old-import.jpg"),
            shot_order.index("old-shot-new-import.jpg"),
        )
