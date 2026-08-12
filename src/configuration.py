"""独立于 Flask 的统一配置注册、读取、校验与持久化服务。"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.database import database_connection, write_transaction

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SettingDefinition:
    """描述一个配置项的类型、来源策略、管理权限和生效范围。"""

    key: str
    name: str
    group: str
    value_type: str
    default: Any
    description: str
    editable: bool = False
    sensitive: bool = False
    restart_required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] = ()
    scopes: tuple[str, ...] = ()
    validator: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class ConfigurationActor:
    """保存配置修改人的编号和用户名快照。"""

    user_id: int | None
    username: str


@dataclass(frozen=True)
class SettingsState:
    """保存数据库中的全局配置版本和可编辑配置覆盖值。"""

    version: int
    values: dict[str, Any]


class ConfigurationValidationError(ValueError):
    """表示一批配置包含未知、只读、敏感或类型范围错误。"""

    def __init__(self, errors: Mapping[str, str]) -> None:
        """保存逐配置错误，调用方可在不泄露值的前提下展示。"""
        self.errors = dict(errors)
        super().__init__("配置校验失败")


class ConfigurationConflictError(RuntimeError):
    """表示提交基于过期的全局配置版本。"""

    def __init__(self, expected_version: int, current_version: int) -> None:
        """保存请求版本和数据库当前版本，便于调用方要求用户刷新。"""
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"配置版本冲突: expected={expected_version}, current={current_version}"
        )


def _setting(
    key: str,
    name: str,
    group: str,
    value_type: str,
    default: Any,
    description: str,
    **options: Any,
) -> SettingDefinition:
    """用紧凑参数创建不可变配置定义。"""
    return SettingDefinition(
        key, name, group, value_type, default, description, **options
    )


def _validate_image_dirs(value: Any) -> None:
    """校验照片目录配置可安全用于扫描、上传与回收站隔离。

    写入配置时使用：要求分号分隔的每个目录都存在、是目录、可读，且互不嵌套。
    实现位于本模块下方的 `parse_image_dirs`，调用发生在运行期，不影响导入顺序。

    Args:
        value: 待写入的照片目录配置值。

    Raises:
        ValueError: 配置为空、含文件系统根目录、目录互相嵌套或目录不可用。
    """
    parse_image_dirs(value, base_dir=PROJECT_ROOT, require_existing=True)


def _validate_time_windows(value: Any) -> None:
    """校验展示页生效时间段可被正确解析。

    实现位于本模块下方的 `parse_time_windows`，调用发生在运行期，不影响导入顺序。

    Args:
        value: 待写入的时间段配置值。

    Raises:
        ValueError: 时间或星期写法非法、区间零长度。
    """
    parse_time_windows(value)


_SETTING_DEFINITIONS = (
    _setting("APP_ENV", "运行环境", "system", "string", "development", "应用运行环境。", choices=("development", "testing", "production")),
    _setting("PROJECT_NAME", "项目名称", "system", "string", "InkTime 相册", "网站显示名称。", editable=True, restart_required=False),
    _setting("DB_PATH", "数据库路径", "system", "string", "./data/photos.db", "SQLite 数据库路径。", scopes=("analysis", "render", "worker", "web")),
    _setting("IMAGE_DIR", "照片目录", "system", "string", "./data/photos", "照片扫描与上传目录，多个目录用分号分隔，第一个是上传写入的主目录。", editable=True, restart_required=False, validator=_validate_image_dirs, scopes=("analysis", "render", "worker", "web")),
    _setting("BIN_OUTPUT_DIR", "渲染输出目录", "system", "string", "./data/output", "墨水屏渲染产物目录。", scopes=("render", "worker", "web")),
    _setting("FLASK_HOST", "Web 监听地址", "system", "string", "0.0.0.0", "Web 服务监听地址。", scopes=("web",)),
    _setting("FLASK_PORT", "Web 监听端口", "system", "integer", 5005, "Web 服务监听端口。", minimum=1, maximum=65535, scopes=("web",)),
    _setting("SECRET_KEY", "会话签名密钥", "security", "string", "", "Flask 会话签名密钥。", sensitive=True, scopes=("web",)),
    _setting("API_KEY", "模型接口密钥", "security", "string", "", "视觉语言模型接口密钥。留空提交表示保持原值不变。", editable=True, restart_required=False, sensitive=True, scopes=("analysis", "worker")),
    _setting("DOWNLOAD_KEY", "设备下载密钥", "security", "string", "", "墨水屏设备下载路径密钥。", sensitive=True, scopes=("web",)),
    _setting("SESSION_COOKIE_HTTPONLY", "会话禁止脚本读取", "security", "boolean", True, "禁止浏览器脚本读取会话 Cookie。", scopes=("web",)),
    _setting("SESSION_COOKIE_SAMESITE", "会话同站策略", "security", "string", "Lax", "会话 Cookie 的 SameSite 策略。", choices=("Lax", "Strict", "None"), scopes=("web",)),
    _setting("SESSION_COOKIE_SECURE", "会话仅安全传输", "security", "boolean", False, "是否只通过 HTTPS 发送会话 Cookie。", scopes=("web",)),
    _setting("PERMANENT_SESSION_LIFETIME", "会话有效秒数", "security", "integer", 28800, "管理员永久会话有效期。", minimum=1, scopes=("web",)),
    _setting("WTF_CSRF_TIME_LIMIT", "跨站请求伪造令牌有效秒数", "security", "integer", 3600, "表单令牌有效期。", minimum=1, scopes=("web",)),
    _setting("ADMIN_LOGIN_MAX_FAILURES", "登录失败上限", "security", "integer", 5, "登录限流窗口内允许的失败次数。", minimum=1, scopes=("web",)),
    _setting("ADMIN_LOGIN_FAILURE_WINDOW_SECONDS", "登录失败窗口秒数", "security", "integer", 300, "管理员登录失败限流窗口。", minimum=1, scopes=("web",)),
    _setting("API_URL", "模型接口地址", "analysis", "string", "http://127.0.0.1:1234/v1/chat/completions", "OpenAI 兼容模型接口地址。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("MODEL_NAME", "分析模型", "analysis", "string", "qwen3-vl-32b-instruct", "照片分析使用的视觉语言模型。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("TIMEOUT", "模型请求超时秒数", "analysis", "integer", 600, "模型请求超时时间。", editable=True, restart_required=False, minimum=1, scopes=("analysis", "worker")),
    _setting("VLM_MAX_LONG_EDGE", "模型图片最长边", "analysis", "integer", 2560, "发送给视觉语言模型的图片最长边像素。", editable=True, restart_required=False, minimum=256, maximum=8192, scopes=("analysis", "worker")),
    _setting("WORLD_CITIES_CSV", "城市索引路径", "analysis", "string", "./data/world_cities_zh.csv", "离线中文城市索引文件。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("CITY_GRID_DEG", "城市网格精度", "analysis", "float", 1.0, "城市候选网格精度。", editable=True, restart_required=False, minimum=0.01, maximum=10, scopes=("analysis", "worker")),
    _setting("CITY_MAX_DISTANCE_KM", "城市匹配最大距离", "analysis", "float", 100.0, "坐标与城市的最大匹配距离。", editable=True, restart_required=False, minimum=0, maximum=20000, scopes=("analysis", "worker")),
    _setting("HOME_LAT", "常驻地纬度", "analysis", "float", 22.543096, "常驻地纬度。", editable=True, restart_required=False, minimum=-90, maximum=90, scopes=("analysis", "worker")),
    _setting("HOME_LON", "常驻地经度", "analysis", "float", 114.057865, "常驻地经度。", editable=True, restart_required=False, minimum=-180, maximum=180, scopes=("analysis", "worker")),
    _setting("HOME_RADIUS_KM", "常驻地半径", "analysis", "float", 60.0, "常驻地判断半径。", editable=True, restart_required=False, minimum=0, maximum=20000, scopes=("analysis", "worker")),
    _setting("FONT_PATH", "字体路径", "render", "string", "", "图片渲染使用的中文字体文件。", editable=True, restart_required=False, scopes=("render", "worker")),
    _setting("DISPLAY_TEMPLATE", "展示页模板", "display", "string", "classic", "展示页布局模板。", editable=True, restart_required=False, choices=("classic", "dashboard"), scopes=("web",)),
    _setting("DISPLAY_ROTATE_MODE", "展示切换模式", "display", "string", "interval", "展示页自动切换模式。", editable=True, restart_required=False, choices=("interval", "hourly", "minutely", "daily", "off"), scopes=("web",)),
    _setting("DISPLAY_ROTATE_INTERVAL_SEC", "展示切换间隔秒数", "display", "integer", 60, "interval 模式的自动切换间隔。", editable=True, restart_required=False, minimum=1, maximum=86400, scopes=("web",)),
    _setting("DISPLAY_KEEP_AWAKE", "展示页保持唤醒", "display", "boolean", True, "是否请求浏览器阻止空闲息屏。", editable=True, restart_required=False, scopes=("web",)),
    _setting("DISPLAY_UI_HIDE_DELAY_SEC", "展示界面隐藏延迟", "display", "integer", 3, "静置后隐藏操作界面的秒数，零表示不隐藏。", editable=True, restart_required=False, minimum=0, maximum=3600, scopes=("web",)),
    _setting("DISPLAY_MIN_SCORE", "展示最低回忆度", "display", "float", 70.0, "展示页候选照片最低回忆度，零表示不限制。", editable=True, restart_required=False, minimum=0, maximum=100, scopes=("web",)),
    _setting("DISPLAY_ACTIVE_WINDOWS", "展示生效时间段", "display", "string", "", "展示页自动轮播的生效时间段，分号分隔，格式 星期@HH:MM-HH:MM，星期可省略表示每天。留空表示全天生效。", editable=True, restart_required=False, validator=_validate_time_windows, scopes=("web",)),
    _setting("DISPLAY_IDLE_MODE", "休息期画面", "display", "string", "freeze", "非生效时间段的画面：freeze 停在最后一张，photo 显示指定照片，rest 显示休息文案。", editable=True, restart_required=False, choices=("freeze", "photo", "rest"), scopes=("web",)),
    _setting("DISPLAY_IDLE_PHOTO_ID", "休息期固定照片编号", "display", "integer", 0, "photo 模式使用的照片编号，零表示未指定；照片不存在或已删除时回退为停在最后一张。", editable=True, restart_required=False, minimum=0, scopes=("web",)),
    _setting("DISPLAY_REST_TEXT", "休息期文案", "display", "string", "休息中", "rest 模式显示的文案。", editable=True, restart_required=False, scopes=("web",)),
    _setting("DISPLAY_NEW_PHOTO_WEIGHT", "新照片展示权重", "display", "float", 3.0, "未展示照片在同轮候选中的权重。", editable=True, restart_required=False, minimum=1, maximum=100, scopes=("web",)),
    _setting("ONTHISDAY_COUNT", "历史上的今天条数", "display", "integer", 2, "信息面板展示的历史事件数量。", editable=True, restart_required=False, minimum=1, maximum=20, scopes=("web",)),
    _setting("ONTHISDAY_STRATEGY", "历史事件筛选策略", "display", "string", "curated", "历史事件筛选策略。", editable=True, restart_required=False, choices=("recent", "curated", "ai"), scopes=("web",)),
    _setting("ONTHISDAY_MIN_YEAR", "历史事件最小年份", "display", "integer", 1900, "curated 策略允许的最早年份。", editable=True, restart_required=False, minimum=1, maximum=9999, scopes=("web",)),
    _setting("PANEL_AI_MODEL", "信息面板模型", "display", "string", "", "人工智能筛选策略使用的模型，空值回退分析模型。", editable=True, restart_required=False, scopes=("web",)),
    _setting("MEMORY_THRESHOLD", "渲染回忆度阈值", "render", "float", 70.0, "每日渲染候选照片最低回忆度。", editable=True, restart_required=False, minimum=0, maximum=100, scopes=("render", "worker")),
    _setting("DAILY_PHOTO_QUANTITY", "每日渲染照片数量", "render", "integer", 5, "每日渲染的照片数量。", editable=True, restart_required=False, minimum=1, maximum=20, scopes=("render", "worker")),
    _setting("FILL_FROM_GLOBAL", "全局照片补足", "render", "boolean", True, "历史同日照片不足时是否从全局高分照片补足。", editable=True, restart_required=False, scopes=("render", "worker")),
    _setting("ENABLE_REVIEW_WEBUI", "产物目录浏览总开关", "system", "boolean", True, "产物目录浏览的第二重开关，需与「启用产物目录浏览」同时为真才开放 /files/。不影响照片墙、分类、搜索与展示页。", editable=True, restart_required=False, scopes=("web",)),
    _setting("ENABLE_FILE_BROWSER", "启用产物目录浏览", "system", "boolean", False, "是否开放产物文件目录浏览。", editable=True, restart_required=False, scopes=("web",)),
    _setting("UPLOAD_MAX_FILES", "单批上传文件数", "worker", "integer", 10, "单批上传允许的最大文件数。", editable=True, restart_required=False, minimum=1, maximum=10, scopes=("web", "worker")),
    _setting("UPLOAD_MAX_BYTES", "单文件上传字节数", "worker", "integer", 20971520, "单个上传文件允许的最大字节数。", editable=True, restart_required=False, minimum=1, maximum=20971520, scopes=("web", "worker")),
    _setting("UPLOAD_MAX_PIXELS", "单图最大像素数", "worker", "integer", 80000000, "上传图片解码后的最大像素数。", editable=True, restart_required=False, minimum=1, maximum=80000000, scopes=("web", "worker")),
    _setting("JOB_MAX_ATTEMPTS", "任务最大尝试次数", "worker", "integer", 3, "后台任务最大执行次数。", editable=True, restart_required=False, minimum=1, maximum=3, scopes=("web", "worker")),
    _setting("JOB_LEASE_SECONDS", "任务租约秒数", "worker", "integer", 120, "后台任务租约时长，实际生效下界为 2 秒。", editable=True, restart_required=False, minimum=1, scopes=("web", "worker")),
    _setting("JOB_RENEW_SECONDS", "任务续租秒数", "worker", "integer", 30, "后台任务续租间隔，必须小于租约时长，否则自动收敛为租约减一秒。", editable=True, restart_required=False, minimum=1, scopes=("web", "worker")),
    _setting("JOB_POLL_SECONDS", "任务轮询秒数", "worker", "float", 2.0, "工作进程空队列轮询间隔。", editable=True, restart_required=False, minimum=0.1, scopes=("worker",)),
    _setting("TRASH_RETENTION_DAYS", "回收站保留天数", "worker", "integer", 30, "回收站过期清理的默认保留天数。", editable=True, restart_required=False, minimum=1, maximum=3650, scopes=("web", "worker")),
)

SETTING_REGISTRY: dict[str, SettingDefinition] = {
    definition.key: definition for definition in _SETTING_DEFINITIONS
}


IMAGE_DIR_SEPARATOR = ";"
TRASH_DIRECTORY_NAME = ".trash"


def parse_image_dirs(
    raw: Any,
    *,
    base_dir: Any | None = None,
    require_existing: bool = False,
) -> tuple[Path, ...]:
    """把分号分隔的照片目录配置解析为有序、去重且互不嵌套的绝对路径。

    分隔符选分号而非冒号，是为了不与 Windows 盘符冲突，也让含空格的路径无需转义。
    值中没有分号时与单目录配置完全等价，因此现有 `.env` 与数据库配置无需改动。
    列表中第一个目录是主目录：上传与临时文件只写主目录，其余目录只读扫描。

    嵌套检测是安全关键校验：若同时配置 `/photos` 与 `/photos/private`，那么
    `/photos/private/.trash/1/x.jpg` 对第一个根来说不在 `/photos/.trash` 下，会被
    当成合法的活动区照片，导致已删除照片通过公开接口泄露。比较在 `resolve()`
    之后进行，因此指向另一个根内部的符号链接同样会被判定为嵌套。

    Args:
        raw: 分号分隔的配置值，可为字符串或路径对象。
        base_dir: 解析相对路径使用的基准目录；为空时按当前工作目录解析。
        require_existing: 是否要求每个目录都存在、是目录且可读。写入配置时必须
            开启；运行期读取时保持关闭，避免网络存储临时不可用直接中断服务。

    Returns:
        按配置顺序去重后的绝对路径元组，至少包含一个元素。

    Raises:
        ValueError: 配置为空、包含文件系统根目录、目录互相嵌套，或在要求存在性
            时目录不存在、不是目录、不可读。
    """
    text = os.fspath(raw) if isinstance(raw, os.PathLike) else str(raw or "")
    base = Path(base_dir) if base_dir is not None else None
    resolved: list[Path] = []
    for segment in text.split(IMAGE_DIR_SEPARATOR):
        candidate = segment.strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        path = path.resolve()
        if path.parent == path:
            raise ValueError("照片目录不能是文件系统根目录")
        if path in resolved:
            continue
        resolved.append(path)
    if not resolved:
        raise ValueError("照片目录不能为空")
    for index, current in enumerate(resolved):
        for other in resolved[index + 1 :]:
            if current.is_relative_to(other) or other.is_relative_to(current):
                raise ValueError(f"照片目录不能互相嵌套: {current} 与 {other}")
    if require_existing:
        for path in resolved:
            if not path.exists():
                raise ValueError(f"照片目录不存在: {path}")
            if not path.is_dir():
                raise ValueError(f"照片目录不是目录: {path}")
            if not os.access(path, os.R_OK | os.X_OK):
                raise ValueError(f"照片目录不可读: {path}")
    return tuple(resolved)


def primary_image_dir(raw: Any, *, base_dir: Any | None = None) -> Path:
    """返回照片目录列表中的主目录，即上传与临时文件的唯一写入位置。"""
    return parse_image_dirs(raw, base_dir=base_dir)[0]


TIME_WINDOW_SEPARATOR = ";"
WEEKDAY_SEPARATOR = "@"
_MINUTES_PER_DAY = 24 * 60
_WEEKDAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_WEEKDAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TimeWindows = tuple[tuple[tuple[int, int], ...], ...]


def _parse_clock(text: str) -> int:
    """把 HH:MM 解析为当日分钟数，允许 24:00 作为结束边界。"""
    parts = text.split(":")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise ValueError(f"时间段格式必须是 HH:MM-HH:MM: {text}")
    hour = int(parts[0])
    minute = int(parts[1])
    if minute > 59:
        raise ValueError(f"分钟必须在 0 到 59 之间: {text}")
    if hour > 24 or (hour == 24 and minute != 0):
        raise ValueError(f"小时必须在 0 到 23 之间，仅允许 24:00 作为结束边界: {text}")
    return hour * 60 + minute


def _parse_weekdays(text: str) -> tuple[int, ...]:
    """把星期前缀解析为周一起算的编号集合，支持范围与列表。"""
    selected: set[int] = set()
    for part in text.split(","):
        token = part.strip().lower()
        if not token:
            raise ValueError(f"星期不能为空: {text}")
        if "-" in token:
            start_name, _, end_name = token.partition("-")
            start = _WEEKDAY_NAMES.get(start_name.strip())
            end = _WEEKDAY_NAMES.get(end_name.strip())
            if start is None or end is None:
                raise ValueError(f"未知的星期写法: {part}")
            # 允许 Fri-Mon 这种跨周末的范围，按周一到周日的循环顺序展开
            index = start
            selected.add(index)
            while index != end:
                index = (index + 1) % 7
                selected.add(index)
            continue
        weekday = _WEEKDAY_NAMES.get(token)
        if weekday is None:
            raise ValueError(f"未知的星期写法: {part}")
        selected.add(weekday)
    return tuple(sorted(selected))


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """把同一天内的区间排序并合并重叠或首尾相接的部分。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
            continue
        merged.append((start, end))
    return tuple(merged)


