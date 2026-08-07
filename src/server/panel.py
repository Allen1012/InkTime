#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""展示页信息面板的数据提供层。

给 dashboard 模板的右侧信息栏提供数据，目前两块：

- 农历 / 节气 / 干支 / 传统节日：由 lunar-python 本地计算，**完全离线**
- 历史上的今天：取自维基百科官方 feed API（免 key），返回的是繁体，用 zhconv 转简

设计约束：

1. **外部数据一律走服务端**。前端直连有两个问题：跨域被浏览器拦，以及每次
   切换照片都会重新请求。服务端还便于统一缓存。
2. **每块数据独立降级**。任一数据源失败只让对应模块为空，绝不影响整个面板。
   前端据此隐藏该区域。实测常见的免费数据源会静默失效（内容不再更新但仍返回
   200），所以不能假设外部源长期可用。
3. **缓存分层**。农历按天缓存（本来就是本地计算，缓存只为省重复构造）；
   历史上的今天按 月-日 缓存 24 小时。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

# 两个依赖都是纯 Python 包。缺失时对应模块降级，不影响 server 启动。
try:
    from lunar_python import Solar
except ImportError:
    Solar = None

try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None


WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 维基百科官方 feed API：免 key，返回指定月日的历史事件
WIKI_ONTHISDAY_URL = "https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/events/{month:02d}/{day:02d}"
WIKI_TIMEOUT_SEC = 8
WIKI_CACHE_TTL_SEC = 24 * 3600
# 从 API 取回后先保留这么多条作为候选池，再按策略筛选出最终展示的条目
ONTHISDAY_POOL_SIZE = 30

# 展示条数与筛选策略，由 server.py 从环境变量注入
ONTHISDAY_COUNT = 2
# recent   — 纯按年份降序取最近的（最简单，但维基选材偏灾难，实际效果一般）
# curated  — 过滤负面事件 + 限制年份下限 + 周年加分（默认）
# ai       — 交给大模型筛选（需配置 API，失败时自动回退 curated）
ONTHISDAY_STRATEGY = "curated"
# curated 策略的年份下限：太久远的事件普通人共鸣低
ONTHISDAY_MIN_YEAR = 1900
# AI 筛选的超时。前端有 30 分钟轮询兜底，这里不宜设太长以免拖慢首屏
AI_TIMEOUT_SEC = 20

# 负面事件关键词。维基「历史上的今天」选材本身偏重灾难与战争，
# 实测某一天 30 条里前 6 条有 5 条是空难/泥石流/台风致死/炸弹袭击/屠杀。
# 家庭相框场景不适合天天展示这类内容，故默认过滤。
NEGATIVE_KEYWORDS = [
    "死", "亡", "罹难", "遇难", "伤亡", "受伤", "灾害", "灾难", "地震", "海啸",
    "洪水", "泥石流", "坠毁", "空难", "沉没", "爆炸", "袭击", "屠杀", "战争",
    "战役", "入侵", "轰炸", "枪击", "刺杀", "处决", "起义", "暴动", "骚乱",
    "事故", "失控", "瘟疫", "疫情", "病毒", "饥荒", "绑架", "劫机",
]

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _to_simplified(text: str) -> str:
    """繁体转简体。zhconv 缺失时原样返回，不报错。"""
    if not text:
        return ""
    if _zh_convert is None:
        return text
    try:
        return _zh_convert(text, "zh-cn")
    except Exception:
        return text


def get_date_info(today: Optional[dt.date] = None) -> Dict[str, Any]:
    """公历日期信息。纯本地计算。"""
    d = today or dt.date.today()
    return {
        "iso": d.isoformat(),
        "year": d.year,
        "month": d.month,
        "day": d.day,
        "weekday": WEEKDAY_CN[d.weekday()],
        "day_of_year": d.timetuple().tm_yday,
    }


