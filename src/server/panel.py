#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""展示页日期、农历与历史事件信息面板的数据提供层。"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.provider_fallback import fallback_reason

try:
    from lunar_python import Solar
except ImportError:
    Solar = None

try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
# 数据源标识到中文名的映射，界面、接口与日志一律显示中文名。
# 标识本身保持英数字：它要写进 .env、容器环境变量和数据库配置，中文值在这些
# 位置容易因编码或转义出问题。
ONTHISDAY_SOURCE_NAMES = {
    "baidu": "百度百科",
    "60s": "60s 开源接口",
    "wikipedia": "维基百科",
}
DEFAULT_ONTHISDAY_SOURCE = "baidu"
# 百度百科按月返回整月事件，键为月份与「月日」两级；国内直连，无需密钥。
BAIDU_ONTHISDAY_URL = "https://baike.baidu.com/cms/home/eventsOnHistory/{month:02d}.json"
# 60s 是百度数据的开源二次封装，字段已清洗，可作备用源；date 参数为 MM-DD。
SIXTYS_ONTHISDAY_URL = "https://60s.viki.moe/v2/today_in_history?date={month:02d}-{day:02d}"
WIKI_ONTHISDAY_URL = "https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/events/{month:02d}/{day:02d}"
ONTHISDAY_TIMEOUT_SEC = 8
ONTHISDAY_CACHE_TTL_SEC = 24 * 3600
ONTHISDAY_POOL_SIZE = 30
ONTHISDAY_COUNT = 2
ONTHISDAY_STRATEGY = "curated"
ONTHISDAY_MIN_YEAR = 1900
ONTHISDAY_SOURCE = DEFAULT_ONTHISDAY_SOURCE
AI_TIMEOUT_SEC = 20
# 百度百科与 60s 都带事件类型，逝世类直接按类型剔除，比关键词命中准。
NEGATIVE_EVENT_TYPES = {"death"}
NEGATIVE_KEYWORDS = [
    "死", "亡", "逝世", "去世", "病逝", "罹难", "遇难", "伤亡", "受伤", "灾害",
    "灾难", "地震", "海啸",
    "洪水", "泥石流", "坠毁", "空难", "沉没", "爆炸", "袭击", "屠杀", "战争",
    "战役", "入侵", "轰炸", "枪击", "刺杀", "处决", "起义", "暴动", "骚乱",
    "事故", "失控", "瘟疫", "疫情", "病毒", "饥荒", "绑架", "劫机",
]
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
LOGGER = logging.getLogger(__name__)


def _to_simplified(text: str) -> str:
    """把繁体文本转为简体，转换依赖不可用或失败时返回原文。"""
    if not text or _zh_convert is None:
        return text or ""
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text


def get_date_info(today: Optional[dt.date] = None) -> Dict[str, Any]:
    """返回完全本地计算的公历日期信息。"""
    day = today or dt.date.today()
    return {
        "iso": day.isoformat(), "year": day.year, "month": day.month,
        "day": day.day, "weekday": WEEKDAY_CN[day.weekday()],
        "day_of_year": day.timetuple().tm_yday,
    }


def get_lunar_info(today: Optional[dt.date] = None) -> Dict[str, Any]:
    """返回本地农历、节气与传统节日，依赖不可用时独立降级。"""
    day = today or dt.date.today()
    if Solar is None:
        return {"available": False, "error": "未安装 lunar-python"}
    try:
        solar = Solar.fromYmd(day.year, day.month, day.day)
        lunar = solar.getLunar()
        jieqi_today = lunar.getJieQi() or ""
        next_term = lunar.getNextJieQi()
        if next_term is not None and next_term.getSolar().toYmd() == day.isoformat():
            candidate = lunar.getNextJieQi(True)
            if candidate is not None and candidate.getSolar().toYmd() != day.isoformat():
                next_term = candidate
            else:
                tomorrow = day + dt.timedelta(days=1)
                next_term = Solar.fromYmd(
                    tomorrow.year, tomorrow.month, tomorrow.day
                ).getLunar().getNextJieQi()
        next_jieqi = None
        if next_term is not None:
            next_day = dt.date.fromisoformat(next_term.getSolar().toYmd())
            next_jieqi = {
                "name": next_term.getName(), "date": next_day.isoformat(),
                "days_left": (next_day - day).days,
            }
        return {
            "available": True,
            "text": f"{lunar.getYearInGanZhi()}年 {lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
            "month_cn": lunar.getMonthInChinese(), "day_cn": lunar.getDayInChinese(),
            "ganzhi_year": lunar.getYearInGanZhi(),
            "shengxiao": lunar.getYearShengXiao(), "jieqi": jieqi_today,
            "next_jieqi": next_jieqi, "festivals": list(lunar.getFestivals() or []),
        }
    except Exception as error:
        return {"available": False, "error": f"农历计算失败: {error}"}


