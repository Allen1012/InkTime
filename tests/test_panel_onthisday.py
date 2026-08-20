#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""校验「历史上的今天」多数据源取数、清洗与配置切换。

重点锁死三条：数据源可在后台切换且缓存互不串台；百度百科返回的超链接标签必须
被剥成纯文本；任一数据源不可达时只让历史事件段降级，日期与农历照常返回。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from src.configuration import SETTING_REGISTRY, ConfigurationActor
from src.server import panel
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


def _baidu_payload(month: int, day: int) -> dict:
    """构造一份百度百科整月响应，含超链接标签与逝世类事件。"""
    return {
        f"{month:02d}": {
            f"{month:02d}{day:02d}": [
                {
                    "year": "1833",
                    "title": '美国第23任总统<a target="_blank" href="https://baike.baidu.com/item/x">本杰明·哈里森</a>出生',
                    "type": "birth",
                },
                {
                    "year": "1854",
                    "title": '德国哲学家<a href="https://baike.baidu.com/item/y">谢林</a>逝世',
                    "type": "death",
                },
                {
                    "year": "1977",
                    "title": "美国发射<a>旅行者2号</a>探测器&nbsp;成功",
                    "type": "event",
                },
                {"year": "前221", "title": "秦统一六国", "type": "event"},
            ],
            f"{month:02d}01": [{"year": "1900", "title": "别的日子", "type": "event"}],
        }
    }


def _sixtys_payload(month: int, day: int) -> dict:
    """构造一份 60s 接口响应，字段已由上游清洗。"""
    return {
        "code": 200,
        "data": {
            "date": f"{month}-{day}",
            "month": month,
            "day": day,
            "items": [
                {"year": "1977", "title": "旅行者2号发射", "event_type": "event"},
                {"year": "1854", "title": "谢林逝世", "event_type": "death"},
            ],
        },
    }


class OnThisDaySourceTestCase(TemporaryDatabaseTestCase):
    """校验取数层的数据源分发、清洗与降级。"""

    def setUp(self) -> None:
        """清空面板缓存，避免用例之间互相命中。"""
        super().setUp()
        panel.reset_cache()
        self.addCleanup(panel.reset_cache)
        self.original_fetcher = panel._default_fetcher
        self.addCleanup(setattr, panel, "_default_fetcher", self.original_fetcher)

    def use_fetcher(self, fetcher: Any) -> list[str]:
        """替换取数实现并返回被请求过的地址列表。"""
        seen: list[str] = []

        def wrapped(url: str, timeout: float) -> Any:
            seen.append(url)
            return fetcher(url, timeout)

        panel._default_fetcher = wrapped
        return seen

    def test_default_source_is_baidu_with_chinese_name(self) -> None:
        """验证默认数据源为百度百科，且结果带中文源名。"""
        day = dt.date(2026, 8, 20)
        self.use_fetcher(lambda url, timeout: _baidu_payload(8, 20))

        result = panel.get_onthisday(day, count=3, strategy="recent")

        self.assertEqual("baidu", result["source"])
        self.assertEqual("百度百科", result["source_name"])
        self.assertTrue(result["available"])

    def test_baidu_strips_html_and_parses_year(self) -> None:
        """验证超链接标签、实体与公元前年份都被正确解析。"""
        day = dt.date(2026, 8, 20)
        self.use_fetcher(lambda url, timeout: _baidu_payload(8, 20))

        result = panel.get_onthisday(day, count=4, strategy="recent", source="baidu")

        texts = [item["text"] for item in result["items"]]
        self.assertIn("美国第23任总统本杰明·哈里森出生", texts)
        self.assertIn("美国发射旅行者2号探测器 成功", texts)
        for text in texts:
            self.assertNotIn("<", text)
        years = [item["year"] for item in result["items"]]
        self.assertEqual([1977, 1854, 1833, -221], years)

    def test_curated_drops_death_events_by_type(self) -> None:
        """验证规则精选按事件类型剔除逝世类条目。"""
        day = dt.date(2026, 8, 20)
        self.use_fetcher(lambda url, timeout: _baidu_payload(8, 20))

        result = panel.get_onthisday(
            day, count=2, strategy="curated", min_year=1900, source="baidu"
        )

        self.assertTrue(result["available"])
        for item in result["items"]:
            self.assertNotIn("逝世", item["text"])

    def test_sixtys_source_requests_matching_date(self) -> None:
        """验证 60s 源按 MM-DD 请求，并校验返回日期一致。"""
        day = dt.date(2026, 3, 5)
        seen = self.use_fetcher(lambda url, timeout: _sixtys_payload(3, 5))

        result = panel.get_onthisday(day, count=2, strategy="recent", source="60s")

        self.assertEqual(1, len(seen))
        self.assertIn("date=03-05", seen[0])
        self.assertEqual("60s", result["source"])
        self.assertEqual("60s 开源接口", result["source_name"])
        self.assertEqual("旅行者2号发射", result["items"][0]["text"])

    def test_sixtys_rejects_mismatched_date(self) -> None:
        """验证 60s 返回的日期与请求不一致时不采用该数据。"""
        day = dt.date(2026, 3, 5)
        self.use_fetcher(lambda url, timeout: _sixtys_payload(8, 20))

        result = panel.get_onthisday(day, count=2, strategy="recent", source="60s")

        self.assertFalse(result["available"])
        self.assertIn("不一致", result["error"])

    def test_wikipedia_source_still_available(self) -> None:
        """验证维基源保留可用，仍走原有 events 结构。"""
        day = dt.date(2026, 8, 20)
        payload = {"events": [{"year": 1977, "text": "旅行者2号發射"}]}
        seen = self.use_fetcher(lambda url, timeout: payload)

        result = panel.get_onthisday(
            day, count=1, strategy="recent", source="wikipedia"
        )

        self.assertIn("api.wikimedia.org", seen[0])
        self.assertEqual("维基百科", result["source_name"])
        self.assertEqual(1977, result["items"][0]["year"])

    def test_cache_is_isolated_per_source(self) -> None:
        """验证切换数据源不会命中上一个源的候选池缓存。"""
        day = dt.date(2026, 8, 20)

        def fetcher(url: str, timeout: float) -> Any:
            if "baike.baidu.com" in url:
                return _baidu_payload(8, 20)
            return _sixtys_payload(8, 20)

        seen = self.use_fetcher(fetcher)

        panel.get_onthisday(day, count=1, strategy="recent", source="baidu")
        panel.get_onthisday(day, count=1, strategy="recent", source="60s")
        panel.get_onthisday(day, count=1, strategy="recent", source="baidu")

        self.assertEqual(2, len(seen))
        self.assertTrue(any("baike.baidu.com" in url for url in seen))
        self.assertTrue(any("60s.viki.moe" in url for url in seen))

    def test_unknown_source_falls_back_to_baidu(self) -> None:
        """验证非法数据源回退百度百科而不是直接失败。"""
        day = dt.date(2026, 8, 20)
        seen = self.use_fetcher(lambda url, timeout: _baidu_payload(8, 20))

        result = panel.get_onthisday(
            day, count=1, strategy="recent", source="不存在的源"
        )

        self.assertEqual("baidu", result["source"])
        self.assertIn("baike.baidu.com", seen[0])

    def test_failure_reports_source_and_keeps_other_sections(self) -> None:
        """验证取数失败时历史段单独降级，日期与农历不受影响。"""
        day = dt.date(2026, 8, 20)

        def failing(url: str, timeout: float) -> Any:
            raise OSError("network down")

        self.use_fetcher(failing)

        data = panel.get_panel_data(day, source="baidu")

        self.assertFalse(data["onthisday"]["available"])
        self.assertEqual("百度百科", data["onthisday"]["source_name"])
        self.assertTrue(data["date"]["iso"])
        self.assertIn("lunar", data)