def get_lunar_info(today: Optional[dt.date] = None) -> Dict[str, Any]:
    """农历 / 节气 / 干支 / 传统节日。完全离线。

    lunar-python 缺失时返回 available=False，前端隐藏该区域。
    """
    d = today or dt.date.today()
    if Solar is None:
        return {"available": False, "error": "未安装 lunar-python"}

    try:
        solar = Solar.fromYmd(d.year, d.month, d.day)
        lunar = solar.getLunar()

        # 当天正好是节气时，getNextJieQi() 返回的仍是当天，需要往后跳一天再取，
        # 否则「下一个节气」会显示成今天。
        jieqi_today = lunar.getJieQi() or ""
        nxt = lunar.getNextJieQi()
        if nxt is not None and nxt.getSolar().toYmd() == d.isoformat():
            probe = Solar.fromYmd(d.year, d.month, d.day)
            nxt2 = probe.getLunar().getNextJieQi(True)
            # getNextJieQi(True) 表示跳过当天；部分版本行为不同，再兜一层
            if nxt2 is not None and nxt2.getSolar().toYmd() != d.isoformat():
                nxt = nxt2
            else:
                tomorrow = d + dt.timedelta(days=1)
                nxt = Solar.fromYmd(tomorrow.year, tomorrow.month, tomorrow.day) \
                    .getLunar().getNextJieQi()

        next_jieqi = None
        if nxt is not None:
            nd = dt.date.fromisoformat(nxt.getSolar().toYmd())
            next_jieqi = {
                "name": nxt.getName(),
                "date": nd.isoformat(),
                "days_left": (nd - d).days,
            }

        return {
            "available": True,
            # 例：丙午年 六月廿五
            "text": f"{lunar.getYearInGanZhi()}年 {lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
            "month_cn": lunar.getMonthInChinese(),
            "day_cn": lunar.getDayInChinese(),
            "ganzhi_year": lunar.getYearInGanZhi(),
            "shengxiao": lunar.getYearShengXiao(),
            "jieqi": jieqi_today,
            "next_jieqi": next_jieqi,
            "festivals": list(lunar.getFestivals() or []),
        }
    except Exception as e:
        return {"available": False, "error": f"农历计算失败: {e}"}


