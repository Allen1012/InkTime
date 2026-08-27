"""后台照片详情页的相邻导航、全屏入口与收录状态编辑回归测试。"""

from __future__ import annotations

import re
from pathlib import Path

from src.server.app import create_app
from src.server.errors import ParameterError
from tests.support import TemporaryDatabaseTestCase

ADMIN_USERNAME = "detail-admin"
ADMIN_PASSWORD = "inktime-detail-password"


class PhotoAdjacencyServiceTestCase(TemporaryDatabaseTestCase):
    """验证相邻照片查询与列表页的筛选、排序口径完全一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(self.application_config())
        with self.app.app_context():
            self.service = self.app.extensions["inktime_services"]["admin_photo"]

    def _listing_ids(self, **filters) -> list[int]:
        """按同一组筛选取出列表页顺序下的照片编号。"""
        defaults = {
            "page": 1,
            "limit": 100,
            "query": "",
            "category": "",
            "analysis_status": "",
            "date_from": "",
            "date_to": "",
            "sort": "latest",
            "view": "table",
        }
        defaults.update(filters)
        with self.app.app_context():
            result = self.service.list_photos(**defaults)
        return [item["id"] for item in result["items"]]

    def test_adjacency_matches_listing_order(self) -> None:
        """逐个照片的前后邻居必须与列表顺序严格对应。

        这是本功能唯一真正重要的不变量：详情页「下一张」的含义就是「列表里的下一
        张」。手写排序比较很容易与后台那套含 NULL 优先级的多列排序产生偏差，所以
        这里拿列表实际返回的顺序当基准逐项核对。
        """
        for index in range(5):
            self.create_photo(f"order-{index}.jpg", date_taken=f"2024:0{index + 1}:01 10:00:00")
        ordered = self._listing_ids()
        self.assertEqual(5, len(ordered))

        with self.app.app_context():
            for position, photo_id in enumerate(ordered):
                with self.subTest(position=position):
                    adjacent = self.service.adjacent_photos(photo_id, sort="latest")
                    expected_previous = ordered[position - 1] if position > 0 else None
                    expected_next = (
                        ordered[position + 1] if position + 1 < len(ordered) else None
                    )
                    self.assertEqual(expected_previous, adjacent["previous_id"])
                    self.assertEqual(expected_next, adjacent["next_id"])

    def test_adjacency_respects_sort(self) -> None:
        """换排序后邻居关系必须跟着变，不能固定按编号。"""
        for index in range(3):
            self.create_photo(f"sorted-{index}.jpg", date_taken=f"2024:0{index + 1}:01 10:00:00")
        latest = self._listing_ids(sort="latest")
        oldest = self._listing_ids(sort="oldest")
        self.assertEqual(latest, list(reversed(oldest)), "两种排序应互为逆序")

        with self.app.app_context():
            middle = latest[1]
            by_latest = self.service.adjacent_photos(middle, sort="latest")
            by_oldest = self.service.adjacent_photos(middle, sort="oldest")

        self.assertEqual(by_latest["previous_id"], by_oldest["next_id"])
        self.assertEqual(by_latest["next_id"], by_oldest["previous_id"])

    def test_adjacency_stays_inside_current_filter(self) -> None:
        """带筛选时邻居只能在筛选结果内，不能跳到被筛掉的照片上。"""
        included = [self.create_photo(f"inc-{i}.jpg", is_included=1) for i in range(2)]
        self.create_photo("exc.jpg", is_included=0)
        filtered = self._listing_ids(curation="included")
        self.assertEqual(sorted(included), sorted(filtered))

        with self.app.app_context():
            for photo_id in filtered:
                adjacent = self.service.adjacent_photos(photo_id, curation="included")
                for neighbour in (adjacent["previous_id"], adjacent["next_id"]):
                    if neighbour is not None:
                        self.assertIn(neighbour, filtered)

    def test_single_result_has_no_neighbours(self) -> None:
        """只有一张照片时两端都为 None，模板据此把按钮置灰。"""
        photo_id = self.create_photo("only.jpg")
        with self.app.app_context():
            adjacent = self.service.adjacent_photos(photo_id)

        self.assertIsNone(adjacent["previous_id"])
        self.assertIsNone(adjacent["next_id"])

    def test_photo_outside_filter_has_no_neighbours(self) -> None:
        """当前照片被筛选排除时不给邻居，避免翻到一个与筛选无关的序列里。"""
        self.create_photo("visible.jpg", is_included=1)
        hidden_by_filter = self.create_photo("filtered-out.jpg", is_included=0)

        with self.app.app_context():
            adjacent = self.service.adjacent_photos(
                hidden_by_filter, curation="included"
            )

        self.assertIsNone(adjacent["previous_id"])
        self.assertIsNone(adjacent["next_id"])

    def test_invalid_sort_is_rejected(self) -> None:
        """排序键走与列表相同的白名单，不能因为来自 return_query 就放行。"""
        photo_id = self.create_photo("bad-sort.jpg")
        with self.app.app_context():
            with self.assertRaises(ParameterError):
                self.service.adjacent_photos(photo_id, sort="'; DROP TABLE x;--")


class PhotoDetailPageTestCase(TemporaryDatabaseTestCase):
    """通过真实登录会话验证详情页的导航、全屏入口与收录编辑。"""

    def logged_in_client(self):
        """创建应用与管理员并完成登录，返回登录后可用的 CSRF 令牌。"""
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
        self.assertIn(response.status_code, (302, 303))
        return app, client

    def _detail(self, client, photo_id: int, query: str = "") -> str:
        """打开详情页并返回 HTML。"""
        path = f"/admin/photos/{photo_id}"
        if query:
            path += f"?return_query={query}"
        response = client.get(path)
        self.assertEqual(200, response.status_code)
        return response.get_data(as_text=True)

    def test_detail_renders_navigation_links(self) -> None:
        """中间那张照片的预览框内应同时有左右箭头。"""
        ids = [
            self.create_photo(f"nav-{i}.jpg", date_taken=f"2024:0{i + 1}:01 10:00:00")
            for i in range(3)
        ]
        for name in ("nav-0.jpg", "nav-1.jpg", "nav-2.jpg"):
            (self.image_directory / name).write_bytes(b"stub")
        _, client = self.logged_in_client()

        body = self._detail(client, ids[1])

        self.assertIn("detail-viewer", body)
        self.assertIn("data-nav-prev", body)
        self.assertIn("data-nav-next", body)
        self.assertIn('rel="prev"', body)
        self.assertIn('rel="next"', body)
        # 箭头在预览框内，不再是顶部标题旁的文字按钮
        self.assertNotIn("detail-nav", body)

    def test_navigation_disabled_at_both_ends(self) -> None:
        """只有一张照片时两个箭头都置灰，且保留占位不移除。"""
        photo_id = self.create_photo("nav-single.jpg")
        (self.image_directory / "nav-single.jpg").write_bytes(b"stub")
        _, client = self.logged_in_client()

        body = self._detail(client, photo_id)

        self.assertEqual(2, body.count('aria-disabled="true"'))
        self.assertEqual(2, body.count("detail-hint"), "两个箭头都要渲染出来")
        self.assertIn("is-disabled", body)

    def test_navigation_links_carry_return_query(self) -> None:
        """箭头链接必须继续带上下文，否则翻一次页就丢了原来的筛选。"""
        ids = [
            self.create_photo(f"ctx-{i}.jpg", date_taken=f"2024:0{i + 1}:01 10:00:00")
            for i in range(2)
        ]
        for name in ("ctx-0.jpg", "ctx-1.jpg"):
            (self.image_directory / name).write_bytes(b"stub")
        _, client = self.logged_in_client()

        body = self._detail(client, ids[0], query="sort=oldest&limit=50")

        self.assertIn("data-nav-next", body)
        self.assertIn("return_query", body)
        self.assertIn("sort%3Doldest", body.replace("&amp;", "&"))

    def test_detail_has_fullscreen_trigger_and_adjacent_endpoint(self) -> None:
        """预览框要同时提供全屏入口与全屏内翻页所需的邻居接口地址。"""
        photo_id = self.create_photo("fullscreen.jpg")
        (self.image_directory / "fullscreen.jpg").write_bytes(b"not-a-real-image")
        _, client = self.logged_in_client()

        body = self._detail(client, photo_id)

        self.assertIn("data-fullscreen-trigger", body)
        self.assertIn("detail-image-button", body)
        # 全屏容器是预览框而不是图片按钮，否则箭头会被留在全屏之外
        self.assertIn("data-detail-viewer", body)
        self.assertIn(f"/api/admin/photos/{photo_id}/adjacent", body)

    def test_adjacent_api_returns_neighbours(self) -> None:
        """全屏内翻页依赖的邻居接口要返回编号，并遵守传入的筛选。"""
        ids = [
            self.create_photo(f"api-{i}.jpg", date_taken=f"2024:0{i + 1}:01 10:00:00")
            for i in range(3)
        ]
        _, client = self.logged_in_client()

        response = client.get(f"/api/admin/photos/{ids[1]}/adjacent")

        self.assertEqual(200, response.status_code)
        data = response.get_json()["data"]
        self.assertIn("previous_id", data)
        self.assertIn("next_id", data)
        self.assertEqual({ids[0], ids[2]}, {data["previous_id"], data["next_id"]})

    def test_adjacent_api_requires_login(self) -> None:
        """邻居接口属于后台接口，未认证不得访问。"""
        photo_id = self.create_photo("api-private.jpg")
        app = create_app(self.application_config())

        response = app.test_client().get(f"/api/admin/photos/{photo_id}/adjacent")

        self.assertIn(response.status_code, (301, 302, 303, 401, 403))

    def test_layout_places_scores_together_and_curation_after_city(self) -> None:
        """两个评分相邻，收录状态排在城市之后。"""
        photo_id = self.create_photo("layout.jpg")
        _, client = self.logged_in_client()

        body = self._detail(client, photo_id)

        memory_at = body.index('name="memory_score"')
        beauty_at = body.index('name="beauty_score"')
        city_at = body.index('name="exif_city"')
        curation_at = body.index('name="curation"')
        self.assertLess(memory_at, beauty_at, "回忆分应在美观分之前")
        self.assertLess(beauty_at, city_at, "两个评分应在原始信息修正之前，保持相邻")
        self.assertLess(city_at, curation_at, "收录状态应排在城市之后")

    def test_narration_label_uses_display_caption_wording(self) -> None:
        """界面上不再出现「旁白」这个称呼。"""
        photo_id = self.create_photo("wording.jpg")
        _, client = self.logged_in_client()

        body = self._detail(client, photo_id)

        self.assertIn("展示文案", body)
        self.assertNotIn("旁白", body)

    def test_preview_frame_suppresses_selection(self) -> None:
        """预览框必须禁止选中，否则连续点击箭头会在图片上留下选中色。

        连续快速点击会被浏览器判成双击/三击，在框内建立选区；箭头是绝对定位的
        兄弟节点，选区会横跨到中间的图片上，表现为图片蒙一层系统选中色。展示页
        的同类交互早就加了 user-select: none，后台这份最初漏了。
        """
        stylesheet = Path("src/server/static/css/admin.css").read_text(encoding="utf-8")
        start = stylesheet.index(".detail-viewer {")
        block = stylesheet[start : stylesheet.index("}", start)]

        self.assertIn("user-select: none", block)
        self.assertIn("-webkit-user-select: none", block)
        self.assertIn("-webkit-tap-highlight-color: transparent", block)

    def test_disabled_arrow_has_no_href_attribute(self) -> None:
        """禁用态箭头不能带空 href：href="" 在部分浏览器等价于当前页地址。"""
        photo_id = self.create_photo("single-arrow.jpg")
        (self.image_directory / "single-arrow.jpg").write_bytes(b"stub")
        _, client = self.logged_in_client()

        body = self._detail(client, photo_id)

        self.assertIn("detail-hint", body)
        self.assertNotIn('href=""', body)

    def test_fullscreen_navigation_queues_clicks(self) -> None:
        """全屏内连续点击必须排队而不是被静默丢弃。

        邻居查询要一次网络往返，而连续点击的间隔常常更短。早期实现用一个
        navigating 标志直接 return，表现为「点了三下只前进一张」。
        """
        script = Path("src/server/static/js/admin-photo-detail.js").read_text(encoding="utf-8")

        self.assertIn("function navigate(direction)", script)
        self.assertIn("pending", script)
        self.assertNotIn("if (!photoId || navigating) return;", script)
        # 选区兜底清理仍需保留：CSS 是主防线，这条防其他祖先节点产生的选区
        self.assertIn("removeAllRanges", script)

    def test_detail_shows_curation_control_with_current_value(self) -> None:
        """收录状态要作为可编辑控件出现，并预选当前值。"""
        excluded = self.create_photo("curation-off.jpg", is_included=0)
        _, client = self.logged_in_client()

        body = self._detail(client, excluded)

        self.assertIn('name="curation"', body)
        self.assertIn("收录状态", body)
        # WTForms 把 selected 渲染在 value 之前，断言顺序要按实际输出写
        self.assertIn('<option selected value="excluded">', body)
        self.assertNotIn('<option selected value="included">', body)

    def test_detail_form_saves_curation(self) -> None:
        """详情页保存能改收录状态，无需退回列表页批量操作。"""
        photo_id = self.create_photo("curation-save.jpg", is_included=1)
        app, client = self.logged_in_client()
        body = self._detail(client, photo_id)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token)
        version = self.read_photo(photo_id)["version"]

        response = client.post(
            f"/admin/photos/{photo_id}",
            data={
                "csrf_token": token.group(1),
                "version": version,
                "caption": "描述",
                "side_caption": "旁白",
                "reason": "",
                "exif_city": "",
                "category": "家人",
                "date_taken": "",
                "analysis_status": "legacy",
                "curation": "excluded",
            },
        )

        self.assertIn(response.status_code, (302, 303))
        after = self.read_photo(photo_id)
        self.assertEqual(0, after["is_included"])
        self.assertEqual(version + 1, after["version"])

    def test_curation_change_is_audited(self) -> None:
        """收录状态是展示可见性开关，改动必须可追溯到人。"""
        photo_id = self.create_photo("curation-audit.jpg", is_included=1)
        app, client = self.logged_in_client()
        body = self._detail(client, photo_id)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body).group(1)
        version = self.read_photo(photo_id)["version"]

        client.post(
            f"/admin/photos/{photo_id}",
            data={
                "csrf_token": token,
                "version": version,
                "caption": "",
                "side_caption": "",
                "reason": "",
                "exif_city": "",
                "category": "",
                "date_taken": "",
                "analysis_status": "legacy",
                "curation": "excluded",
            },
        )

        with self.database() as connection:
            row = connection.execute(
                "SELECT action, new_values_json, admin_username FROM photo_audit_log "
                "WHERE photo_id=? ORDER BY id DESC LIMIT 1",
                (photo_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual("photo_update", row["action"])
        self.assertIn("is_included", row["new_values_json"])
        self.assertEqual(ADMIN_USERNAME, row["admin_username"])

    def test_missing_curation_field_keeps_current_value(self) -> None:
        """不提交 curation 时保持原收录状态，而不是整张表单校验失败。

        新增下拉后如果按必填处理，浏览器里缓存的旧详情页一提交就是 400，用户会
        看到一次莫名的校验错误。取值合法性交给服务层把关，缺失即不改动。
        """
        photo_id = self.create_photo("curation-absent.jpg", is_included=1)
        _, client = self.logged_in_client()
        body = self._detail(client, photo_id)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body).group(1)
        version = self.read_photo(photo_id)["version"]

        response = client.post(
            f"/admin/photos/{photo_id}",
            data={
                "csrf_token": token,
                "version": version,
                "caption": "改了描述",
                "side_caption": "",
                "reason": "",
                "exif_city": "",
                "category": "",
                "date_taken": "",
                "analysis_status": "legacy",
            },
        )

        self.assertIn(response.status_code, (302, 303))
        after = self.read_photo(photo_id)
        self.assertEqual(1, after["is_included"], "缺失字段不应改动收录状态")
        self.assertEqual("改了描述", after["caption"])