def parse_time_windows(raw: Any) -> TimeWindows:
    """把展示页生效时间段配置解析为按星期归一化的分钟区间。

    格式为分号分隔的 `星期@HH:MM-HH:MM`，星期前缀可省略，省略即每天生效。星期与
    时间之间用 `@` 分隔而不是冒号，避免与 `HH:MM` 的冒号混淆。跨零点区间会被拆成
    两段，且后半段归属**次日**星期，因此下游判定只需比较当天星期与分钟数，不必再
    处理跨天逻辑。区间为左闭右开，重叠或相邻区间在同一星期内合并。

    Args:
        raw: 配置值，空字符串表示全天生效。

    Returns:
        长度固定为 7 的元组，下标 0 为周一；每项是该天已排序合并的分钟区间元组。
        全部为空表示不限制时间。

    Raises:
        ValueError: 时间或星期写法非法、区间零长度。
    """
    text = str(raw or "")
    buckets: list[list[tuple[int, int]]] = [[] for _ in range(7)]
    for segment in text.split(TIME_WINDOW_SEPARATOR):
        candidate = segment.strip()
        if not candidate:
            continue
        if WEEKDAY_SEPARATOR in candidate:
            weekday_text, _, range_text = candidate.partition(WEEKDAY_SEPARATOR)
            weekdays = _parse_weekdays(weekday_text)
        else:
            weekday_text, range_text = "", candidate
            weekdays = tuple(range(7))
        range_text = range_text.strip()
        if not range_text:
            raise ValueError(f"缺少时间段: {candidate}")
        bounds = range_text.split("-")
        if len(bounds) != 2:
            raise ValueError(f"时间段格式必须是 HH:MM-HH:MM: {candidate}")
        start = _parse_clock(bounds[0].strip())
        end = _parse_clock(bounds[1].strip())
        if start == end:
            raise ValueError(f"时间段不能是零长度: {candidate}")
        for weekday in weekdays:
            if start < end:
                buckets[weekday].append((start, end))
                continue
            # 跨零点：当天保留到 24:00，剩余部分落到次日
            buckets[weekday].append((start, _MINUTES_PER_DAY))
            buckets[(weekday + 1) % 7].append((0, end))
    return tuple(_merge_ranges(ranges) for ranges in buckets)