def _fetch_json(url: str, timeout: float) -> Any:
    """请求外部接口并解析 JSON；抽成独立函数便于测试替换。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "InkTime/1.0 (personal photo frame)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# 测试与调试可替换该引用，取数实现与筛选逻辑因此保持解耦。
_default_fetcher: Callable[[str, float], Any] = _fetch_json


def source_name(source: str) -> str:
    """返回数据源的中文名，未知标识回显原值以便排查配置错误。"""
    return ONTHISDAY_SOURCE_NAMES.get(str(source or "").strip().lower(), str(source or ""))


def _strip_html(raw: Any) -> str:
    """去掉百科文本里的标签与实体，压平空白后返回纯文本。"""
    text = re.sub(r"<[^>]*>", "", str(raw or ""))
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_year(raw: Any) -> Optional[int]:
    """把年份解析为整数，「前221」等公元前写法返回负数，无法解析返回空。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    negative = text.startswith("前") or text.startswith("-")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    return -int(digits) if negative else int(digits)


def _normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按年份倒序裁剪候选池，无年份的条目排在最后。"""
    items.sort(key=lambda item: (item["year"] is None, -(item["year"] or 0)))
    return items[:ONTHISDAY_POOL_SIZE]


def _fetch_baidu(month: int, day: int) -> List[Dict[str, Any]]:
    """请求百度百科整月数据并取出当日事件。

    百度按月返回，键为 `"08"` 与 `"0820"` 两级；`title` 含词条超链接标签，必须
    剥成纯文本后再交给展示层。整月响应约 400 KB，命中缓存后一天只请求一次。
    """
    data = _default_fetcher(
        BAIDU_ONTHISDAY_URL.format(month=month), ONTHISDAY_TIMEOUT_SEC
    )
    month_key = f"{month:02d}"
    day_key = f"{month:02d}{day:02d}"
    events = ((data or {}).get(month_key) or {}).get(day_key) or []
    items: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        text = _strip_html(event.get("title"))
        if text:
            items.append({
                "year": _parse_year(event.get("year")),
                "text": text,
                "type": str(event.get("type") or ""),
            })
    return _normalize_items(items)


def _fetch_sixtys(month: int, day: int) -> List[Dict[str, Any]]:
    """请求 60s 接口；它是百度数据的开源封装，字段已清洗。"""
    data = _default_fetcher(
        SIXTYS_ONTHISDAY_URL.format(month=month, day=day), ONTHISDAY_TIMEOUT_SEC
    )
    payload = (data or {}).get("data") or {}
    if int(payload.get("month") or 0) != month or int(payload.get("day") or 0) != day:
        raise ValueError("返回日期与请求日期不一致")
    items: List[Dict[str, Any]] = []
    for event in payload.get("items") or []:
        if not isinstance(event, dict):
            continue
        text = _strip_html(event.get("title"))
        if text:
            items.append({
                "year": _parse_year(event.get("year")),
                "text": text,
                "type": str(event.get("event_type") or ""),
            })
    return _normalize_items(items)


def _fetch_wikipedia(month: int, day: int) -> List[Dict[str, Any]]:
    """请求维基百科历史事件接口并转成简体候选。"""
    data = _default_fetcher(
        WIKI_ONTHISDAY_URL.format(month=month, day=day), ONTHISDAY_TIMEOUT_SEC
    )
    items: List[Dict[str, Any]] = []
    for event in (data or {}).get("events") or []:
        text = _to_simplified(_strip_html(event.get("text")))
        if text:
            items.append({
                "year": _parse_year(event.get("year")),
                "text": text,
                "type": "",
            })
    return _normalize_items(items)


_FETCHERS = {
    "baidu": _fetch_baidu,
    "60s": _fetch_sixtys,
    "wikipedia": _fetch_wikipedia,
}


def normalize_source(source: Any) -> str:
    """把数据源配置规范为已支持的标识，非法值回退百度百科并告警。"""
    candidate = str(source or "").strip().lower()
    if candidate in _FETCHERS:
        return candidate
    if candidate:
        print(
            f"[WARN] ONTHISDAY_SOURCE={candidate!r} 非法，"
            f"回退{ONTHISDAY_SOURCE_NAMES[DEFAULT_ONTHISDAY_SOURCE]}"
        )
    return DEFAULT_ONTHISDAY_SOURCE


def _fetch_onthisday(month: int, day: int, source: str) -> List[Dict[str, Any]]:
    """按数据源取当日历史事件候选池，返回按年份倒序的纯文本条目。"""
    return _FETCHERS[normalize_source(source)](month, day)


def _is_negative(text: str) -> bool:
    """判断历史事件文本是否命中家庭场景不宜展示的负面关键词。"""
    return any(word in text for word in NEGATIVE_KEYWORDS)


def _is_negative_item(item: Dict[str, Any]) -> bool:
    """按事件类型与文本关键词共同判断条目是否不宜展示。"""
    if str(item.get("type") or "").strip().lower() in NEGATIVE_EVENT_TYPES:
        return True
    return _is_negative(item.get("text") or "")


def _score_item(item: Dict[str, Any], this_year: int, min_year: int) -> float:
    """按本次最小年份、周年与文本长度给候选事件评分。"""
    year = item.get("year") or 0
    text = item.get("text") or ""
    score = max(0.0, min(10.0, (year - min_year) / 12.0)) if year > 0 else 0.0
    if year > 0:
        anniversary = this_year - year
        if anniversary > 0 and anniversary % 100 == 0:
            score += 6.0
        elif anniversary > 0 and anniversary % 50 == 0:
            score += 4.0
        elif anniversary > 0 and anniversary % 10 == 0:
            score += 3.0
    if 20 <= len(text) <= 60:
        score += 2.0
    elif len(text) > 90:
        score -= 2.0
    return score


def _curated_select(
    items: List[Dict[str, Any]], today: dt.date, count: int, min_year: int
) -> List[Dict[str, Any]]:
    """按本次条数和年份过滤负面事件并评分选择。"""
    pool = [
        item for item in items
        if (item.get("year") or 0) >= min_year and not _is_negative_item(item)
    ]
    if len(pool) < count:
        relaxed = [item for item in items if not _is_negative_item(item)]
        pool = relaxed if len(relaxed) >= count else items
    return sorted(
        pool, key=lambda item: -_score_item(item, today.year, min_year)
    )[:count]


AI_SYSTEM_PROMPT = """你在为一个家用电子相框挑选「历史上的今天」条目。相框摆在家里，旁边轮播的是主人的家庭照片。