class OnThisDaySourceSettingTestCase(TemporaryDatabaseTestCase):
    """校验数据源配置项的注册、中文标签与在线切换。"""

    def setUp(self) -> None:
        """准备应用与管理员，并清空面板缓存。"""
        super().setUp()
        panel.reset_cache()
        self.addCleanup(panel.reset_cache)
        self.original_fetcher = panel._default_fetcher
        self.addCleanup(setattr, panel, "_default_fetcher", self.original_fetcher)
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.app = create_app(self.application_config())
        self.configuration = self.app.extensions["inktime_services"]["configuration"]

    def test_registry_exposes_chinese_choice_labels(self) -> None:
        """验证数据源与筛选策略在管理视图里显示中文名。"""
        definition = SETTING_REGISTRY["ONTHISDAY_SOURCE"]
        self.assertTrue(definition.editable)
        self.assertFalse(definition.restart_required)
        self.assertEqual("baidu", definition.default)

        settings = {
            item["key"]: item
            for item in self.configuration.list_admin_settings()["settings"]
        }
        self.assertEqual(
            {"baidu": "百度百科", "60s": "60s 开源接口", "wikipedia": "维基百科"},
            settings["ONTHISDAY_SOURCE"]["choice_labels"],
        )
        self.assertEqual(
            "规则精选", settings["ONTHISDAY_STRATEGY"]["choice_labels"]["curated"]
        )

    def test_panel_endpoint_follows_configured_source(self) -> None:
        """验证在线切换数据源后，面板接口按新数据源取数。"""
        seen: list[str] = []

        def fetcher(url: str, timeout: float) -> Any:
            seen.append(url)
            today = dt.date.today()
            if "baike.baidu.com" in url:
                return _baidu_payload(today.month, today.day)
            return _sixtys_payload(today.month, today.day)

        panel._default_fetcher = fetcher

        response = self.app.test_client().get("/api/panel")
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "百度百科", response.get_json()["data"]["onthisday"]["source_name"]
        )

        self.configuration.update_batch(
            {"ONTHISDAY_SOURCE": "60s"},
            self.configuration.list_admin_settings()["version"],
            self.actor,
        )
        response = self.app.test_client().get("/api/panel")

        payload = response.get_json()["data"]["onthisday"]
        self.assertEqual("60s", payload["source"])
        self.assertEqual("60s 开源接口", payload["source_name"])
        self.assertTrue(any("60s.viki.moe" in url for url in seen))