def is_within_windows(moment: datetime, windows: TimeWindows) -> bool:
    """判断给定时刻是否落在生效时间段内；未配置任何时间段时恒为真。

    Args:
        moment: 待判定时刻，使用与配置同一时钟的本地时间。
        windows: `parse_time_windows()` 的返回值。

    Returns:
        位于任一生效区间内返回 True。
    """
    if not any(windows):
        return True
    minutes = moment.hour * 60 + moment.minute
    return any(start <= minutes < end for start, end in windows[moment.weekday()])


def next_window_start(moment: datetime, windows: TimeWindows) -> datetime | None:
    """返回下一个生效时间段的开始时刻；未配置时间段时返回空。

    最多向后查看 8 天，覆盖「本周剩余全部休息、下周同一天才恢复」的配置。

    Args:
        moment: 起算时刻。
        windows: `parse_time_windows()` 的返回值。

    Returns:
        下一个生效区间的开始时刻，无时间段限制时为 None。
    """
    if not any(windows):
        return None
    base = moment.replace(second=0, microsecond=0)
    minutes = base.hour * 60 + base.minute
    for offset in range(8):
        weekday = (base.weekday() + offset) % 7
        for start, _end in windows[weekday]:
            if offset == 0 and start <= minutes:
                continue
            day = (base + timedelta(days=offset)).replace(hour=0, minute=0)
            return day + timedelta(minutes=start)
    return None


WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _format_clock(minutes: int) -> str:
    """把当日分钟数格式化为 HH:MM。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _describe_weekdays(weekdays: tuple[int, ...]) -> str:
    """把星期编号集合描述为「周一至周五」或「周六、周日」这样的中文。"""
    if len(weekdays) == 7:
        return "每天"
    groups: list[list[int]] = []
    for weekday in weekdays:
        if groups and weekday == groups[-1][-1] + 1:
            groups[-1].append(weekday)
            continue
        groups.append([weekday])
    parts: list[str] = []
    for group in groups:
        if len(group) >= 3:
            parts.append(f"{WEEKDAY_LABELS[group[0]]}至{WEEKDAY_LABELS[group[-1]]}")
            continue
        parts.extend(WEEKDAY_LABELS[weekday] for weekday in group)
    return "、".join(parts)


def describe_time_windows(windows: TimeWindows) -> list[str]:
    """把生效时间段描述成人类可读的中文行，按相同时间安排合并星期。

    配置页用它展示「当前实际生效成什么样」：使用者写下的字符串经过跨零点拆分与区间
    合并后，真正生效的范围可能与直觉不同，直接摊开比让人心算更可靠。

    Args:
        windows: `parse_time_windows()` 的返回值。

    Returns:
        每行一条描述；未配置任何时间段时返回单行「全天生效」。
    """
    if not any(windows):
        return ["全天生效，不限制时间"]
    grouped: dict[tuple[tuple[int, int], ...], list[int]] = {}
    for weekday, ranges in enumerate(windows):
        grouped.setdefault(ranges, []).append(weekday)
    lines: list[str] = []
    resting: list[int] = []
    for ranges, weekdays in grouped.items():
        if not ranges:
            resting.extend(weekdays)
            continue
        spans = "、".join(
            f"{_format_clock(start)} 到 {_format_clock(end)}" for start, end in ranges
        )
        lines.append(f"{_describe_weekdays(tuple(sorted(weekdays)))} {spans}")
    if resting:
        lines.append(f"{_describe_weekdays(tuple(sorted(resting)))} 全天休息")
    return lines


def estimate_daily_rotations(
    windows: TimeWindows, mode: str, interval_seconds: float
) -> dict[int, int] | None:
    """估算每个星期在生效时间段内会切换多少次照片。

    仅对 `hourly` 与 `interval` 两种常用模式给出估算：`minutely` 数量过大无参考意义，
    `daily` 与 `off` 不由时间段决定。估算能提前暴露「区间右开导致少一次」这类边界
    问题，例如 `09:00-22:00` 在整点模式下最后一次是 21:00 而不是 22:00。

    Args:
        windows: `parse_time_windows()` 的返回值。
        mode: 当前 `DISPLAY_ROTATE_MODE`。
        interval_seconds: 当前 `DISPLAY_ROTATE_INTERVAL_SEC`。

    Returns:
        星期编号到次数的映射；模式不适用时返回 None。
    """
    normalized = str(mode or "").strip().lower()
    if normalized not in {"hourly", "interval"}:
        return None
    effective = tuple(windows) if any(windows) else tuple(((0, _MINUTES_PER_DAY),) for _ in range(7))
    counts: dict[int, int] = {}
    for weekday, ranges in enumerate(effective):
        if normalized == "hourly":
            counts[weekday] = sum(
                1
                for hour in range(24)
                for start, end in ranges
                if start <= hour * 60 < end
            )
            continue
        minutes = sum(end - start for start, end in ranges)
        step = max(1.0, float(interval_seconds)) / 60.0
        counts[weekday] = int(minutes / step) if minutes else 0
    return counts


def like_prefix(directory: Any) -> str:
    """构造匹配某个目录下所有路径的 SQL LIKE 前缀，并转义通配符。

    末尾附加路径分隔符，避免 `/photos%` 误匹配 `/photos-other/x.jpg`；`%`、`_` 与
    反斜杠会被转义，使用方必须搭配 `ESCAPE '\\'`。

    Args:
        directory: 照片目录路径。

    Returns:
        可直接用于 `LIKE ? ESCAPE '\\'` 的前缀字符串。
    """
    text = str(directory).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{text}{os.sep}%"


def current_setting(configuration_service: Any | None, key: str, fallback: Any) -> Any:
    """按当前生效配置读取单项，未注入配置服务时回退到调用方给定值。

    供仍需兼容旧式标量构造参数的服务在方法内按需取值，避免把配置冻结在实例属性上。

    Args:
        configuration_service: 可选统一配置服务。
        key: 注册表配置键。
        fallback: 未注入配置服务时使用的值。

    Returns:
        当前生效配置值或回退值。
    """
    if configuration_service is None:
        return fallback
    return configuration_service.get(key)


def bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    """把配置值收敛为闭区间内的整数，无法解析时使用回退值。"""
    try:
        if isinstance(value, bool):
            raise ValueError("布尔值不是整数")
        number = int(value)
    except (TypeError, ValueError):
        number = int(fallback)
    return max(minimum, min(maximum, number))


def bounded_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    """把配置值收敛为闭区间内的有限浮点数，无法解析时使用回退值。"""
    try:
        if isinstance(value, bool):
            raise ValueError("布尔值不是数字")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("非有限数字")
    except (TypeError, ValueError):
        number = float(fallback)
    return max(minimum, min(maximum, number))


def bounded_boolean(value: Any, fallback: bool) -> bool:
    """把配置值收敛为布尔值，字符串按常见真值词解析，无法解析时使用回退值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return bool(fallback)
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(fallback)


