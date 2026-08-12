"""展示页天气取数、映射与降级的单元测试。

按「先测后写」执行。测试全程不联网：外部请求由注入的假抓取函数替代，既能稳定跑，
也能精确验证超时、异常与陈旧兜底这些真实环境难以复现的分支。
"""

from __future__ import annotations

import unittest

from src.server import weather


def _open_meteo_payload(code: int = 2) -> dict:
    """构造一份 Open-Meteo 当前天气响应。"""
    return {
        "latitude": 22.5,
        "longitude": 114.1,
        "timezone": "Asia/Shanghai",
        "current": {
            "time": "2026-08-12T13:00",
            "temperature_2m": 26.4,
            "apparent_temperature": 29.1,
            "relative_humidity_2m": 68,
            "weather_code": code,
            "wind_speed_10m": 18.5,
            "wind_direction_10m": 135,
        },
    }


class ParseLocationTestCase(unittest.TestCase):
    """校验手动配置坐标的解析与回退。"""

    def test_explicit_location_wins_over_home(self) -> None:
        """验证显式配置的坐标覆盖常驻地坐标。"""
        self.assertEqual(
            (31.24, 121.47),
            weather.parse_location("31.24,121.47", home_lat=22.54, home_lon=114.06),
        )

    def test_blank_location_falls_back_to_home(self) -> None:
        """验证留空时回落到常驻地坐标。"""
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(
                    (22.54, 114.06),
                    weather.parse_location(raw, home_lat=22.54, home_lon=114.06),
                )

    def test_spaces_and_full_width_comma_are_tolerated(self) -> None:
        """验证容忍空格与中文逗号，减少手工填写出错。"""
        self.assertEqual(
            (31.24, 121.47),
            weather.parse_location(" 31.24 ， 121.47 ", home_lat=0.0, home_lon=0.0),
        )

    def test_invalid_location_is_rejected(self) -> None:
        """验证格式或范围非法时抛出可读错误。"""
        cases = {
            "31.24": "纬度",
            "abc,121": "数字",
            "91,121": "纬度",
            "31,181": "经度",
            "31,121,5": "格式",
        }
        for raw, keyword in cases.items():
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as captured:
                    weather.parse_location(raw, home_lat=0.0, home_lon=0.0)
                self.assertIn(keyword, str(captured.exception))


class DescribeWeatherCodeTestCase(unittest.TestCase):
    """校验 WMO 天气代码到中文与图标的映射。"""

    def test_common_codes_map_to_expected_text(self) -> None:
        """验证常见天气代码映射到预期中文。"""
        expected = {
            0: "晴",
            1: "晴间多云",
            2: "多云",
            3: "阴",
            45: "雾",
            51: "小雨",
            61: "小雨",
            63: "中雨",
            65: "大雨",
            71: "小雪",
            75: "大雪",
            80: "阵雨",
            95: "雷阵雨",
            96: "雷阵雨伴冰雹",
        }
        for code, text in expected.items():
            with self.subTest(code=code):
                self.assertEqual(text, weather.describe_weather_code(code)["text"])

    def test_every_documented_code_has_icon(self) -> None:
        """验证映射表中每个代码都有可用图标名。"""
        for code in weather.WEATHER_CODES:
            with self.subTest(code=code):
                icon = weather.describe_weather_code(code)["icon"]
                self.assertIn(icon, weather.WEATHER_ICONS)

    def test_unknown_code_degrades_without_raising(self) -> None:
        """验证未知代码不抛错，退化为通用描述与云图标。"""
        result = weather.describe_weather_code(999)
        self.assertEqual("未知", result["text"])
        self.assertIn(result["icon"], weather.WEATHER_ICONS)


class BeaufortLevelTestCase(unittest.TestCase):
    """校验风速到蒲福风级的换算。"""

    def test_boundaries_match_standard_table(self) -> None:
        """验证各级边界按标准风级表换算。"""
        cases = [(0.0, 0), (1.0, 1), (6.0, 2), (12.0, 3), (20.0, 4), (30.0, 5), (118.0, 12)]
        for speed, level in cases:
            with self.subTest(speed=speed):
                self.assertEqual(level, weather.beaufort_level(speed))

    def test_extreme_and_invalid_speeds_are_clamped(self) -> None:
        """验证超大或非法风速被收敛，不越出 0 至 12。"""
        self.assertEqual(12, weather.beaufort_level(500))
        self.assertEqual(0, weather.beaufort_level(-5))
        self.assertEqual(0, weather.beaufort_level(None))

    def test_wind_direction_is_described_in_chinese(self) -> None:
        """验证风向角度描述为中文方位。"""
        self.assertEqual("北风", weather.describe_wind_direction(0))
        self.assertEqual("东南风", weather.describe_wind_direction(135))
        self.assertEqual("西风", weather.describe_wind_direction(270))
        self.assertEqual("", weather.describe_wind_direction(None))


