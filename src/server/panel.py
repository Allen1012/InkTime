#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""展示页日期、农历与历史事件信息面板的数据提供层。"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from lunar_python import Solar
except ImportError:
    Solar = None

try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None

WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
WIKI_ONTHISDAY_URL = "https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/events/{month:02d}/{day:02d}"
WIKI_TIMEOUT_SEC = 8
WIKI_CACHE_TTL_SEC = 24 * 3600
ONTHISDAY_POOL_SIZE = 30
ONTHISDAY_COUNT = 2
ONTHISDAY_STRATEGY = "curated"
ONTHISDAY_MIN_YEAR = 1900
AI_TIMEOUT_SEC = 20
NEGATIVE_KEYWORDS = [
    "死", "亡", "罹难", "遇难", "伤亡", "受伤", "灾害", "灾难", "地震", "海啸",
    "洪水", "泥石流", "坠毁", "空难", "沉没", "爆炸", "袭击", "屠杀", "战争",
    "战役", "入侵", "轰炸", "枪击", "刺杀", "处决", "起义", "暴动", "骚乱",
    "事故", "失控", "瘟疫", "疫情", "病毒", "饥荒", "绑架", "劫机",
]
_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


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


def _fetch_onthisday(month: int, day: int) -> List[Dict[str, Any]]:
    """请求维基百科历史事件接口并返回按年份倒序的简体候选。"""
    request = urllib.request.Request(
        WIKI_ONTHISDAY_URL.format(month=month, day=day),
        headers={
            "User-Agent": "InkTime/1.0 (personal photo frame)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=WIKI_TIMEOUT_SEC) as response:
        data = json.loads(response.read().decode("utf-8"))
    items: List[Dict[str, Any]] = []
    for event in data.get("events", []):
        text = _to_simplified(str(event.get("text", "")).strip())
        if text:
            items.append({"year": event.get("year"), "text": text})
    items.sort(key=lambda item: (item["year"] is None, -(item["year"] or 0)))
    return items[:ONTHISDAY_POOL_SIZE]


def _is_negative(text: str) -> bool:
    """判断历史事件文本是否命中家庭场景不宜展示的负面关键词。"""
    return any(word in text for word in NEGATIVE_KEYWORDS)


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
        if (item.get("year") or 0) >= min_year
        and not _is_negative(item.get("text") or "")
    ]
    if len(pool) < count:
        relaxed = [item for item in items if not _is_negative(item.get("text") or "")]
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
        raise RuntimeError("未配置 API_URL / PANEL_AI_MODEL")
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
) -> List[Dict[str, Any]]:
    """按实际模型缓存本次人工智能筛选，失败时回退同次规则参数。"""
    actual_model = panel_ai_model or model_name
    key = f"ai:{today.month:02d}-{today.day:02d}:{count}:{actual_model}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit["ts"] < WIKI_CACHE_TTL_SEC:
        return hit["items"]
    try:
        picked = _call_ai(
            items, count, panel_ai_model, api_url, api_key, model_name
        )
        with _cache_lock:
            _cache[key] = {"ts": now, "items": picked}
        return picked
    except Exception as error:
        print(f"[WARN] AI 筛选失败，回退 curated：{error}")
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
            api_url, api_key, model_name,
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
) -> Dict[str, Any]:
    """按本次显式配置获取历史事件；省略参数时兼容旧直接调用默认值。"""
    day = today or dt.date.today()
    effective_count = ONTHISDAY_COUNT if count is None else count
    effective_strategy = ONTHISDAY_STRATEGY if strategy is None else strategy
    effective_min_year = ONTHISDAY_MIN_YEAR if min_year is None else min_year
    effective_panel_model = os.environ.get("PANEL_AI_MODEL", "") if panel_ai_model is None else panel_ai_model
    effective_api_url = os.environ.get("API_URL", "") if api_url is None else api_url
    effective_api_key = os.environ.get("API_KEY", "") if api_key is None else api_key
    effective_model_name = os.environ.get("MODEL_NAME", "") if model_name is None else model_name
    key = f"onthisday:{day.month:02d}-{day.day:02d}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)

    def result(pool: List[Dict[str, Any]], timestamp: float, cached: bool, **extra: Any) -> Dict[str, Any]:
        """用本次配置筛选候选池并构造不含密钥的公开结果。"""
        selected = _select_items(
            pool, day, effective_count, effective_strategy, effective_min_year,
            effective_panel_model, effective_api_url, effective_api_key,
            effective_model_name,
        )
        return {
            "available": bool(selected), "items": selected, "pool_size": len(pool),
            "strategy": effective_strategy, "cached": cached,
            "updated_at": timestamp, "source": "wikipedia", **extra,
        }

    if hit and not force and now - hit["ts"] < WIKI_CACHE_TTL_SEC:
        return result(hit["items"], hit["ts"], True)
    try:
        pool = _fetch_onthisday(day.month, day.day)
        with _cache_lock:
            _cache[key] = {"ts": now, "items": pool}
        return result(pool, now, False)
    except Exception as error:
        if hit:
            return result(hit["items"], hit["ts"], True, stale=True, error=str(error))
        return {
            "available": False, "items": [], "pool_size": 0,
            "strategy": effective_strategy, "error": str(error), "source": "wikipedia",
        }


def configure(
    count: Optional[int] = None,
    strategy: Optional[str] = None,
    min_year: Optional[int] = None,
) -> None:
    """更新旧直接调用使用的默认值；Web 请求使用显式独立参数。"""
    global ONTHISDAY_COUNT, ONTHISDAY_STRATEGY, ONTHISDAY_MIN_YEAR
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
) -> Dict[str, Any]:
    """显式传递本次完整配置并聚合可独立降级的面板数据。"""
    day = today or dt.date.today()
    return {
        "date": get_date_info(day),
        "lunar": get_lunar_info(day),
        "onthisday": get_onthisday(
            day, force, count, strategy, min_year, panel_ai_model,
            api_url, api_key, model_name,
        ),
        "generated_at": time.time(),
    }