def _json_object(raw: str, *, field_name: str) -> dict[str, Any]:
    """解析数据库 JSON 对象，损坏数据立即阻止继续使用。"""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{field_name} 不是合法 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{field_name} 必须是 JSON 对象")
    return value


def _canonical_json(value: Any) -> str:
    """生成稳定且保留中文的紧凑 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_timestamp() -> str:
    """返回带时区的当前协调世界时字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_value(
    definition: SettingDefinition,
    value: Any,
    *,
    allow_environment_text: bool,
) -> Any:
    """严格解析单项配置；管理写入不接受用字符串冒充数字或布尔值。"""
    value_type = definition.value_type
    if allow_environment_text and isinstance(value, os.PathLike) and value_type == "string":
        value = str(value)
    if value_type == "boolean":
        if isinstance(value, bool):
            normalized = value
        elif allow_environment_text and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                raise ValueError("必须是布尔值")
            normalized = lowered in {"1", "true", "yes", "on"}
        else:
            raise ValueError("必须是布尔值")
    elif value_type == "integer":
        if isinstance(value, bool):
            raise ValueError("必须是整数")
        if isinstance(value, int):
            normalized = value
        elif allow_environment_text and isinstance(value, str):
            try:
                normalized = int(value.strip())
            except ValueError as error:
                raise ValueError("必须是整数") from error
        else:
            raise ValueError("必须是整数")
    elif value_type == "float":
        if isinstance(value, bool):
            raise ValueError("必须是数字")
        if isinstance(value, (int, float)):
            normalized = float(value)
        elif allow_environment_text and isinstance(value, str):
            try:
                normalized = float(value.strip())
            except ValueError as error:
                raise ValueError("必须是数字") from error
        else:
            raise ValueError("必须是数字")
        if not math.isfinite(normalized):
            raise ValueError("必须是有限数字")
    elif value_type == "string":
        if not isinstance(value, str):
            raise ValueError("必须是字符串")
        normalized = value.strip()
    else:
        raise RuntimeError(f"未知配置类型: {value_type}")

    if definition.minimum is not None and normalized < definition.minimum:
        raise ValueError(f"不能小于 {definition.minimum:g}")
    if definition.maximum is not None and normalized > definition.maximum:
        raise ValueError(f"不能大于 {definition.maximum:g}")
    if definition.choices:
        candidate = normalized
        if isinstance(candidate, str):
            matching = {
                str(choice).lower(): choice
                for choice in definition.choices
                if isinstance(choice, str)
            }
            candidate = matching.get(candidate.lower(), candidate)
        if candidate not in definition.choices:
            choices = "、".join(str(choice) for choice in definition.choices)
            raise ValueError(f"只允许：{choices}")
        normalized = candidate
    return normalized