从候选事件中挑选最适合展示的若干条，并把文字改写得更易读。

挑选标准，按优先级：
1. 避开灾难、事故、战争、死亡、袭击、疾病等沉重内容
2. 优先科技突破、探索发现、文化艺术、体育纪录、建筑落成、有趣的历史瞬间
3. 优先普通人能有共鸣或会觉得有意思的
4. 优先近现代

每条 25~40 字，只能基于原文改写，保留关键人名、地名、数字。
只输出 JSON：{"picks": [{"year": 年份数字, "text": "改写后的文字"}]}"""


def _call_ai(
    pool: List[Dict[str, Any]],
    count: int,
    panel_ai_model: str,
    api_url: str,
    api_key: str,
    model_name: str,
) -> List[Dict[str, Any]]:
    """用本次显式接口与模型配置筛选事件，失败时抛异常供上层回退。"""
    actual_model = panel_ai_model or model_name
    if not api_url or not actual_model:
        raise RuntimeError(
            "没有可用的模型厂商：请在后台「模型厂商」页建档，"
            "并把 PANEL_PROVIDER 或 ANALYSIS_PROVIDER 指向该档案名称"
        )
    candidates = pool[:20]
    user_content = (
        f"请从下面 {len(candidates)} 条候选事件中挑选 {count} 条，格式为「年份|事件」：\n\n"
        + "\n".join(f"{item.get('year')}|{item.get('text')}" for item in candidates)
    )
    payload = {
        "model": actual_model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=AI_TIMEOUT_SEC) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
    parsed = json.loads(content)
    picks = parsed.get("picks") if isinstance(parsed, dict) else parsed
    if not isinstance(picks, list):
        raise ValueError("返回结构不含 picks 列表")
    by_year = {
        item["year"]: item.get("text") or ""
        for item in candidates if isinstance(item.get("year"), int)
    }
    output: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        try:
            year = int(pick.get("year"))
        except (TypeError, ValueError):
            continue
        text = str(pick.get("text") or "").strip()
        if year not in by_year or not text or year in seen:
            continue
        seen.add(year)
        output.append({"year": year, "text": text, "raw_text": by_year[year], "ai": True})
        if len(output) >= count:
            break
    if not output:
        raise ValueError("校验后无有效条目")
    return output


def _ai_select(
    items: List[Dict[str, Any]],
    today: dt.date,
    count: int,
    min_year: int,
    panel_ai_model: str,
    api_url: str,
    api_key: str,
    model_name: str,
    source: str,
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """逐候选执行人工智能筛选，仅网络白名单错误允许切换厂商。

    Args:
        items: 规则清洗后的历史事件候选池。
        today: 当前面板日期。
        count: 需要选择的条目数。
        min_year: 规则精选的最小年份。
        panel_ai_model: 旧面板模型覆盖值。
        api_url: 旧单厂商接口地址。
        api_key: 旧单厂商运行时密钥。
        model_name: 旧单厂商模型名。
        source: 历史事件数据源。
        provider_chain: 含运行时密钥和已解析接口地址的有序候选链。

    Returns:
        人工智能筛选结果；不可降级错误或整链耗尽时返回规则精选。
    """
    candidates = list(provider_chain or ({
        "name": "legacy", "api_url": api_url,
        "api_key": api_key, "model_name": model_name,
    },))
    now = time.time()
    for index, candidate in enumerate(candidates):
        candidate_url = str(candidate.get("api_url") or candidate.get("base_url") or "")
        candidate_model = str(candidate.get("model_name") or "")
        actual_model = panel_ai_model or candidate_model
        key = (
            f"ai:{source}:{today.month:02d}-{today.day:02d}:{count}:"
            f"{candidate_url}:{actual_model}"
        )
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit["ts"] < ONTHISDAY_CACHE_TTL_SEC:
            return hit["items"]
        try:
            picked = _call_ai(
                items,
                count,
                panel_ai_model,
                candidate_url,
                str(candidate.get("api_key") or ""),
                candidate_model,
            )
        except Exception as error:
            reason = fallback_reason(error)
            if reason is not None and index + 1 < len(candidates):
                following = candidates[index + 1]
                LOGGER.warning(
                    "Panel provider fallback, purpose=[panel], from_provider=[%s], "
                    "to_provider=[%s], reason=[%s]",
                    candidate.get("name"), following.get("name"), reason,
                )
                continue
            LOGGER.warning(
                "Panel AI selection failed, using curated, purpose=[panel], provider=[%s]",
                candidate.get("name"),
            )
            return _curated_select(items, today, count, min_year)
        with _cache_lock:
            _cache[key] = {"ts": now, "items": picked}
        return picked
    return _curated_select(items, today, count, min_year)


def _select_items(
    items: List[Dict[str, Any]],
    today: dt.date,
    count: int,
    strategy: str,
    min_year: int,
    panel_ai_model: str,
    api_url: str,
    api_key: str,
    model_name: str,
    source: str,
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """按本次完整配置从候选池选择最终展示条目。"""
    effective_count = max(1, int(count))
    effective_strategy = (strategy or "curated").lower()
    if not items:
        return []
    if effective_strategy == "recent":
        return items[:effective_count]
    if effective_strategy == "ai":
        return _ai_select(
            items, today, effective_count, min_year, panel_ai_model,
            api_url, api_key, model_name, source, provider_chain,
        )
    return _curated_select(items, today, effective_count, min_year)


def get_onthisday(
    today: Optional[dt.date] = None,
    force: bool = False,
    count: Optional[int] = None,
    strategy: Optional[str] = None,
    min_year: Optional[int] = None,
    panel_ai_model: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """按本次显式配置和可选厂商链获取历史事件，省略参数保持旧行为。"""
    day = today or dt.date.today()
    effective_count = ONTHISDAY_COUNT if count is None else count
    effective_strategy = ONTHISDAY_STRATEGY if strategy is None else strategy
    effective_min_year = ONTHISDAY_MIN_YEAR if min_year is None else min_year
    effective_panel_model = os.environ.get("PANEL_AI_MODEL", "") if panel_ai_model is None else panel_ai_model
    # 接口地址、密钥与模型名不再从环境变量兜底：它们只能来自厂商档案，调用方解析不出
    # 候选时就该传空值，让人工智能筛选按「模型不可用」回退规则精选。留着环境变量兜底
    # 等于在移除注册表兜底之后又开一个后门，「改了档案却没生效」会重新出现。
    effective_api_url = "" if api_url is None else api_url
    effective_api_key = "" if api_key is None else api_key
    effective_model_name = "" if model_name is None else model_name
    effective_source = normalize_source(ONTHISDAY_SOURCE if source is None else source)
    # 缓存键含数据源：切换数据源后不会继续命中上一个源的候选池。
    key = f"onthisday:{effective_source}:{day.month:02d}-{day.day:02d}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)

    def result(pool: List[Dict[str, Any]], timestamp: float, cached: bool, **extra: Any) -> Dict[str, Any]:
        """用本次配置筛选候选池并构造不含密钥的公开结果。"""
        selected = _select_items(
            pool, day, effective_count, effective_strategy, effective_min_year,
            effective_panel_model, effective_api_url, effective_api_key,
            effective_model_name, effective_source, provider_chain,
        )
        return {
            "available": bool(selected), "items": selected, "pool_size": len(pool),
            "strategy": effective_strategy, "cached": cached,
            "updated_at": timestamp, "source": effective_source,
            "source_name": source_name(effective_source), **extra,
        }

    if hit and not force and now - hit["ts"] < ONTHISDAY_CACHE_TTL_SEC:
        return result(hit["items"], hit["ts"], True)
    try:
        pool = _fetch_onthisday(day.month, day.day, effective_source)
        with _cache_lock:
            _cache[key] = {"ts": now, "items": pool}
        return result(pool, now, False)
    except Exception as error:
        if hit:
            return result(hit["items"], hit["ts"], True, stale=True, error=str(error))
        return {
            "available": False, "items": [], "pool_size": 0,
            "strategy": effective_strategy, "error": str(error),
            "source": effective_source, "source_name": source_name(effective_source),
        }


def reset_cache() -> None:
    """清空候选池与人工智能筛选缓存，供测试与手动强刷使用。"""
    with _cache_lock:
        _cache.clear()


def configure(
    count: Optional[int] = None,
    strategy: Optional[str] = None,
    min_year: Optional[int] = None,
    source: Optional[str] = None,
) -> None:
    """更新旧直接调用使用的默认值；Web 请求使用显式独立参数。"""
    global ONTHISDAY_COUNT, ONTHISDAY_STRATEGY, ONTHISDAY_MIN_YEAR, ONTHISDAY_SOURCE
    if count is not None:
        ONTHISDAY_COUNT = max(1, int(count))
    if strategy is not None:
        normalized = str(strategy).strip().lower()
        if normalized not in ("recent", "curated", "ai"):
            print(f"[WARN] ONTHISDAY_STRATEGY={normalized!r} 非法，回退为 curated")
            normalized = "curated"
        ONTHISDAY_STRATEGY = normalized
    if min_year is not None:
        ONTHISDAY_MIN_YEAR = int(min_year)
    if source is not None:
        ONTHISDAY_SOURCE = normalize_source(source)


def get_panel_data(
    today: Optional[dt.date] = None,
    force: bool = False,
    count: Optional[int] = None,
    strategy: Optional[str] = None,
    min_year: Optional[int] = None,
    panel_ai_model: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    provider_chain: Sequence[Mapping[str, Any]] | None = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """显式传递完整配置和可选厂商链，聚合可独立降级的面板数据。"""
    day = today or dt.date.today()
    return {
        "date": get_date_info(day),
        "lunar": get_lunar_info(day),
        "onthisday": get_onthisday(
            day, force, count, strategy, min_year, panel_ai_model,
            api_url, api_key, model_name, provider_chain, source,
        ),
        "generated_at": time.time(),
    }
