"""后台时间展示格式化的测试。

起因：照片详情页把 EXIF 原值 `2026:01:31 14:27:37` 直接显示出来。冒号分隔的日期是
EXIF 标准格式（`DateTimeOriginal`），数据库存原值是对的——分析、渲染与选片都按该格式
解析——但展示层不该把它原样丢给人看。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.server.formatting import readable_time


class ReadableTimeTestCase(unittest.TestCase):
    """校验各类时间字符串的可读化。"""

    def test_exif_datetime_is_reformatted(self) -> None:
        """验证 EXIF 冒号格式转为常见读法。"""
        self.assertEqual(
            "2026年1月31日 14:27", readable_time("2026:01:31 14:27:37")
        )

    def test_exif_date_only(self) -> None:
        """验证只有日期部分时不编造时间。"""
        self.assertEqual("2026年1月31日", readable_time("2026:01:31"))

    def test_iso_timestamp_with_timezone_is_converted_to_local(self) -> None:
        """验证带时区的时间戳换算为本机时区再展示。

        后台的创建时间与更新时间以协调世界时存储，原样展示会比本地时间差好几个小时。
        """
        moment = datetime(2026, 8, 12, 9, 5, 35, tzinfo=timezone.utc)
        expected_local = moment.astimezone()
        self.assertEqual(
            f"{expected_local.year}年{expected_local.month}月{expected_local.day}日 "
            f"{expected_local.hour:02d}:{expected_local.minute:02d}",
            readable_time("2026-08-12T09:05:35+00:00"),
        )

    def test_iso_without_timezone_is_treated_as_local(self) -> None:
        """验证不带时区的时间按本地时间处理，不做换算。"""
        self.assertEqual(
            "2026年8月12日 17:05", readable_time("2026-08-12T17:05:35")
        )

    def test_common_dash_format(self) -> None:
        """验证常见的短横线日期时间格式。"""
        self.assertEqual(
            "2026年8月12日 17:05", readable_time("2026-08-12 17:05:35")
        )

    def test_blank_and_unparsable_values_degrade(self) -> None:
        """验证空值与无法解析的值原样返回，不隐藏数据也不报错。"""
        self.assertEqual("", readable_time(""))
        self.assertEqual("", readable_time(None))
        self.assertEqual("未知时间", readable_time("未知时间"))

    def test_datetime_object_is_supported(self) -> None:
        """验证直接传入时间对象也能格式化。"""
        self.assertEqual(
            "2026年3月4日 05:06", readable_time(datetime(2026, 3, 4, 5, 6, 7))
        )

    def test_filter_is_registered_on_application(self) -> None:
        """验证过滤器已注册到应用，模板可直接使用。"""
        from src.server.app import create_app
        from tests.support import TemporaryDatabaseTestCase

        class _Case(TemporaryDatabaseTestCase):
            """仅用于借用临时数据库夹具。"""

            def runTest(self) -> None:  # pragma: no cover - 由外层驱动
                pass

        case = _Case()
        case.setUp()
        try:
            app = create_app(case.application_config())
            self.assertIn("readable_time", app.jinja_env.filters)
            with app.app_context():
                rendered = app.jinja_env.from_string(
                    "{{ value | readable_time }}"
                ).render(value="2026:01:31 14:27:37")
            self.assertEqual("2026年1月31日 14:27", rendered)
        finally:
            case.doCleanups()
