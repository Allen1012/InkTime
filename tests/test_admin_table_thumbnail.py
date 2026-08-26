"""后台三个表格页面的缩略图渲染与专用端点回归测试。"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from src.server.admin_jobs import AdminJobRepository
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase
from tests.test_admin_pages_render import AdminLoginMixin


class AdminTableThumbnailTestCase(AdminLoginMixin, TemporaryDatabaseTestCase):
    """验证照片管理、已隐藏照片与后台任务三个表格都渲染缩略图。"""

    def _write_photo_file(self, filename: str, size: tuple[int, int] = (60, 40)) -> Path:
        """在临时照片目录写一张真实 JPEG，使缩略图能被真正解码生成。

        `create_photo` 只写数据库行，不落盘；缩略图链路要求文件真实存在，
        否则测的就只是缺失占位分支。

        Args:
            filename: 照片文件名。
            size: 图片像素尺寸。

        Returns:
            已写入的文件路径。
        """
        path = (self.image_directory / filename).resolve()
        Image.new("RGB", size, (120, 140, 160)).save(path, "JPEG")
        return path

    def _hide_photo(self, photo_id: int) -> None:
        """把照片标记为已隐藏，并补齐列表渲染需要的删除快照字段。

        新版隐藏不移动文件，因此 `original_path` 取原路径、`trash_path` 留空，
        与真实软删除后的行形态一致。
        """
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET is_deleted=1, deleted_at=?, original_path=path, "
                "deleted_by_username=? WHERE id=?",
                (TEST_TIMESTAMP, "hide-admin", photo_id),
            )

    def _hide_photo_legacy(self, photo_id: int, filename: str) -> Path:
        """复现早期版本的隐藏：文件真被搬进 `.trash/<id>/`，`trash_path` 非空。

        这类历史记录是线上实际存在的主要形态，而 `resolve_photo()` 刻意拒绝回收站
        路径，因此它必须单独覆盖，否则整页预览取不到图的回归不会被测出来。

        Args:
            photo_id: 照片编号。
            filename: 已经写入照片目录的文件名。

        Returns:
            搬移后的回收站内路径。
        """
        source = (self.image_directory / filename).resolve()
        trash_directory = self.image_directory / ".trash" / str(photo_id)
        trash_directory.mkdir(parents=True, exist_ok=True)
        target = (trash_directory / f"moved-{filename}").resolve()
        source.replace(target)
        with self.database() as connection:
            connection.execute(
                "UPDATE photo_scores SET is_deleted=1, deleted_at=?, original_path=path, "
                "trash_path=?, deleted_by_username=? WHERE id=?",
                (TEST_TIMESTAMP, str(target), "hide-admin", photo_id),
            )
        return target

    def _column_titles(self, body: str) -> list[str]:
        """按渲染顺序抽出表头文本，用于钉住列顺序。

        列顺序是这三张表的显式需求，不能只断言「某列存在」——早期实现里预览列
        在最左边，断言存在性完全测不出来。

        Args:
            body: 页面 HTML。

        Returns:
            表头单元格的文本列表，空表头（复选框列）保留为空串。
        """
        head = body[body.index("<thead>") : body.index("</thead>")]
        return [
            re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)
        ]

    def test_photos_table_renders_thumbnail_cell(self) -> None:
        """照片管理表格的预览列应紧跟照片列，并接上悬停预览。"""
        photo_id = self.create_photo("table-thumb.jpg")
        self._write_photo_file("table-thumb.jpg")
        _, client = self.logged_in_client()

        body = client.get("/admin/photos?view=table").get_data(as_text=True)

        titles = self._column_titles(body)
        self.assertEqual(["照片", "预览"], titles[1:3], f"实际列顺序: {titles}")
        self.assertIn('class="thumb-cell"', body)
        self.assertIn(f"/admin/photos/{photo_id}/thumbnail", body)
        self.assertIn(f'data-preview-src="/admin/photos/{photo_id}/thumbnail"', body)

    def test_preview_script_is_loaded_on_admin_pages(self) -> None:
        """三个表格页面都要加载悬停预览脚本，否则只有静态缩略图。"""
        self.create_photo("script-check.jpg")
        _, client = self.logged_in_client()

        for path in ("/admin/photos", "/admin/trash", "/admin/jobs"):
            with self.subTest(path=path):
                body = client.get(path).get_data(as_text=True)
                self.assertIn("js/admin-thumb-preview.js", body)

    def test_photos_table_shows_placeholder_for_missing_file(self) -> None:
        """原文件缺失时只给占位，不发缩略图请求。"""
        photo_id = self.create_photo("never-written.jpg")
        _, client = self.logged_in_client()

        body = client.get("/admin/photos?view=table").get_data(as_text=True)

        self.assertIn("table-thumb is-missing", body)
        self.assertNotIn(f"/admin/photos/{photo_id}/thumbnail", body)

    def test_trash_table_renders_thumbnail_cell(self) -> None:
        """已隐藏照片页的预览列应紧跟照片编号列，并接上悬停预览。"""
        photo_id = self.create_photo("hidden-thumb.jpg")
        self._write_photo_file("hidden-thumb.jpg")
        self._hide_photo(photo_id)
        _, client = self.logged_in_client()

        body = client.get("/admin/trash").get_data(as_text=True)

        titles = self._column_titles(body)
        self.assertEqual(["照片编号", "预览"], titles[0:2], f"实际列顺序: {titles}")
        self.assertIn(f"/admin/trash/{photo_id}/thumbnail", body)
        self.assertIn(f'data-preview-src="/admin/trash/{photo_id}/thumbnail"', body)

    def test_trash_table_shows_placeholder_when_file_deleted(self) -> None:
        """页面本身建议先删文件再扫描，因此文件已删时必须给占位而不是破图。"""
        photo_id = self.create_photo("hidden-gone.jpg")
        self._hide_photo(photo_id)
        _, client = self.logged_in_client()

        body = client.get("/admin/trash").get_data(as_text=True)

        self.assertIn("table-thumb is-missing", body)
        self.assertNotIn(f"/admin/trash/{photo_id}/thumbnail", body)

    def test_trash_thumbnail_endpoint_serves_hidden_photo(self) -> None:
        """已隐藏照片的缩略图端点应返回图片，并带条件请求校验值。"""
        photo_id = self.create_photo("hidden-served.jpg")
        self._write_photo_file("hidden-served.jpg")
        self._hide_photo(photo_id)
        _, client = self.logged_in_client()

        response = client.get(f"/admin/trash/{photo_id}/thumbnail")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["Content-Type"].startswith("image/"))
        self.assertTrue(response.get_data())
        etag = response.headers.get("ETag")
        self.assertTrue(etag)

        cached = client.get(
            f"/admin/trash/{photo_id}/thumbnail", headers={"If-None-Match": etag}
        )
        self.assertEqual(304, cached.status_code)

    def test_trash_thumbnail_endpoint_rejects_active_photo(self) -> None:
        """活动照片不属于已隐藏集合，专用端点必须拒绝而不是回落到活动照片。"""
        photo_id = self.create_photo("still-active.jpg")
        self._write_photo_file("still-active.jpg")
        _, client = self.logged_in_client()

        response = client.get(f"/admin/trash/{photo_id}/thumbnail")

        self.assertEqual(404, response.status_code)

    def test_trash_thumbnail_endpoint_requires_login(self) -> None:
        """未登录访问已隐藏照片缩略图必须被重定向到登录页，不得泄露图片。"""
        photo_id = self.create_photo("hidden-private.jpg")
        self._write_photo_file("hidden-private.jpg")
        self._hide_photo(photo_id)
        app = self.logged_in_client()[0]

        response = app.test_client().get(f"/admin/trash/{photo_id}/thumbnail")

        self.assertIn(response.status_code, (301, 302, 303, 401, 403))

    def test_trash_thumbnail_endpoint_serves_legacy_trash_file(self) -> None:
        """旧版删除的文件在 `.trash` 里，专用端点必须照样能出图。

        `resolve_photo()` 对回收站路径一律抛「照片不存在」，这条用例正是为了
        钉住已隐藏照片走的是放开回收站的 `resolve_hidden_photo()`。
        """
        photo_id = self.create_photo("legacy-hidden.jpg")
        self._write_photo_file("legacy-hidden.jpg")
        moved = self._hide_photo_legacy(photo_id, "legacy-hidden.jpg")
        self.assertTrue(moved.is_file())
        _, client = self.logged_in_client()

        listing = client.get("/admin/trash").get_data(as_text=True)
        self.assertIn(f"/admin/trash/{photo_id}/thumbnail", listing)

        response = client.get(f"/admin/trash/{photo_id}/thumbnail")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["Content-Type"].startswith("image/"))
        self.assertTrue(response.get_data())

    def test_active_photo_endpoint_still_rejects_trash_paths(self) -> None:
        """放开回收站只限已隐藏照片端点，活动照片链路的边界不能被放松。"""
        app = self.logged_in_client()[0]
        photo_id = self.create_photo("boundary.jpg")
        self._write_photo_file("boundary.jpg")
        moved = self._hide_photo_legacy(photo_id, "boundary.jpg")

        with app.app_context():
            media = app.extensions["inktime_services"]["media"]
            with self.assertRaises(Exception) as raised:
                media.resolve_photo(str(moved))
            self.assertIn("照片不存在", str(raised.exception))
            # 同一条路径在已隐藏照片链路里必须可用，两者的差别只有回收站这一条
            self.assertEqual(moved, media.resolve_hidden_photo(str(moved)))

    def test_hidden_photo_resolution_still_blocks_path_escape(self) -> None:
        """放开回收站不等于放开目录穿越，根目录外的路径必须仍被拒绝。"""
        app = self.logged_in_client()[0]
        outside = Path(self.temporary_directory.name) / "outside.jpg"
        Image.new("RGB", (10, 10), (0, 0, 0)).save(outside, "JPEG")

        with app.app_context():
            media = app.extensions["inktime_services"]["media"]
            with self.assertRaises(Exception) as raised:
                media.resolve_hidden_photo(str(outside))

        self.assertIn("超出允许范围", str(raised.exception))

    def test_jobs_table_renders_photo_thumbnail(self) -> None:
        """任务页列顺序应为 编号、照片、预览，且预览接上悬停大图。"""
        photo_id = self.create_photo("job-thumb.jpg")
        self._write_photo_file("job-thumb.jpg")
        admin_id = self.create_admin_user("job-thumb-admin")
        AdminJobRepository(self.database_path, max_attempts=3).enqueue(
            photo_id, "analyze_photo", admin_id, {}
        )
        _, client = self.logged_in_client()

        body = client.get("/admin/jobs").get_data(as_text=True)

        titles = self._column_titles(body)
        self.assertEqual(["编号", "照片", "预览"], titles[0:3], f"实际列顺序: {titles}")
        self.assertIn(f'data-preview-src="/admin/photos/{photo_id}/thumbnail"', body)
        # 预览与编号分列后，编号链接仍在自己的 .job-photo 单元格里且不含缩略图
        photo_cell_at = body.find('class="job-photo"')
        self.assertNotEqual(-1, photo_cell_at, "照片编号列必须保留")
        photo_cell = body[photo_cell_at : body.find("</td>", photo_cell_at)]
        self.assertIn(f"#{photo_id}", photo_cell)
        self.assertNotIn("thumbnail", photo_cell)

    def test_jobs_polling_script_matches_rendered_column_order(self) -> None:
        """轮询脚本重建行的单元格顺序必须与首屏表头一致。

        任务页每隔几秒就会用 `buildRow()` 重建行，顺序一旦与模板不符，刷新一次
        整行内容就会错位到别的列里，而这种错位只在轮询发生后出现，页面首次打开
        看不出来。这里把两侧顺序放在同一个断言里，改了一边忘了另一边就会失败。
        """
        script = (
            Path("src/server/static/js/admin-jobs.js").read_text(encoding="utf-8")
        )
        build = script[script.index("function buildRow") : script.index("function barClassFor")]
        appended = re.findall(r"tr\.appendChild\((\w+)\)", build)

        self.assertEqual(
            [
                "idCell",
                "photoCell",
                "thumbCell",
                "typeCell",
                "statusCell",
                "progressCell",
                "attemptCell",
                "resultCell",
                "errorCell",
                "actionCell",
            ],
            appended,
        )
        # 轮询新建的行同样要带上悬停预览所需的数据属性
        self.assertIn("data-preview-src", build)
