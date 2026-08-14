"""缺拍摄时间的照片仍可展示，以及后台补录提示的回归测试。

设计决定：照片被放进相册就是希望被展示，缺拍摄时间不该成为永不露面的理由。
缺日期只影响两件事——画面上不显示日期，且无法参与「历史上的今天」的月日匹配
（没有日期无从匹配）；它们通过补足档进入当天画面。后台给出提示引导补录。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from src.server.app import create_app
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase


class UndatedPhotoSelectionTestCase(TemporaryDatabaseTestCase):
    """校验渲染选片不再按有无拍摄时间过滤。"""

    def _insert(self, name: str, *, date_taken: str | None, memory: float) -> int:
        """写入一张可展示照片，date_taken 为 None 表示没有拍摄时间。"""
        path = self.image_directory / name
        path.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,exif_datetime,memory_score,side_caption,"
                "analysis_status,is_deleted,created_at,updated_at,version) "
                "VALUES (?,?,?,?,'succeeded',0,?,?,1)",
                (
                    str(path),
                    date_taken,
                    memory,
                    f"side-{name}",
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                ),
            )
            return int(cursor.lastrowid)

    def _load_items(self) -> list[dict]:
        """用临时数据库加载渲染候选池。"""
        import src.render.render_daily_photo as render

        original = render.DB_PATH
        render.DB_PATH = self.database_path
        try:
            return render.load_sim_rows()
        finally:
            render.DB_PATH = original

    def test_undated_photo_enters_candidate_pool(self) -> None:
        """没有拍摄时间的照片必须留在候选池里，否则永不展示。"""
        self._insert("undated.jpg", date_taken=None, memory=90.0)

        items = self._load_items()

        names = {Path(item["path"]).name for item in items}
        self.assertIn("undated.jpg", names)

    def test_undated_photo_has_empty_date_and_md(self) -> None:
        """缺日期的条目 date 与 md 都为空串，画布上日期渲染为空。"""
        self._insert("blank-date.jpg", date_taken=None, memory=90.0)

        item = next(
            i for i in self._load_items() if Path(i["path"]).name == "blank-date.jpg"
        )

        self.assertEqual("", item["date"])
        self.assertEqual("", item["md"])

    def test_empty_string_date_treated_as_missing(self) -> None:
        """空字符串与 NULL 同等对待，不能产生半个日期。"""
        self._insert("empty-date.jpg", date_taken="", memory=90.0)

        item = next(
            i for i in self._load_items() if Path(i["path"]).name == "empty-date.jpg"
        )

        self.assertEqual("", item["md"])

    def test_undated_photo_never_joins_month_day_group(self) -> None:
        """缺日期照片不能污染月日匹配，否则会被当成「今天拍的」。"""
        import src.render.render_daily_photo as render

        self._insert("undated.jpg", date_taken=None, memory=99.0)
        today = dt.date.today()
        dated_name = "dated.jpg"
        self._insert(
            dated_name,
            date_taken=f"2019:{today.month:02d}:{today.day:02d} 12:00:00",
            memory=80.0,
        )

        chosen, info = render.choose_photos_for_today(self._load_items(), today, count=1)

        # 今天有真实匹配时，应当选中当天的照片，而不是分数更高的无日期照片
        self.assertEqual(dated_name, Path(chosen[0]["path"]).name)
        self.assertEqual(f"{today.month:02d}-{today.day:02d}", info["used_md"])

    def test_undated_photo_fills_remaining_slots(self) -> None:
        """当天照片不够时，缺日期照片通过补足档进入画面。"""
        import src.render.render_daily_photo as render

        today = dt.date.today()
        self._insert(
            "dated.jpg",
            date_taken=f"2019:{today.month:02d}:{today.day:02d} 12:00:00",
            memory=80.0,
        )
        self._insert("undated-a.jpg", date_taken=None, memory=95.0)
        self._insert("undated-b.jpg", date_taken=None, memory=94.0)

        chosen, info = render.choose_photos_for_today(self._load_items(), today, count=3)

        self.assertEqual(3, len(chosen))
        self.assertGreater(info["filled_from_global"], 0)
        undated = [c for c in chosen if not c["md"]]
        self.assertTrue(undated, "补足档应当纳入缺日期照片")


class MissingDateAdminHintTestCase(TemporaryDatabaseTestCase):
    """校验后台的缺日期统计、筛选与详情页提示。"""

    ADMIN_USERNAME = "hint-admin"
    ADMIN_PASSWORD = "inktime-missing-date-password"

    def setUp(self) -> None:
        """创建应用与已登录会话。"""
        super().setUp()
        self.application = create_app(self.application_config())
        with self.application.app_context():
            self.application.extensions["inktime_services"]["auth"].create_admin(
                self.ADMIN_USERNAME, self.ADMIN_PASSWORD
            )
        self.client = self.application.test_client()
        body = self.client.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        self.assertIsNotNone(token, "登录页应包含跨站请求伪造令牌")
        response = self.client.post(
            "/admin/login",
            data={
                "username": self.ADMIN_USERNAME,
                "password": self.ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
        )
        self.assertIn(response.status_code, (302, 303))

    def _insert_undated(self, name: str) -> int:
        """写入一张没有拍摄时间的照片。"""
        path = self.image_directory / name
        path.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,exif_datetime,date_source,"
                "analysis_status,is_deleted,created_at,updated_at,version) "
                "VALUES (?,NULL,'none','succeeded',0,?,?,1)",
                (str(path), TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
            return int(cursor.lastrowid)

    def test_dashboard_counts_missing_date(self) -> None:
        """首页统计缺日期数量，空串与 NULL 都算。"""
        self._insert_undated("a.jpg")
        path = self.image_directory / "b.jpg"
        path.write_bytes(b"x")
        with self.database() as connection:
            connection.execute(
                "INSERT INTO photo_scores (path,exif_datetime,analysis_status,"
                "is_deleted,created_at,updated_at,version) "
                "VALUES (?,'   ','succeeded',0,?,?,1)",
                (str(path), TEST_TIMESTAMP, TEST_TIMESTAMP),
            )
        self.create_photo("dated.jpg")  # 有拍摄时间，不该计入

        with self.application.test_request_context("/admin"):
            service = self.application.extensions["inktime_services"]["admin_photo"]
            statistics = service.dashboard()

        self.assertTrue(statistics["missing_date"]["available"])
        self.assertEqual(2, statistics["missing_date"]["data"])

    def test_missing_date_filter_narrows_list(self) -> None:
        """勾选「只看缺拍摄时间」后只返回缺日期的照片。"""
        self._insert_undated("undated.jpg")
        self.create_photo("dated.jpg")

        with self.application.test_request_context("/admin/photos"):
            service = self.application.extensions["inktime_services"]["admin_photo"]
            result = service.list_photos(
                1, 24, "", "", "", "", "", "latest", "grid", missing_date=True
            )

        names = {item["title"] for item in result["items"]}
        self.assertEqual({"undated.jpg"}, names)

    def test_dashboard_links_to_missing_date_filter(self) -> None:
        """首页卡片可点击直达筛选后的列表。"""
        self._insert_undated("undated.jpg")

        body = self.client.get("/admin").get_data(as_text=True)

        self.assertIn("缺拍摄时间", body)
        self.assertIn("missing_date=1", body)

    def test_detail_page_prompts_when_date_missing(self) -> None:
        """详情页在编辑区就近提示补录，并说明照常展示。"""
        photo_id = self._insert_undated("undated.jpg")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertIn("没有拍摄时间", body)
        self.assertIn("照常展示", body)

    def test_detail_page_has_no_prompt_when_date_present(self) -> None:
        """有拍摄时间的照片不显示提示，避免噪音。"""
        photo_id = self.create_photo("dated.jpg")

        body = self.client.get(f"/admin/photos/{photo_id}").get_data(as_text=True)

        self.assertNotIn("没有拍摄时间", body)
