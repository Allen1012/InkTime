"""信息面板天气接入的接口级测试。

重点锁死一条硬要求：天气是唯一新增的外网依赖，它坏掉时不能影响日期、农历、
历史上的今天与照片展示。
"""

from __future__ import annotations

from typing import Any

from src.configuration import ConfigurationActor, ConfigurationValidationError
from src.server import weather
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


def _payload() -> dict:
    """构造一份 Open-Meteo 当前天气响应。"""
    return {
        "current": {
            "temperature_2m": 26.4,
            "apparent_temperature": 29.1,
            "relative_humidity_2m": 68,
            "weather_code": 2,
            "wind_speed_10m": 18.5,
            "wind_direction_10m": 135,
        }
    }


class PanelWeatherTestCase(TemporaryDatabaseTestCase):
    """校验 /api/panel 的天气段行为与降级隔离。"""

    def setUp(self) -> None:
        """准备应用、管理员并清空天气缓存。"""
        super().setUp()
        weather.reset_cache()
        self.addCleanup(weather.reset_cache)
        self.user_id = self.create_admin_user()
        self.actor = ConfigurationActor(self.user_id, "test-admin")
        self.app = create_app(self.application_config())
        self.configuration = self.app.extensions["inktime_services"]["configuration"]

    def change(self, **values: Any) -> None:
        """提交一批配置变更。"""
        self.configuration.update_batch(
            values, self.configuration.list_admin_settings()["version"], self.actor
        )

    def panel(self) -> dict:
        """调用公开面板接口并返回数据段。"""
        response = self.app.test_client().get("/api/panel")
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("ok", payload["status"])
        return payload["data"]

    def test_disabled_by_default_and_other_sections_intact(self) -> None:
        """验证默认关闭天气，其余面板段照常返回。"""
        data = self.panel()

        self.assertFalse(data["weather"]["available"])
        self.assertEqual("weather_disabled", data["weather"]["error"])
        self.assertIn("date", data)
        self.assertIn("lunar", data)
        self.assertIn("onthisday", data)

    def test_enabled_weather_is_merged_into_panel(self) -> None:
        """验证开启后天气段带回归一化字段，且不影响其他段。"""
        self.change(
            WEATHER_ENABLED=True,
            WEATHER_LOCATION="22.5,114.1",
            WEATHER_LOCATION_NAME="深圳",
        )
        original = weather._default_fetcher
        weather._default_fetcher = lambda url, timeout: _payload()
        self.addCleanup(setattr, weather, "_default_fetcher", original)

        data = self.panel()

        self.assertTrue(data["weather"]["available"])
        self.assertEqual("多云", data["weather"]["text"])
        self.assertEqual(26, data["weather"]["temperature"])
        self.assertEqual("深圳", data["weather"]["location_name"])
        self.assertIn("date", data)
        self.assertIn("lunar", data)

    def test_weather_failure_does_not_break_other_sections(self) -> None:
        """验证天气请求异常时其余面板段仍然可用。"""
        self.change(WEATHER_ENABLED=True, WEATHER_LOCATION="22.5,114.1")

        def failing(url: str, timeout: float) -> dict:
            raise OSError("network down")

        original = weather._default_fetcher
        weather._default_fetcher = failing
        self.addCleanup(setattr, weather, "_default_fetcher", original)

        data = self.panel()

        self.assertFalse(data["weather"]["available"])
        self.assertIn("error", data["weather"])
        self.assertTrue(data["date"]["iso"])
        self.assertIn("lunar", data)
        self.assertIn("onthisday", data)

    def test_location_falls_back_to_home_coordinates(self) -> None:
        """验证未配置天气坐标时使用常驻地坐标发起请求。"""
        self.change(WEATHER_ENABLED=True, HOME_LAT=31.24, HOME_LON=121.47)
        seen: list[str] = []

        def fetcher(url: str, timeout: float) -> dict:
            seen.append(url)
            return _payload()

        original = weather._default_fetcher
        weather._default_fetcher = fetcher
        self.addCleanup(setattr, weather, "_default_fetcher", original)

        self.panel()

        self.assertEqual(1, len(seen))
        self.assertIn("latitude=31.24", seen[0])
        self.assertIn("longitude=121.47", seen[0])

    def test_invalid_location_is_rejected_on_save(self) -> None:
        """验证非法坐标在保存时被拒绝且不落库。"""
        version = self.configuration.list_admin_settings()["version"]

        with self.assertRaises(ConfigurationValidationError) as captured:
            self.change(WEATHER_LOCATION="91,200")

        self.assertIn("纬度", captured.exception.errors["WEATHER_LOCATION"])
        self.assertEqual(version, self.configuration.list_admin_settings()["version"])

    def test_cache_prevents_repeated_external_requests(self) -> None:
        """验证缓存期内多次请求面板只访问外部服务一次。"""
        self.change(WEATHER_ENABLED=True, WEATHER_LOCATION="22.5,114.1")
        calls: list[str] = []

        def fetcher(url: str, timeout: float) -> dict:
            calls.append(url)
            return _payload()

        original = weather._default_fetcher
        weather._default_fetcher = fetcher
        self.addCleanup(setattr, weather, "_default_fetcher", original)

        self.panel()
        self.panel()

        self.assertEqual(1, len(calls))

    def test_dashboard_block_is_rendered_only_when_enabled(self) -> None:
        """验证仪表盘天气块由服务端按开关条件渲染。"""
        client = self.app.test_client()

        body = client.get("/display?template=dashboard").get_data(as_text=True)
        self.assertIn('id="dash-weather-block"', body)
        self.assertIn('id="wi-sun"', body)

        self.change(DISPLAY_WEATHER_SHOW=False)
        body = client.get("/display?template=dashboard").get_data(as_text=True)
        self.assertNotIn('id="dash-weather-block"', body)

    def test_immersive_corner_is_off_by_default(self) -> None:
        """验证沉浸式模板默认不渲染天气角标，打开后才出现。"""
        client = self.app.test_client()

        body = client.get("/display?template=classic").get_data(as_text=True)
        self.assertNotIn('id="display-weather"', body)

        self.change(DISPLAY_WEATHER_CORNER=True)
        body = client.get("/display?template=classic").get_data(as_text=True)
        self.assertIn('id="display-weather"', body)
