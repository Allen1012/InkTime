"""展示页天气取数模块。

数据源默认 Open-Meteo：免费、无需注册、无需 API key，并按位置自动选择最优预报模型
（国内位置会用到中国气象局的 CMA GRAPES）。返回的是 WMO 天气代码，中文与图标映射
由本模块维护。

设计原则与 `panel.py` 保持一致：

- 只用标准库 `urllib.request`，不引入新依赖
- 服务端 TTL 缓存加线程锁，避免多请求打爆外部服务
- 任何异常都降级为 `{"available": False}`，绝不让天气故障影响照片展示
- 曾成功取过数则在失败时返回上次结果并标注 `stale`，比直接消失更有用
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
)
REQUEST_TIMEOUT_SEC = 5.0
DEFAULT_CACHE_MINUTES = 15
# 陈旧数据的兜底上限：超过这个时长仍取不到就不再展示，避免显示昨天的天气
STALE_LIMIT_SEC = 6 * 3600

# 图标名集合，对应前端内联 SVG 的 id
WEATHER_ICONS = (
    "sun",
    "cloud-sun",
    "cloud",
    "fog",
    "drizzle",
    "rain",
    "snow",
    "sleet",
    "shower",
    "thunder",
    "hail",
)

# WMO 天气代码到中文与图标的映射
# https://open-meteo.com/en/docs 的 weather_code 定义
WEATHER_CODES: Dict[int, Tuple[str, str]] = {
    0: ("晴", "sun"),
    1: ("晴间多云", "cloud-sun"),
    2: ("多云", "cloud"),
    3: ("阴", "cloud"),
    45: ("雾", "fog"),
    48: ("冻雾", "fog"),
    51: ("小雨", "drizzle"),
    53: ("小雨", "drizzle"),
    55: ("中雨", "drizzle"),
    56: ("冻雨", "sleet"),
    57: ("冻雨", "sleet"),
    61: ("小雨", "rain"),
    63: ("中雨", "rain"),
    65: ("大雨", "rain"),
    66: ("冻雨", "sleet"),
    67: ("冻雨", "sleet"),
    71: ("小雪", "snow"),
    73: ("中雪", "snow"),
    75: ("大雪", "snow"),
    77: ("米雪", "snow"),
    80: ("阵雨", "shower"),
    81: ("阵雨", "shower"),
    82: ("强阵雨", "shower"),
    85: ("阵雪", "snow"),
    86: ("强阵雪", "snow"),
    95: ("雷阵雨", "thunder"),
    96: ("雷阵雨伴冰雹", "hail"),
    99: ("雷阵雨伴冰雹", "hail"),
}

# 蒲福风级下界，单位 km/h
_BEAUFORT_BOUNDS = (1.0, 6.0, 12.0, 20.0, 29.0, 39.0, 50.0, 62.0, 75.0, 89.0, 103.0, 118.0)
_WIND_DIRECTIONS = (
    "北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风",
)

_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    """清空缓存，供隔离验证使用。"""
    with _cache_lock:
        _cache.clear()


def parse_location(
    raw: Any, *, home_lat: float, home_lon: float
) -> Tuple[float, float]:
    """解析手动配置的坐标，留空时回落到常驻地坐标。

    容忍空格与中文逗号：这个值多半是人手抄进去的，为标点格式报错没有意义。

    Args:
        raw: `纬度,经度` 形式的配置值，可为空。
        home_lat: 回落使用的常驻地纬度。
        home_lon: 回落使用的常驻地经度。

    Returns:
        纬度与经度元组。

    Raises:
        ValueError: 格式不是两段、不是数字，或超出经纬度范围。
    """
    text = str(raw or "").strip().replace("，", ",")
    if not text:
        return float(home_lat), float(home_lon)
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        raise ValueError("坐标格式必须是「纬度,经度」")
    try:
        latitude = float(parts[0])
        longitude = float(parts[1])
    except ValueError as error:
        raise ValueError("坐标必须是数字") from error
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("纬度必须在 -90 到 90 之间")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("经度必须在 -180 到 180 之间")
    return latitude, longitude


def describe_weather_code(code: Any) -> Dict[str, str]:
    """把 WMO 天气代码映射为中文描述与图标名，未知代码安全降级。"""
    try:
        normalized = int(code)
    except (TypeError, ValueError):
        normalized = -1
    text, icon = WEATHER_CODES.get(normalized, ("未知", "cloud"))
    return {"text": text, "icon": icon}


def beaufort_level(speed_kmh: Any) -> int:
    """把风速（km/h）换算为 0 至 12 的蒲福风级，非法值按 0 处理。"""
    try:
        speed = float(speed_kmh)
    except (TypeError, ValueError):
        return 0
    if speed <= 0:
        return 0
    level = 0
    for index, bound in enumerate(_BEAUFORT_BOUNDS):
        if speed >= bound:
            level = index + 1
    return min(12, level)


def describe_wind_direction(degrees: Any) -> str:
    """把风向角度描述为八方位中文，缺失时返回空串。"""
    try:
        angle = float(degrees) % 360.0
    except (TypeError, ValueError):
        return ""
    index = int((angle + 22.5) // 45) % 8
    return _WIND_DIRECTIONS[index]


def _default_fetcher(url: str, timeout: float) -> Dict[str, Any]:
    """用标准库获取并解析 JSON，与 panel 模块抓取维基百科的方式一致。"""
    request = urllib.request.Request(url, headers={"User-Agent": "InkTime/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("天气响应不是 JSON 对象")
    return parsed


def _build_url(latitude: float, longitude: float) -> str:
    """构造只取当前天气所需字段的请求地址，不多取无用数据。"""
    query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:g}",
            "longitude": f"{longitude:g}",
            "current": ",".join(CURRENT_FIELDS),
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        }
    )
    return f"{OPEN_METEO_ENDPOINT}?{query}"


def _normalize(payload: Dict[str, Any], location_name: str) -> Dict[str, Any]:
    """把外部响应归一化为展示字段，字段缺失或类型不对即视为失败。"""
    current = payload.get("current")
    if not isinstance(current, dict):
        raise ValueError("天气响应缺少 current 字段")
    temperature = float(current["temperature_2m"])
    apparent = current.get("apparent_temperature", temperature)
    described = describe_weather_code(current.get("weather_code"))
    return {
        "available": True,
        "stale": False,
        "text": described["text"],
        "icon": described["icon"],
        "temperature": round(temperature),
        "apparent_temperature": round(float(apparent)),
        "humidity": int(float(current.get("relative_humidity_2m") or 0)),
        "wind_level": beaufort_level(current.get("wind_speed_10m")),
        "wind_direction": describe_wind_direction(current.get("wind_direction_10m")),
        "location_name": location_name,
        "provider": "open-meteo",
    }


def get_weather(
    *,
    enabled: bool,
    home_lat: float,
    home_lon: float,
    location: Any = "",
    location_name: str = "",
    cache_minutes: int = DEFAULT_CACHE_MINUTES,
    fetcher: Optional[Callable[[str, float], Dict[str, Any]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """返回当前天气，带缓存、降级与陈旧兜底。

    任何失败都以 `{"available": False, "error": ...}` 返回而不抛出：调用方是信息面板
    聚合入口，天气不可用绝不能影响日期、农历、历史上的今天与照片展示。

    Args:
        enabled: 总开关，关闭时不发起任何请求。
        home_lat: 常驻地纬度，作为坐标回落值。
        home_lon: 常驻地经度，作为坐标回落值。
        location: 手动配置的 `纬度,经度`，留空使用常驻地。
        location_name: 显示用地名，留空则前端不显示。
        cache_minutes: 服务端缓存分钟数。
        fetcher: 可注入的抓取函数，用于隔离验证。
        clock: 可注入的时钟，用于隔离验证缓存过期。

    Returns:
        含 available、text、icon、temperature 等字段的展示字典。
    """
    if not enabled:
        return {"available": False, "error": "weather_disabled"}
    now = (clock or time.time)()
    try:
        latitude, longitude = parse_location(
            location, home_lat=home_lat, home_lon=home_lon
        )
    except ValueError as error:
        return {"available": False, "error": str(error)}

    ttl = max(60.0, float(cache_minutes) * 60.0)
    key = f"open-meteo:{latitude:g},{longitude:g}"
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit["ts"] < ttl:
        cached = dict(hit["data"])
        cached["fetched_at"] = hit["ts"]
        cached["stale"] = False
        return cached

    try:
        payload = (fetcher or _default_fetcher)(
            _build_url(latitude, longitude), REQUEST_TIMEOUT_SEC
        )
        data = _normalize(payload, str(location_name or "").strip())
    except Exception as error:  # 外部依赖的任何异常都不应向上冒泡
        if hit is not None and now - hit["ts"] < STALE_LIMIT_SEC:
            stale = dict(hit["data"])
            stale["stale"] = True
            stale["fetched_at"] = hit["ts"]
            return stale
        return {"available": False, "error": f"{type(error).__name__}: {error}"[:200]}

    with _cache_lock:
        _cache[key] = {"ts": now, "data": dict(data)}
    data["fetched_at"] = now
    return data