class GetWeatherTestCase(unittest.TestCase):
    """校验取数主流程：缓存、降级与陈旧兜底。"""

    def setUp(self) -> None:
        """每个测试使用干净的缓存。"""
        weather.reset_cache()

    def test_disabled_returns_unavailable_without_fetching(self) -> None:
        """验证总开关关闭时不发起任何请求。"""
        calls: list[str] = []

        def fetcher(url: str, timeout: float) -> dict:
            calls.append(url)
            return _open_meteo_payload()

        result = weather.get_weather(enabled=False, home_lat=22.5, home_lon=114.1, fetcher=fetcher)

        self.assertFalse(result["available"])
        self.assertEqual([], calls)

    def test_successful_fetch_returns_normalized_fields(self) -> None:
        """验证成功取数后返回归一化后的展示字段。"""
        result = weather.get_weather(
            enabled=True,
            home_lat=22.5,
            home_lon=114.1,
            location_name="深圳",
            fetcher=lambda url, timeout: _open_meteo_payload(2),
        )

        self.assertTrue(result["available"])
        self.assertEqual("多云", result["text"])
        self.assertEqual(26, result["temperature"])
        self.assertEqual(29, result["apparent_temperature"])
        self.assertEqual(68, result["humidity"])
        self.assertEqual(3, result["wind_level"])
        self.assertEqual("东南风", result["wind_direction"])
        self.assertEqual("深圳", result["location_name"])
        self.assertFalse(result["stale"])
        self.assertIn(result["icon"], weather.WEATHER_ICONS)

    def test_second_call_within_ttl_uses_cache(self) -> None:
        """验证缓存有效期内不再访问外部服务。"""
        calls: list[str] = []

        def fetcher(url: str, timeout: float) -> dict:
            calls.append(url)
            return _open_meteo_payload()

        first = weather.get_weather(enabled=True, home_lat=22.5, home_lon=114.1, fetcher=fetcher)
        second = weather.get_weather(enabled=True, home_lat=22.5, home_lon=114.1, fetcher=fetcher)

        self.assertEqual(1, len(calls))
        self.assertEqual(first["fetched_at"], second["fetched_at"])

    def test_expired_cache_triggers_refetch(self) -> None:
        """验证缓存过期后重新获取。"""
        calls: list[str] = []
        clock = [1000.0]

        def fetcher(url: str, timeout: float) -> dict:
            calls.append(url)
            return _open_meteo_payload()

        for _ in range(2):
            weather.get_weather(
                enabled=True, home_lat=22.5, home_lon=114.1,
                cache_minutes=1, fetcher=fetcher, clock=lambda: clock[0],
            )
        clock[0] += 61
        weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1,
            cache_minutes=1, fetcher=fetcher, clock=lambda: clock[0],
        )

        self.assertEqual(2, len(calls))

    def test_different_location_uses_separate_cache_entry(self) -> None:
        """验证缓存按坐标区分，换位置不会读到旧数据。"""
        calls: list[str] = []

        def fetcher(url: str, timeout: float) -> dict:
            calls.append(url)
            return _open_meteo_payload()

        weather.get_weather(enabled=True, home_lat=22.5, home_lon=114.1, fetcher=fetcher)
        weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1,
            location="31.24,121.47", fetcher=fetcher,
        )

        self.assertEqual(2, len(calls))

    def test_fetch_failure_degrades_without_raising(self) -> None:
        """验证请求异常时降级返回，不抛出到调用方。"""
        def fetcher(url: str, timeout: float) -> dict:
            raise TimeoutError("timed out")

        result = weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1, fetcher=fetcher
        )

        self.assertFalse(result["available"])
        self.assertIn("error", result)

    def test_malformed_payload_degrades(self) -> None:
        """验证响应结构异常时降级。"""
        for payload in ({}, {"current": {}}, {"current": {"temperature_2m": "abc"}}):
            with self.subTest(payload=payload):
                weather.reset_cache()
                result = weather.get_weather(
                    enabled=True, home_lat=22.5, home_lon=114.1,
                    fetcher=lambda url, timeout: payload,
                )
                self.assertFalse(result["available"])

    def test_stale_data_is_served_after_later_failure(self) -> None:
        """验证曾成功过之后再失败，返回上次数据并标注陈旧。"""
        clock = [1000.0]
        weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1, cache_minutes=1,
            fetcher=lambda url, timeout: _open_meteo_payload(0), clock=lambda: clock[0],
        )
        clock[0] += 120

        def failing(url: str, timeout: float) -> dict:
            raise OSError("network down")

        result = weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1, cache_minutes=1,
            fetcher=failing, clock=lambda: clock[0],
        )

        self.assertTrue(result["available"])
        self.assertTrue(result["stale"])
        self.assertEqual("晴", result["text"])

    def test_invalid_location_degrades_without_fetching(self) -> None:
        """验证坐标非法时直接降级，不发起请求。"""
        calls: list[str] = []

        result = weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1, location="abc",
            fetcher=lambda url, timeout: calls.append(url) or {},
        )

        self.assertFalse(result["available"])
        self.assertEqual([], calls)

    def test_request_url_targets_open_meteo_with_expected_fields(self) -> None:
        """验证请求地址包含坐标与所需字段，避免多取无用数据。"""
        seen: list[str] = []

        weather.get_weather(
            enabled=True, home_lat=22.5, home_lon=114.1,
            fetcher=lambda url, timeout: seen.append(url) or _open_meteo_payload(),
        )

        self.assertEqual(1, len(seen))
        url = seen[0]
        self.assertIn("api.open-meteo.com", url)
        self.assertIn("latitude=22.5", url)
        self.assertIn("longitude=114.1", url)
        for field in (
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "weather_code",
            "wind_speed_10m",
        ):
            self.assertIn(field, url)