class SettingsRepository:
    """使用短连接持久化单例配置、全局版本和不可分割审计。"""

    def __init__(self, database_path: str | Path) -> None:
        """保存明确数据库路径，不依赖 Flask 请求上下文。"""
        self.database_path = Path(database_path).expanduser().resolve()

    def read_version(self) -> int:
        """读取轻量全局版本，供每次配置访问检查跨进程失效。"""
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute("SELECT version FROM app_settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("app_settings 缺少单例配置")
        return int(row["version"])

    def read_state(self) -> SettingsState:
        """读取数据库配置覆盖值及其版本。"""
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT settings_json,version FROM app_settings WHERE id=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("app_settings 缺少单例配置")
        return SettingsState(
            version=int(row["version"]),
            values=_json_object(row["settings_json"], field_name="app_settings.settings_json"),
        )

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """按创建时间和编号倒序读取配置审计，不修改任何数据库状态。

        Args:
            limit: 返回条数，必须在 1 到 100 之间。

        Returns:
            可脱离数据库连接使用的审计记录字典列表。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("审计条数必须在 1 到 100 之间")
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT id,batch_id,old_version,new_version,changed_keys_json,"
                "old_values_json,new_values_json,modified_by_user_id,"
                "modified_by_username,created_at FROM app_settings_audit "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_batch(
        self,
        changes: Mapping[str, Any],
        expected_version: int,
        actor: ConfigurationActor,
        old_values: Mapping[str, Any],
    ) -> SettingsState:
        """在单一立即写事务中更新配置、递增版本并写入审计。"""
        now = _utc_timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT settings_json,version FROM app_settings WHERE id=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("app_settings 缺少单例配置")
            current_version = int(row["version"])
            if current_version != expected_version:
                raise ConfigurationConflictError(expected_version, current_version)
            values = _json_object(
                row["settings_json"], field_name="app_settings.settings_json"
            )
            values.update(changes)
            next_version = current_version + 1
            cursor = connection.execute(
                "UPDATE app_settings SET settings_json=?,version=?,modified_by_user_id=?,"
                "modified_by_username=?,updated_at=? WHERE id=1 AND version=?",
                (
                    _canonical_json(values), next_version, actor.user_id,
                    actor.username, now, current_version,
                ),
            )
            if cursor.rowcount != 1:
                latest = connection.execute(
                    "SELECT version FROM app_settings WHERE id=1"
                ).fetchone()
                raise ConfigurationConflictError(
                    expected_version, int(latest["version"]) if latest else -1
                )
            changed_keys = sorted(changes)
            connection.execute(
                "INSERT INTO app_settings_audit(batch_id,old_version,new_version,"
                "changed_keys_json,old_values_json,new_values_json,modified_by_user_id,"
                "modified_by_username,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex, current_version, next_version,
                    _canonical_json(changed_keys), _canonical_json(dict(old_values)),
                    _canonical_json(dict(changes)), actor.user_id, actor.username, now,
                ),
            )
        return SettingsState(next_version, values)


class ConfigurationService:
    """统一解析环境、默认值与数据库热配置，并提供原子管理写入。"""

    def __init__(
        self,
        database_path: str | Path,
        environment: Mapping[str, Any] | None = None,
        registry: Mapping[str, SettingDefinition] | None = None,
    ) -> None:
        """捕获进程启动配置；后续只通过数据库版本刷新热配置。"""
        self.registry = dict(registry or SETTING_REGISTRY)
        source = os.environ if environment is None else environment
        self._initial_values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, definition in self.registry.items():
            if key not in source:
                continue
            try:
                self._initial_values[key] = _normalize_value(
                    definition, source[key], allow_environment_text=True
                )
            except ValueError as error:
                errors[key] = str(error)
        if errors:
            raise ConfigurationValidationError(errors)
        self.repository = SettingsRepository(database_path)
        self._cached_state: SettingsState | None = None

    def _validated_state(self, state: SettingsState) -> SettingsState:
        """校验并规范化数据库持久化覆盖值，供缓存和事务内快照共同复用。"""
        invalid: dict[str, str] = {}
        normalized: dict[str, Any] = {}
        for key, value in state.values.items():
            definition = self.registry.get(key)
            if definition is None:
                invalid[key] = "数据库包含未知配置"
                continue
            if not definition.editable:
                continue
            try:
                normalized[key] = _normalize_value(
                    definition, value, allow_environment_text=False
                )
            except ValueError as error:
                invalid[key] = str(error)
        if invalid:
            raise RuntimeError(f"数据库配置无效: {sorted(invalid)}")
        return SettingsState(state.version, normalized)

    def _state_from_connection(self, connection: Any) -> SettingsState:
        """通过调用方现有 SQLite 连接读取并校验配置，不创建额外连接。"""
        row = connection.execute(
            "SELECT settings_json,version FROM app_settings WHERE id=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("app_settings 缺少单例配置")
        return self._validated_state(
            SettingsState(
                version=int(row["version"]),
                values=_json_object(
                    row["settings_json"], field_name="app_settings.settings_json"
                ),
            )
        )

    def _state(self) -> SettingsState:
        """每次访问先查版本，版本变化时才重载并统一校验完整配置对象。"""
        version = self.repository.read_version()
        if self._cached_state is None or self._cached_state.version != version:
            self._cached_state = self._validated_state(self.repository.read_state())
        return self._cached_state

    def _resolved(self, key: str, state: SettingsState) -> tuple[Any, str]:
        """按数据库热配置、启动环境、注册默认值顺序解析单项来源。"""
        definition = self.registry.get(key)
        if definition is None:
            raise KeyError(key)
        if definition.editable and key in state.values:
            return state.values[key], "database"
        if key in self._initial_values:
            return self._initial_values[key], "environment"
        return definition.default, "default"

    def get(self, key: str) -> Any:
        """读取一个配置项；未知键抛出 KeyError。"""
        return self._resolved(key, self._state())[0]

    def get_many(self, keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """在同一配置版本上读取多个配置项。"""
        state = self._state()
        return {key: self._resolved(key, state)[0] for key in keys}

    def snapshot(
        self, scope: str | None = None, connection: Any | None = None
    ) -> dict[str, Any]:
        """生成排除敏感值的配置快照，可复用调用方现有 SQLite 事务连接。

        Args:
            scope: 可选配置作用域；为空时包含全部非敏感配置。
            connection: 可选当前 SQLite 连接；传入后只通过该连接读取 app_settings。

        Returns:
            包含配置版本和按统一优先级解析后 settings 的字典。
        """
        state = (
            self._state_from_connection(connection)
            if connection is not None
            else self._state()
        )
        settings = {
            key: self._resolved(key, state)[0]
            for key, definition in self.registry.items()
            if not definition.sensitive
            and (scope is None or scope in definition.scopes)
        }
        return {"version": state.version, "settings": settings}

    def task_snapshot(
        self, scope: str, connection: Any | None = None
    ) -> tuple[int, str]:
        """生成可直接持久化的稳定任务快照。

        Args:
            scope: 任务配置作用域。
            connection: 可选当前 SQLite 连接，用于与任务认领保持同一事务视图。

        Returns:
            配置版本及包含 version、settings 的稳定 JSON 文本。
        """
        snapshot = self.snapshot(scope, connection)
        return int(snapshot["version"]), _canonical_json(snapshot)

    def resolve_task_snapshot(
        self, job: Mapping[str, Any], scope: str
    ) -> dict[str, Any]:
        """严格解析任务快照；仅允许版本零空对象的历史任务回退当前配置。

        Args:
            job: 含 config_version 和 config_snapshot_json 的任务记录。
            scope: 当前任务允许使用的配置作用域。

        Returns:
            经注册表类型、范围和作用域校验后的非敏感配置映射。

        Raises:
            ValueError: 快照格式、版本、配置键或配置值不合法。
        """
        version = job.get("config_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("config_version 必须是非负整数")
        try:
            snapshot = json.loads(str(job.get("config_snapshot_json", "")))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("config_snapshot_json 不是合法 JSON") from error
        if not isinstance(snapshot, dict):
            raise ValueError("config_snapshot_json 必须是 JSON 对象")
        if not snapshot:
            if version != 0:
                raise ValueError("非零配置版本不能使用空快照")
            return dict(self.snapshot(scope)["settings"])
        if set(snapshot) != {"version", "settings"}:
            raise ValueError("任务快照顶层字段不合法")
        snapshot_version = snapshot.get("version")
        settings = snapshot.get("settings")
        if (
            isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
            or snapshot_version != version
        ):
            raise ValueError("任务配置版本与快照不一致")
        if not isinstance(settings, dict):
            raise ValueError("任务快照 settings 必须是 JSON 对象")
        expected_keys = {
            key
            for key, definition in self.registry.items()
            if not definition.sensitive and scope in definition.scopes
        }
        if set(settings) != expected_keys:
            raise ValueError("任务快照配置键集合不完整")
        normalized: dict[str, Any] = {}
        for key, value in settings.items():
            definition = self.registry.get(key)
            if definition is None:
                raise ValueError(f"任务快照包含未知配置: {key}")
            if definition.sensitive or scope not in definition.scopes:
                raise ValueError(f"任务快照包含越权配置: {key}")
            try:
                normalized[key] = _normalize_value(
                    definition, value, allow_environment_text=False
                )
            except ValueError as error:
                raise ValueError(f"任务快照配置无效: {key}") from error
        return normalized

    def list_admin_settings(self) -> dict[str, Any]:
        """返回管理视图元数据；敏感项只暴露是否已配置。"""
        state = self._state()
        items: list[dict[str, Any]] = []
        for key, definition in self.registry.items():
            value, source = self._resolved(key, state)
            item = {
                "key": key,
                "name": definition.name,
                "group": definition.group,
                "value_type": definition.value_type,
                "description": definition.description,
                "editable": definition.editable,
                "sensitive": definition.sensitive,
                "restart_required": definition.restart_required,
                "minimum": definition.minimum,
                "maximum": definition.maximum,
                "choices": list(definition.choices),
                "source": source,
            }
            if definition.sensitive:
                item["configured"] = bool(value)
            else:
                item["value"] = value
            items.append(item)
        return {"version": state.version, "settings": items}

    def list_admin_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """解析最近配置审计并补充中文名称，所有敏感键的值均被脱敏。

        Args:
            limit: 返回条数，必须在 1 到 100 之间。

        Returns:
            按时间倒序排列、可安全用于后台页面和接口的审计记录。
        """
        records: list[dict[str, Any]] = []
        for row in self.repository.list_audit(limit):
            try:
                changed_keys = json.loads(row["changed_keys_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("配置审计 changed_keys_json 不是合法 JSON") from error
            if not isinstance(changed_keys, list) or not all(
                isinstance(key, str) for key in changed_keys
            ):
                raise RuntimeError("配置审计 changed_keys_json 必须是字符串数组")
            old_values = _json_object(
                row["old_values_json"], field_name="app_settings_audit.old_values_json"
            )
            new_values = _json_object(
                row["new_values_json"], field_name="app_settings_audit.new_values_json"
            )
            changes: list[dict[str, Any]] = []
            for key in changed_keys:
                definition = self.registry.get(key)
                normalized_key = key.upper()
                sensitive = bool(definition and definition.sensitive) or any(
                    marker in normalized_key
                    for marker in ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "DOWNLOAD_KEY")
                )
                changes.append(
                    {
                        "key": key,
                        "name": definition.name if definition else key,
                        "old_value": "已脱敏" if sensitive else old_values.get(key),
                        "new_value": "已脱敏" if sensitive else new_values.get(key),
                    }
                )
            records.append(
                {
                    "id": int(row["id"]),
                    "batch_id": row["batch_id"],
                    "old_version": int(row["old_version"]),
                    "new_version": int(row["new_version"]),
                    "modified_by_user_id": row["modified_by_user_id"],
                    "modified_by_username": row["modified_by_username"],
                    "created_at": row["created_at"],
                    "changes": changes,
                }
            )
        return records

    def update_batch(
        self,
        changes: Mapping[str, Any],
        expected_version: int,
        actor: ConfigurationActor,
    ) -> dict[str, Any]:
        """严格校验整批配置，以全局版本乐观锁提交实际变化项。

        版本检查先于相同值过滤，过期提交即使没有实际变化也会冲突；全部值均与
        当前有效值相同时直接返回当前管理视图，不递增版本且不写审计。敏感且可
        编辑的配置只写不读：提交空字符串表示保持原值，提交非空值则覆盖，且值
        不会出现在管理视图、接口响应与审计记录中。
        """
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ConfigurationValidationError({"expected_version": "必须是整数"})
        if not isinstance(actor, ConfigurationActor) or not actor.username.strip():
            raise ValueError("配置修改人用户名不能为空")
        if not changes:
            raise ConfigurationValidationError({"changes": "至少包含一个配置项"})

        errors: dict[str, str] = {}
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            definition = self.registry.get(key)
            if definition is None:
                errors[key] = "未知配置项"
                continue
            if not definition.editable or definition.restart_required:
                errors[key] = "该配置只读或需要通过部署环境修改"
                continue
            if definition.sensitive:
                if not isinstance(value, str):
                    errors[key] = "必须是字符串"
                    continue
                if not value.strip():
                    # 敏感配置留空表示保持原值，避免页面不回显导致误清空。
                    continue
            try:
                normalized[key] = _normalize_value(
                    definition, value, allow_environment_text=False
                )
            except ValueError as error:
                errors[key] = str(error)
                continue
            if definition.validator is not None:
                # 语义校验只在写入路径执行：读取路径若同样强校验，网络存储临时不可
                # 用就会让整个服务无法读配置。
                try:
                    definition.validator(normalized[key])
                except ValueError as error:
                    errors[key] = str(error)
                    normalized.pop(key, None)
        if errors:
            raise ConfigurationValidationError(errors)

        state = self._state()
        if state.version != expected_version:
            raise ConfigurationConflictError(expected_version, state.version)
        effective_changes = {
            key: value
            for key, value in normalized.items()
            if value != self._resolved(key, state)[0]
        }
        if not effective_changes:
            return self.list_admin_settings()
        old_values = {
            key: self._resolved(key, state)[0] for key in effective_changes
        }
        updated = self.repository.update_batch(
            effective_changes, expected_version, actor, old_values
        )
        self._cached_state = updated
        return self.list_admin_settings()