def _fetch_onthisday(month: int, day: int) -> List[Dict[str, Any]]:
    """请求维基 API 并转成简体。失败时抛异常，由调用方降级。"""
    url = WIKI_ONTHISDAY_URL.format(month=month, day=day)
    req = urllib.request.Request(
        url,
        headers={
            # 维基要求带 User-Agent，缺失会被拒
            "User-Agent": "InkTime/1.0 (personal photo frame)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=WIKI_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    items: List[Dict[str, Any]] = []
    for ev in data.get("events", []):
        year = ev.get("year")
        text = _to_simplified(str(ev.get("text", "")).strip())
        if not text:
            continue
        items.append({"year": year, "text": text})

    # 按年份降序，近的事件更有共鸣
    items.sort(key=lambda x: (x["year"] is None, -(x["year"] or 0)))
    return items[:ONTHISDAY_POOL_SIZE]


def _is_negative(text: str) -> bool:
    """是否命中负面事件关键词。"""
    return any(w in text for w in NEGATIVE_KEYWORDS)


def _score_item(item: Dict[str, Any], this_year: int) -> float:
    """给候选事件打分，分数越高越优先展示。

    维基 API 没有提供任何「重要度」或「热度」字段（实测 pages 关联条目数
    全是 3-4、配图数全是 2-3，区分度太低），所以只能用这几个启发式信号。
    """
    year = item.get("year") or 0
    text = item.get("text") or ""
    score = 0.0

    # 年份新近度：越近越有共鸣，归一到 0~10 分
    if year > 0:
        score += max(0.0, min(10.0, (year - ONTHISDAY_MIN_YEAR) / 12.0))

    # 整十周年加分，整百再加，这类日子有纪念感
    if year > 0:
        anniv = this_year - year
        if anniv > 0 and anniv % 100 == 0:
            score += 6.0
        elif anniv > 0 and anniv % 50 == 0:
            score += 4.0
        elif anniv > 0 and anniv % 10 == 0:
            score += 3.0

    # 长度适中：太短信息量不足，太长在窄栏里显示不下
    n = len(text)
    if 20 <= n <= 60:
        score += 2.0
    elif n > 90:
        score -= 2.0

    return score


def _curated_select(items: List[Dict[str, Any]], today: dt.date, count: int) -> List[Dict[str, Any]]:
    """规则筛选：过滤负面事件 + 年份下限 + 打分排序。"""
    pool = [
        it for it in items
        if (it.get("year") or 0) >= ONTHISDAY_MIN_YEAR and not _is_negative(it.get("text") or "")
    ]

    # 粗筛后不足时逐级放宽，保证总能凑出条目
    if len(pool) < count:
        relaxed = [it for it in items if not _is_negative(it.get("text") or "")]
        pool = relaxed if len(relaxed) >= count else items

    pool = sorted(pool, key=lambda it: -_score_item(it, today.year))
    return pool[:count]


# ---------------- AI 筛选 ----------------

AI_SYSTEM_PROMPT = """你在为一个家用电子相框挑选「历史上的今天」条目。相框摆在家里，旁边轮播的是主人的家庭照片。

从候选事件中挑选最适合展示的若干条，并把文字改写得更易读。

挑选标准，按优先级：
1. 避开灾难、事故、战争、死亡、袭击、疾病等沉重内容 —— 这是硬性要求，家庭场景不适合
2. 优先科技突破、探索发现、文化艺术、体育纪录、建筑落成、有趣的历史瞬间
3. 优先普通人能有共鸣或会觉得有意思的
4. 优先近现代，年代太久远的事件共鸣低

改写要求：
1. 每条 25~40 字，通顺自然的中文，去掉百科腔和冗长的从句
2. 只能基于原文改写，**严禁添加原文中没有的任何信息**，不确定就少写
3. 保留关键的人名、地名、数字
4. 不要以「今天」「这一天」开头

只输出 JSON，不要用 markdown 代码块包裹，格式：
{"picks": [{"year": 年份数字, "text": "改写后的文字"}]}"""


def _call_ai(pool: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """请求大模型筛选并改写。失败时抛异常，由调用方回退规则策略。"""
    api_url = os.environ.get("API_URL") or ""
    api_key = os.environ.get("API_KEY") or ""
    model = os.environ.get("PANEL_AI_MODEL") or os.environ.get("MODEL_NAME") or ""
    if not api_url or not model:
        raise RuntimeError("未配置 API_URL / PANEL_AI_MODEL")

    # 候选太多会浪费 token，取前 20 条（已按年份降序）
    cand = pool[:20]
    lines = [f"{it.get('year')}|{it.get('text')}" for it in cand]
    user = (
        f"请从下面 {len(cand)} 条候选事件中挑选 {count} 条，格式为「年份|事件」：\n\n"
        + "\n".join(lines)
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=AI_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = content.strip()
    # 模型有时仍会套 markdown 代码块，剥掉
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()

    parsed = json.loads(content)
    picks = parsed.get("picks") if isinstance(parsed, dict) else parsed
    if not isinstance(picks, list):
        raise ValueError("返回结构不含 picks 列表")

    # 防幻觉：年份必须存在于候选池中，改写文本不能为空；
    # 同时保留原文，便于核对模型是否改错事实
    by_year: Dict[int, str] = {}
    for it in cand:
        y = it.get("year")
        if isinstance(y, int) and y not in by_year:
            by_year[y] = it.get("text") or ""

    out: List[Dict[str, Any]] = []
    seen = set()
    for p in picks:
        if not isinstance(p, dict):
            continue
        try:
            year = int(p.get("year"))
        except (TypeError, ValueError):
            continue
        text = str(p.get("text") or "").strip()
        if year not in by_year or not text or year in seen:
            continue
        seen.add(year)
        out.append({"year": year, "text": text, "raw_text": by_year[year], "ai": True})
        if len(out) >= count:
            break

    if not out:
        raise ValueError("校验后无有效条目（年份不在候选池或文本为空）")
    return out


def _ai_select(items: List[Dict[str, Any]], today: dt.date, count: int) -> List[Dict[str, Any]]:
    """AI 筛选，带结果缓存与回退。

    结果按 月-日 + 条数 缓存 24 小时。前端每 30 分钟轮询一次面板，
    不缓存的话一天会调用几十次，缓存后每天只需 1 次。
    """
    key = f"ai:{today.month:02d}-{today.day:02d}:{count}"
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit["ts"] < WIKI_CACHE_TTL_SEC:
        return hit["items"]

    try:
        picked = _call_ai(items, count)
        with _cache_lock:
            _cache[key] = {"ts": now, "items": picked}
        return picked
    except Exception as e:
        # 回退规则策略。内网 API 在阶段二迁到 NAS 后不可达，这条路径是常态而非异常
        print(f"[WARN] AI 筛选失败，回退 curated：{e}")
        return _curated_select(items, today, count)


def _select_items(items: List[Dict[str, Any]], today: dt.date) -> List[Dict[str, Any]]:
    """按配置的策略从候选池里挑出最终展示的条目。"""
    count = max(1, ONTHISDAY_COUNT)
    strategy = (ONTHISDAY_STRATEGY or "curated").lower()

    if not items:
        return []

    if strategy == "recent":
        # 纯按年份降序（候选池已排好序）
        return items[:count]

    if strategy == "ai":
        return _ai_select(items, today, count)

    return _curated_select(items, today, count)


def get_onthisday(today: Optional[dt.date] = None, force: bool = False) -> Dict[str, Any]:
    """历史上的今天。

    缓存的是原始候选池（按 月-日 缓存 24 小时），筛选在每次返回时做，
    这样调整策略或展示条数不需要重新请求网络。
    失败时优先返回过期缓存，其次返回空列表。
    """
    d = today or dt.date.today()
    key = f"onthisday:{d.month:02d}-{d.day:02d}"
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)

    def _ok(pool: List[Dict[str, Any]], ts: float, cached: bool, **extra) -> Dict[str, Any]:
        selected = _select_items(pool, d)
        return {
            "available": bool(selected),
            "items": selected,
            "pool_size": len(pool),
            "strategy": ONTHISDAY_STRATEGY,
            "cached": cached,
            "updated_at": ts,
            "source": "wikipedia",
            **extra,
        }

    if hit and not force and now - hit["ts"] < WIKI_CACHE_TTL_SEC:
        return _ok(hit["items"], hit["ts"], True)

    try:
        pool = _fetch_onthisday(d.month, d.day)
        with _cache_lock:
            _cache[key] = {"ts": now, "items": pool}
        return _ok(pool, now, False)
    except Exception as e:
        # 降级：宁可用过期数据，也不让整块空掉
        if hit:
            return _ok(hit["items"], hit["ts"], True, stale=True, error=str(e))
        return {"available": False, "items": [], "pool_size": 0,
                "strategy": ONTHISDAY_STRATEGY, "error": str(e),
                "source": "wikipedia"}


def configure(count: Optional[int] = None, strategy: Optional[str] = None,
              min_year: Optional[int] = None) -> None:
    """由 server.py 注入配置。

    不在本模块直接读环境变量，是为了让它能脱离 Flask 单独测试。
    """
    global ONTHISDAY_COUNT, ONTHISDAY_STRATEGY, ONTHISDAY_MIN_YEAR
    if count is not None:
        ONTHISDAY_COUNT = max(1, int(count))
    if strategy is not None:
        s = str(strategy).strip().lower()
        if s not in ("recent", "curated", "ai"):
            print(f"[WARN] ONTHISDAY_STRATEGY={s!r} 非法，回退为 curated。可选：('recent', 'curated', 'ai')")
            s = "curated"
        ONTHISDAY_STRATEGY = s
    if min_year is not None:
        ONTHISDAY_MIN_YEAR = int(min_year)


def get_panel_data(today: Optional[dt.date] = None, force: bool = False) -> Dict[str, Any]:
    """聚合信息面板所需的全部数据。

    每块独立取值并独立降级，任一块失败不影响其余部分。
    """
    d = today or dt.date.today()
    return {
        "date": get_date_info(d),
        "lunar": get_lunar_info(d),
        "onthisday": get_onthisday(d, force=force),
        "generated_at": time.time(),
    }
