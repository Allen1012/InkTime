"""独立于 Flask 的统一配置注册、读取、校验与持久化服务。"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
    # 候选值的中文显示名，形如 (("baidu", "百度百科"),)。写入与存储仍用英数字
    # 标识，避免中文值出现在 .env、容器环境变量和数据库里；未列出的候选值在界面
    # 上直接显示原值。
    choice_labels: tuple[tuple[Any, str], ...] = ()
    scopes: tuple[str, ...] = ()
    validator: Callable[[Any], Any] | None = None
    # 用途路由单独固化到任务快照顶层 provider，不能混入历史 settings 精确键集合。
    task_snapshot: bool = True
    # 后台配置页的显示单位与换算刻度，只影响人看的那一层：存储、环境变量、任务快照与
    # JSON 接口一律使用基准单位（字节），页面按 `值 / display_scale` 显示并在提交时乘回去。
    # 这样「把 64 MiB 写成 67108864」这件事不再需要人来做，而 .env、Compose 与派生的
    # MAX_CONTENT_LENGTH 都不必跟着改语义——改配置键的单位会让现有部署里那行字节数被
    # 悄悄当成 MB，属于静默破坏。
    #
    # 刻度取 2 的幂，因此整数字节除以刻度总能被浮点精确表示，来回换算不会产生漂移。
    display_unit: str = ""
    display_scale: int = 1
    # 基准单位的中文名（字节、秒、像素），用于在页面上写出「等于 N 秒」这类换算提示。
    # 只读项同样需要它：那些项只能在部署环境按基准单位设置，提示要说清填的是什么。
    base_unit: str = ""


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


def _validate_weather_location(value: Any) -> None:
    """校验天气位置坐标可被解析且在合法范围内。

    留空表示回落到常驻地坐标，因此空值合法。

    Args:
        value: 待写入的 `纬度,经度` 配置值。

    Raises:
        ValueError: 格式不是两段、不是数字或超出经纬度范围。
    """
    from src.server.weather import parse_location

    parse_location(value, home_lat=0.0, home_lon=0.0)


def _validate_provider_route(value: Any) -> None:
    """校验分号分隔的模型厂商路由名称格式；空值表示回退旧配置。

    Args:
        value: 待保存的用途路由字符串。

    Raises:
        ValueError: 名称为空段、过长、重复或含换行。
    """
    text = str(value or "").strip()
    if not text:
        return
    names = [item.strip() for item in text.split(";")]
    if any(not name for name in names):
        raise ValueError("厂商名称之间不能有空项")
    if any(len(name) > 50 for name in names):
        raise ValueError("厂商名称不能超过 50 个字符")
    if any("\n" in name or "\r" in name for name in names):
        raise ValueError("厂商名称不能包含换行")
    if len(names) != len(set(names)):
        raise ValueError("厂商路由不能包含重复名称")


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
    _setting("PERMANENT_SESSION_LIFETIME", "会话有效期", "security", "integer", 28800, "管理员永久会话有效期。只能在部署环境按秒设置，本页按小时显示。", minimum=1, scopes=("web",), display_unit="小时", display_scale=3600, base_unit="秒"),
    _setting("WTF_CSRF_TIME_LIMIT", "跨站请求伪造令牌有效期", "security", "integer", 3600, "表单令牌有效期。只能在部署环境按秒设置，本页按小时显示。", minimum=1, scopes=("web",), display_unit="小时", display_scale=3600, base_unit="秒"),
    _setting("ADMIN_LOGIN_MAX_FAILURES", "登录失败上限", "security", "integer", 5, "登录限流窗口内允许的失败次数。", minimum=1, scopes=("web",)),
    _setting("ADMIN_LOGIN_FAILURE_WINDOW_SECONDS", "登录失败统计窗口", "security", "integer", 300, "管理员登录失败限流窗口。只能在部署环境按秒设置，本页按分钟显示。", minimum=1, scopes=("web",), display_unit="分钟", display_scale=60, base_unit="秒"),
    # 这一项决定「哪个动作是付费闸门」，而不只是一个默认值，因此描述里要把两种模式的
    # 后果写全：选「默认未收录」时收录动作本身就是闸门，收录即排队分析；选「默认已收录」
    # 时新照片直接进候选池，闸门退回照片管理页的按张数放行。两者绑在同一个开关上是有意
    # 的——拆成「默认收录」加「收录即分析」两个独立开关，就能配出「扫描进来的每张照片
    # 都自动调用模型」这种没有任何闸门的组合，那正是这套机制要防的事情。
    _setting("NEW_PHOTO_CURATION", "新照片默认收录状态", "analysis", "string", "excluded", "扫描照片目录登记的新照片默认是否收录。选「默认未收录」时，把照片改为已收录就会自动排队分析，不必再按张数放行；选「默认已收录」时，新照片直接进入相框候选，分析仍需在照片管理页按张数放行。后台上传不受本项影响，一律按已收录登记并立即排队分析。", editable=True, restart_required=False, choices=("excluded", "included"), choice_labels=(("excluded", "默认未收录（改为已收录即分析）"), ("included", "默认已收录（按张数放行分析）")), scopes=("web",)),
    _setting("ANALYSIS_PROVIDER", "照片分析厂商路由", "analysis", "string", "", "照片评分与内容识别使用的厂商名称；多个名称用分号分隔。留空或厂商不可用时回退兼容模型接口。", editable=True, restart_required=False, validator=_validate_provider_route, task_snapshot=False, scopes=("analysis", "worker", "web")),
    _setting("NARRATION_PROVIDER", "照片旁白厂商路由", "analysis", "string", "", "照片旁白使用的厂商名称；留空时先跟随照片分析厂商路由，再回退兼容模型接口。", editable=True, restart_required=False, validator=_validate_provider_route, task_snapshot=False, scopes=("analysis", "worker", "web")),
    _setting("PANEL_PROVIDER", "信息面板厂商路由", "display", "string", "", "历史上的今天使用模型筛选时采用的厂商名称；留空时先跟随照片分析厂商路由，再回退兼容模型接口。", editable=True, restart_required=False, validator=_validate_provider_route, task_snapshot=False, scopes=("web",)),
    _setting("API_URL", "模型接口地址", "analysis", "string", "http://127.0.0.1:1234/v1/chat/completions", "OpenAI 兼容模型接口地址。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("MODEL_NAME", "分析模型", "analysis", "string", "qwen3-vl-32b-instruct", "照片分析使用的视觉语言模型。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("TIMEOUT", "模型请求超时秒数", "analysis", "integer", 600, "模型请求超时时间。", editable=True, restart_required=False, minimum=1, scopes=("analysis", "worker")),
    _setting("VLM_MAX_LONG_EDGE", "模型图片最长边", "analysis", "integer", 2560, "发送给视觉语言模型的图片最长边像素。", editable=True, restart_required=False, minimum=256, maximum=8192, scopes=("analysis", "worker")),
    _setting("WORLD_CITIES_CSV", "城市索引路径", "analysis", "string", "", "离线中文城市索引文件。留空按顺序自动查找：data/world_cities_zh.csv，然后是随代码分发的 resources/world_cities_zh.csv。", editable=True, restart_required=False, scopes=("analysis", "worker")),
    _setting("CITY_GRID_DEG", "城市网格精度", "analysis", "float", 1.0, "城市候选网格精度。", editable=True, restart_required=False, minimum=0.01, maximum=10, scopes=("analysis", "worker")),
    _setting("CITY_MAX_DISTANCE_KM", "城市匹配最大距离", "analysis", "float", 100.0, "坐标与城市的最大匹配距离。", editable=True, restart_required=False, minimum=0, maximum=20000, scopes=("analysis", "worker")),
    _setting("HOME_LAT", "常驻地纬度", "analysis", "float", 22.543096, "常驻地纬度。", editable=True, restart_required=False, minimum=-90, maximum=90, scopes=("analysis", "worker")),
    _setting("HOME_LON", "常驻地经度", "analysis", "float", 114.057865, "常驻地经度。", editable=True, restart_required=False, minimum=-180, maximum=180, scopes=("analysis", "worker")),
    _setting("HOME_RADIUS_KM", "常驻地半径", "analysis", "float", 60.0, "常驻地判断半径。", editable=True, restart_required=False, minimum=0, maximum=20000, scopes=("analysis", "worker")),
    _setting("FONT_PATH", "字体路径", "render", "string", "", "图片渲染使用的中文字体文件。", editable=True, restart_required=False, scopes=("render", "worker")),
    _setting("THUMBNAIL_MAX_EDGE", "缩略图长边像素", "display", "integer", 640, "照片墙与后台网格所用缩略图的长边上限。原值 300 在高清屏上会明显模糊；小图不会被放大。", editable=True, restart_required=False, minimum=64, maximum=2048, scopes=("web",)),
    _setting("THUMBNAIL_CACHE_ENABLED", "缩略图磁盘缓存", "display", "boolean", True, "是否把生成好的缩略图缓存到 data/cache/thumbnails。关闭后每次请求都实时解码原图。", editable=True, restart_required=False, scopes=("web",)),
    _setting("THUMBNAIL_QUALITY", "缩略图 JPEG 质量", "display", "integer", 82, "缩略图编码质量，取值 40 到 95。越高越清晰、体积越大。", editable=True, restart_required=False, minimum=40, maximum=95, scopes=("web",)),
    _setting("DISPLAY_TEMPLATE", "展示页模板", "display", "string", "classic", "展示页布局模板。", editable=True, restart_required=False, choices=("classic", "dashboard"), choice_labels=(("classic", "经典大图"), ("dashboard", "信息看板")), scopes=("web",)),
    _setting("DISPLAY_ROTATE_MODE", "展示切换模式", "display", "string", "interval", "展示页自动切换模式。", editable=True, restart_required=False, choices=("interval", "hourly", "minutely", "daily", "off"), choice_labels=(("interval", "固定间隔切换"), ("hourly", "整点切换"), ("minutely", "整分切换（调试用）"), ("daily", "每天零点切换"), ("off", "不自动切换")), scopes=("web",)),
    _setting("DISPLAY_ROTATE_INTERVAL_SEC", "展示切换间隔秒数", "display", "integer", 60, "「固定间隔切换」模式的自动切换间隔。", editable=True, restart_required=False, minimum=1, maximum=86400, scopes=("web",)),
    _setting("DISPLAY_KEEP_AWAKE", "展示页保持唤醒", "display", "boolean", True, "是否请求浏览器阻止空闲息屏。", editable=True, restart_required=False, scopes=("web",)),
    _setting("DISPLAY_UI_HIDE_DELAY_SEC", "展示界面隐藏延迟", "display", "integer", 3, "静置后隐藏操作界面的秒数，零表示不隐藏。", editable=True, restart_required=False, minimum=0, maximum=3600, scopes=("web",)),
    _setting("DISPLAY_MIN_SCORE", "展示最低回忆度", "display", "float", 70.0, "展示页候选照片最低回忆度，零表示不限制。", editable=True, restart_required=False, minimum=0, maximum=100, scopes=("web",)),
    _setting("DISPLAY_ACTIVE_WINDOWS", "展示生效时间段", "display", "string", "", "展示页自动轮播的生效时间段，分号分隔，格式 星期@HH:MM-HH:MM，星期可省略表示每天。留空表示全天生效。", editable=True, restart_required=False, validator=_validate_time_windows, scopes=("web",)),
    _setting("DISPLAY_IDLE_MODE", "休息期画面", "display", "string", "freeze", "非生效时间段的画面：停在最后一张、显示指定照片，或显示休息文案。", editable=True, restart_required=False, choices=("freeze", "photo", "rest"), choice_labels=(("freeze", "停在最后一张"), ("photo", "显示指定照片"), ("rest", "显示休息文案")), scopes=("web",)),
    _setting("DISPLAY_IDLE_PHOTO_ID", "休息期固定照片编号", "display", "integer", 0, "「显示指定照片」模式使用的照片编号，零表示未指定；照片不存在或已删除时回退为停在最后一张。", editable=True, restart_required=False, minimum=0, scopes=("web",)),
    _setting("DISPLAY_REST_TEXT", "休息期文案", "display", "string", "休息中", "「显示休息文案」模式显示的文案。", editable=True, restart_required=False, scopes=("web",)),
    _setting("WEATHER_ENABLED", "启用天气显示", "display", "boolean", False, "是否在展示页显示当前天气。关闭时不会向外部服务发起任何请求。", editable=True, restart_required=False, scopes=("web",)),
    _setting("WEATHER_PROVIDER", "天气数据源", "display", "string", "open-meteo", "天气数据来源。Open-Meteo 免注册免密钥。", editable=True, restart_required=False, choices=("open-meteo",), choice_labels=(("open-meteo", "Open-Meteo（免注册免密钥）"),), scopes=("web",)),
    _setting("WEATHER_LOCATION", "天气位置坐标", "display", "string", "", "格式「纬度,经度」。留空则使用常驻地坐标（HOME_LAT 与 HOME_LON）。坐标会发送给天气服务，建议只填到小数点后一位。", editable=True, restart_required=False, validator=_validate_weather_location, scopes=("web",)),
    _setting("WEATHER_LOCATION_NAME", "天气地名", "display", "string", "", "展示用地名，留空则不显示。不做地名反查以免增加外部依赖。", editable=True, restart_required=False, scopes=("web",)),
    _setting("WEATHER_CACHE_MINUTES", "天气缓存分钟数", "display", "integer", 15, "服务端天气缓存时长，缓存期内不重复请求外部服务。", editable=True, restart_required=False, minimum=1, maximum=1440, scopes=("web",)),
    _setting("DISPLAY_WEATHER_SHOW", "仪表盘显示天气", "display", "boolean", True, "仪表盘模板是否显示天气块。", editable=True, restart_required=False, scopes=("web",)),
    _setting("DISPLAY_WEATHER_CORNER", "沉浸式显示天气角标", "display", "boolean", False, "沉浸式模板是否在右上角显示极简天气角标。默认关闭，以保持照片占满、干扰最小。", editable=True, restart_required=False, scopes=("web",)),
    _setting("DISPLAY_NEW_PHOTO_WEIGHT", "新照片展示权重", "display", "float", 3.0, "未展示照片在同轮候选中的权重。", editable=True, restart_required=False, minimum=1, maximum=100, scopes=("web",)),
    _setting("ONTHISDAY_COUNT", "历史上的今天条数", "display", "integer", 2, "信息面板展示的历史事件数量。", editable=True, restart_required=False, minimum=1, maximum=20, scopes=("web",)),
    _setting("ONTHISDAY_SOURCE", "历史事件数据源", "display", "string", "baidu", "历史事件取数来源。百度百科为国内直连、免密钥，且带事件类型可精确过滤逝世类；60s 是百度数据的开源封装，作备用；维基百科在国内网络下可能不可达。", editable=True, restart_required=False, choices=("baidu", "60s", "wikipedia"), choice_labels=(("baidu", "百度百科"), ("60s", "60s 开源接口"), ("wikipedia", "维基百科")), scopes=("web",)),
    _setting("ONTHISDAY_STRATEGY", "历史事件筛选策略", "display", "string", "curated", "历史事件筛选策略。", editable=True, restart_required=False, choices=("recent", "curated", "ai"), choice_labels=(("recent", "最近优先"), ("curated", "规则精选"), ("ai", "模型筛选")), scopes=("web",)),
    _setting("ONTHISDAY_MIN_YEAR", "历史事件最小年份", "display", "integer", 1900, "「规则精选」策略允许的最早年份。", editable=True, restart_required=False, minimum=1, maximum=9999, scopes=("web",)),
    _setting("PANEL_AI_MODEL", "信息面板模型", "display", "string", "", "人工智能筛选策略使用的模型，空值回退分析模型。", editable=True, restart_required=False, scopes=("web",)),
    _setting("MEMORY_THRESHOLD", "渲染回忆度阈值", "render", "float", 70.0, "每日渲染候选照片最低回忆度。", editable=True, restart_required=False, minimum=0, maximum=100, scopes=("render", "worker")),
    _setting("DAILY_PHOTO_QUANTITY", "每日渲染照片数量", "render", "integer", 5, "每日渲染的照片数量。", editable=True, restart_required=False, minimum=1, maximum=20, scopes=("render", "worker")),
    _setting("FILL_FROM_GLOBAL", "全局照片补足", "render", "boolean", True, "历史同日照片不足时是否从全局高分照片补足。", editable=True, restart_required=False, scopes=("render", "worker")),
    _setting("ENABLE_REVIEW_WEBUI", "产物目录浏览总开关", "system", "boolean", True, "产物目录浏览的第二重开关，需与「启用产物目录浏览」同时为真才开放 /files/。不影响照片墙、分类、搜索与展示页。", editable=True, restart_required=False, scopes=("web",)),
    _setting("ENABLE_FILE_BROWSER", "启用产物目录浏览", "system", "boolean", False, "是否开放产物文件目录浏览。", editable=True, restart_required=False, scopes=("web",)),
    _setting("UPLOAD_MAX_FILES", "单批上传文件数", "worker", "integer", 10, "单批上传允许的最大文件数。", editable=True, restart_required=False, minimum=1, maximum=10, scopes=("web", "worker")),
    _setting("UPLOAD_MAX_BYTES", "单文件上传上限", "worker", "integer", 67108864, "单个上传文件允许的最大体积，上界 100 MiB。手机原图常有四五十兆，默认放到 64 MiB。环境变量与接口按字节取值，本页按 MiB 填写。", editable=True, restart_required=False, minimum=1, maximum=104857600, scopes=("web", "worker"), display_unit="MiB", display_scale=1048576, base_unit="字节"),
    _setting("UPLOAD_TARGET_BYTES", "上传压缩目标体积", "worker", "integer", 5242880, "上传照片落盘的目标体积，超过则先按长边缩放再逐档降质压到该体积以内。零表示不压缩。PNG 只缩放不降质。可填小数，例如 0.5 表示 512 KiB。环境变量与接口按字节取值，本页按 MiB 填写。", editable=True, restart_required=False, minimum=0, maximum=104857600, scopes=("web", "worker"), display_unit="MiB", display_scale=1048576, base_unit="字节"),
    _setting("UPLOAD_MAX_LONG_EDGE", "上传图片长边上限", "worker", "integer", 4096, "上传照片落盘时的长边像素上限，超过则等比缩小。零表示不缩放。", editable=True, restart_required=False, minimum=0, maximum=20000, scopes=("web", "worker")),
    _setting("UPLOAD_MAX_PIXELS", "单图像素上限", "worker", "integer", 80000000, "上传图片解码后的最大像素数，用于挡住解压炸弹。环境变量与接口按像素取值，本页按百万像素填写。", editable=True, restart_required=False, minimum=1, maximum=80000000, scopes=("web", "worker"), display_unit="百万像素", display_scale=1000000, base_unit="像素"),
    _setting("JOB_MAX_ATTEMPTS", "任务最大尝试次数", "worker", "integer", 3, "后台任务最大执行次数。", editable=True, restart_required=False, minimum=1, maximum=3, scopes=("web", "worker")),
    _setting("JOB_RETRY_BACKOFF_SECONDS", "任务重试退避秒数", "worker", "integer", 30, "任务失败后重新认领前的等待秒数基数，按尝试次数指数增长（30、60 秒）。零表示立即重试，可能在上游抖动或限流时几秒内烧光全部尝试次数。", editable=True, restart_required=False, minimum=0, maximum=3600, scopes=("web", "worker")),
    _setting("JOB_LEASE_SECONDS", "任务租约秒数", "worker", "integer", 120, "后台任务租约时长，实际生效下界为 2 秒。", editable=True, restart_required=False, minimum=1, scopes=("web", "worker")),
    _setting("JOB_RENEW_SECONDS", "任务续租秒数", "worker", "integer", 30, "后台任务续租间隔，必须小于租约时长，否则自动收敛为租约减一秒。", editable=True, restart_required=False, minimum=1, scopes=("web", "worker")),
    _setting("JOB_POLL_SECONDS", "任务轮询秒数", "worker", "float", 2.0, "工作进程空队列轮询间隔。", editable=True, restart_required=False, minimum=0.1, scopes=("worker",)),
    _setting("TRASH_RETENTION_DAYS", "回收站保留天数", "worker", "integer", 30, "回收站过期清理的默认保留天数。", editable=True, restart_required=False, minimum=1, maximum=3650, scopes=("web", "worker")),
)

SETTING_REGISTRY: dict[str, SettingDefinition] = {
    definition.key: definition for definition in _SETTING_DEFINITIONS
}


def choice_label(key: str, value: Any) -> str:
    """取配置项某个候选值的中文显示名，未登记时回退为原值。

    存在的意义是让页面上除下拉框以外的地方（例如时间段摘要里的当前切换模式）
    也能复用注册表里唯一的一份中文措辞，避免同一个值在一处显示「整点切换」、
    另一处显示 `hourly`。

    Args:
        key: 配置项键名，未登记的键按原值返回。
        value: 候选值，通常是存储用的英数字标识。

    Returns:
        中文显示名；键或候选值未登记标签时返回值的字符串形式。
    """
    text = str(value)
    definition = SETTING_REGISTRY.get(key)
    if definition is None:
        return text
    for candidate, label in definition.choice_labels:
        if str(candidate) == text:
            return label
    return text


def format_display_number(value: Any) -> str:
    """把换算后的显示值格式化成不带多余零、且能精确往返的文本。

    刻度都取 2 的幂，整数字节除以刻度的结果可被浮点精确表示，因此这里不做四舍五入：
    截断显示会让「保存一次无关配置」把 204800 字节悄悄写成 204472，属于隐蔽的数据漂移。

    Args:
        value: 已按显示刻度换算过的数值。

    Returns:
        整数值去掉小数点，其余给出最短往返写法。
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def to_display_value(definition: SettingDefinition, value: Any) -> Any:
    """把基准单位的值换算成配置页显示用的值。"""
    if definition.display_scale <= 1 or not isinstance(value, (int, float)):
        return value
    if isinstance(value, bool):
        return value
    return value / definition.display_scale


def from_display_value(definition: SettingDefinition, value: Any) -> Any:
    """把配置页填写的值换算回基准单位。

    整数型配置换算后取最近整数：页面按 MiB 填写小数时，乘回字节几乎不会正好落在整数上。
    """
    if definition.display_scale <= 1:
        return value
    scaled = float(value) * definition.display_scale
    return int(round(scaled)) if definition.value_type == "integer" else scaled


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


# 审计脱敏占位文本，与页面上的措辞保持一致，便于人工核对。
REDACTED_TEXT = "已脱敏"
# 即使注册表里没标 sensitive，键名命中这些片段也一律按敏感处理。存在的意义是给
# 「以后有人加了敏感配置却忘了标 sensitive」留一道兜底，宁可多脱敏也不能漏。
_SENSITIVE_KEY_MARKERS = ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "DOWNLOAD_KEY")


def is_sensitive_key(key: str, registry: Mapping[str, SettingDefinition] | None = None) -> bool:
    """判断配置键是否按敏感处理。

    写入审计与展示审计共用这一份口径，避免两处判断走偏——走偏的表现是页面显示
    「已脱敏」而数据库里躺着原值，属于看起来安全的不安全。

    Args:
        key: 配置键名。
        registry: 可选注册表，缺省使用全局注册表。

    Returns:
        命中注册表的 sensitive 标记或键名片段时返回真。
    """
    definitions = registry if registry is not None else SETTING_REGISTRY
    definition = definitions.get(key)
    if definition is not None and definition.sensitive:
        return True
    normalized = key.upper()
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def redact_sensitive_values(
    values: Mapping[str, Any],
    registry: Mapping[str, SettingDefinition] | None = None,
) -> dict[str, Any]:
    """把映射里敏感键的值替换为占位文本，供写入审计前调用。

    脱敏必须发生在写入前：数据库备份、`sqlite3` 直连与误导出都会绕过展示层，
    只在页面上显示「已脱敏」并不能阻止密钥从审计表里被读出来。

    Args:
        values: 待写入审计的键值映射。
        registry: 可选注册表，缺省使用全局注册表。

    Returns:
        敏感值已替换的新映射，非敏感值原样保留。
    """
    return {
        key: (REDACTED_TEXT if is_sensitive_key(key, registry) else value)
        for key, value in values.items()
    }


def redact_settings_audit_history(
    database_path: str | Path, *, apply_changes: bool = False
) -> dict[str, Any]:
    """把配置审计历史里残留的敏感值原地替换为占位文本。

    写入路径的脱敏只对新记录生效。任何在该修复之前通过后台改过 `API_KEY` 的部署，
    审计表里都躺着一份明文，且它会随每一次数据库备份被复制出去。这个函数负责补上
    那段历史，同时**保留审计的时间、版本、变更键与操作人**——审计的价值在于「谁在
    什么时候改了什么」，为了脱敏把整行删掉是过度处置。

    默认只试运行：改正式库前先看清会动几行，这是不可逆写操作应有的默认值。

    Args:
        database_path: 目标数据库文件。
        apply_changes: 为真时才真正写入，否则只统计。

    Returns:
        含 scanned、affected（受影响的审计行编号）、applied 的报告。
    """
    path = Path(database_path)
    pending: list[tuple[int, str, str]] = []
    with database_connection(path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT id,old_values_json,new_values_json FROM app_settings_audit ORDER BY id"
        ).fetchall()
        for row in rows:
            old_values = _json_object(
                row["old_values_json"], field_name="app_settings_audit.old_values_json"
            )
            new_values = _json_object(
                row["new_values_json"], field_name="app_settings_audit.new_values_json"
            )
            redacted_old = _canonical_json(redact_sensitive_values(old_values))
            redacted_new = _canonical_json(redact_sensitive_values(new_values))
            # 只在内容真的会变时才登记：已经脱敏过的行重复执行不该被算成受影响，
            # 否则这个命令永远报「还有 N 行要处理」，看不出到底清干净了没有。
            if redacted_old != row["old_values_json"] or redacted_new != row["new_values_json"]:
                pending.append((int(row["id"]), redacted_old, redacted_new))
    if apply_changes and pending:
        with write_transaction(path) as connection:
            for audit_id, redacted_old, redacted_new in pending:
                connection.execute(
                    "UPDATE app_settings_audit SET old_values_json=?,new_values_json=? "
                    "WHERE id=?",
                    (redacted_old, redacted_new, audit_id),
                )
    return {
        "scanned": len(rows),
        "affected": [audit_id for audit_id, _old, _new in pending],
        "applied": bool(apply_changes and pending),
    }


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
                    _canonical_json(changed_keys),
                    _canonical_json(redact_sensitive_values(old_values)),
                    _canonical_json(redact_sensitive_values(changes)),
                    actor.user_id, actor.username, now,
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
        environment_keys: Iterable[str] | None = None,
    ) -> None:
        """捕获进程启动配置；后续只通过数据库版本刷新热配置。

        `environment` 是启动时解析出的全部初始值，Web 进程会把 Flask 配置默认值
        也合并进来，因此「键在 environment 里」并不等于「部署方显式设置过」。
        需要区分两者时由调用方另传 `environment_keys`，只列真正来自进程环境的键；
        不传则退化为 `environment` 的全部键，兼容直接传 `os.environ` 的脚本。
        """
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
        if environment_keys is None:
            self._environment_keys = set(self._initial_values)
        else:
            self._environment_keys = {
                key for key in environment_keys if key in self.registry
            }
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

    def get_many(
        self, keys: list[str] | tuple[str, ...], connection: Any | None = None
    ) -> dict[str, Any]:
        """在同一配置版本上读取多个配置项，可复用现有事务连接。

        Args:
            keys: 要读取的配置键。
            connection: 可选当前 SQLite 连接；传入后不负责关闭。

        Returns:
            配置键到当前有效值的映射。
        """
        state = (
            self._state_from_connection(connection)
            if connection is not None
            else self._state()
        )
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
            and definition.task_snapshot
            and (scope is None or scope in definition.scopes)
        }
        return {"version": state.version, "settings": settings}

    def task_snapshot(
        self,
        scope: str,
        connection: Any | None = None,
        *,
        provider: Mapping[str, Any] | None = None,
    ) -> tuple[int, str]:
        """生成可直接持久化的稳定任务快照，可附带公开厂商档案。

        Args:
            scope: 任务配置作用域。
            connection: 可选当前 SQLite 连接，用于与任务认领保持同一事务视图。
            provider: 按用途组织的公开厂商执行参数，严禁包含密钥。

        Returns:
            配置版本及稳定 JSON 文本；没有厂商路由时保持旧两字段结构。
        """
        snapshot = self.snapshot(scope, connection)
        normalized_provider = self._normalize_provider_snapshot(provider or {})
        if normalized_provider:
            snapshot["provider"] = normalized_provider
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
        if set(snapshot) not in (
            {"version", "settings"},
            {"version", "settings", "provider"},
        ):
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
            if not definition.sensitive
            and definition.task_snapshot
            and scope in definition.scopes
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
        self._normalize_provider_snapshot(snapshot.get("provider", {}))
        return normalized

    @staticmethod
    def _normalize_provider_snapshot(provider: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """严格校验厂商快照并把阶段二单对象统一提升为阶段三有序数组。

        Args:
            provider: 按 analysis、narration 用途组织的单对象或非空对象数组。

        Returns:
            各用途始终对应公开厂商档案列表的规范字典。

        Raises:
            ValueError: 用途、数组、字段集合、字段类型或范围不合法。
        """
        if not isinstance(provider, Mapping):
            raise ValueError("任务快照 provider 必须是 JSON 对象")
        if not set(provider).issubset({"analysis", "narration"}):
            raise ValueError("任务快照 provider 用途不合法")
        required_fields = {
            "id", "name", "version", "base_url", "model_name",
            "timeout_seconds", "max_long_edge",
        }
        # 高级请求参数是可选字段：没配过额外参数的厂商不写这一项，阶段三之前固化的
        # 快照也不会有它。因此这里校验「必需字段齐全且没有未知字段」，而不是精确相等。
        optional_fields = {"request_options"}
        normalized: dict[str, list[dict[str, Any]]] = {}
        for purpose, raw_chain in provider.items():
            if isinstance(raw_chain, Mapping):
                candidates = [raw_chain]
            elif isinstance(raw_chain, list) and raw_chain:
                candidates = raw_chain
            else:
                raise ValueError(f"任务快照 provider.{purpose} 必须是非空数组")
            chain: list[dict[str, Any]] = []
            for raw in candidates:
                if not isinstance(raw, Mapping):
                    raise ValueError(f"任务快照 provider.{purpose} 字段不合法")
                present = set(raw)
                if not required_fields.issubset(present):
                    raise ValueError(f"任务快照 provider.{purpose} 字段不合法")
                if present - required_fields - optional_fields:
                    raise ValueError(f"任务快照 provider.{purpose} 字段不合法")
                identifier = raw.get("id")
                version = raw.get("version")
                timeout = raw.get("timeout_seconds")
                long_edge = raw.get("max_long_edge")
                if any(isinstance(value, bool) or not isinstance(value, int) for value in (
                    identifier, version, timeout, long_edge
                )):
                    raise ValueError(f"任务快照 provider.{purpose} 数字字段不合法")
                if identifier < 1 or version < 1 or not 1 <= timeout <= 600:
                    raise ValueError(f"任务快照 provider.{purpose} 数字范围不合法")
                if not 256 <= long_edge <= 8192:
                    raise ValueError(f"任务快照 provider.{purpose} 图片边长不合法")
                name = raw.get("name")
                base_url = raw.get("base_url")
                model_name = raw.get("model_name")
                if any(not isinstance(value, str) or not value.strip() for value in (
                    name, base_url, model_name
                )):
                    raise ValueError(f"任务快照 provider.{purpose} 文本字段不合法")
                candidate = {
                    "id": identifier,
                    "name": name.strip(),
                    "version": version,
                    "base_url": base_url.strip(),
                    "model_name": model_name.strip(),
                    "timeout_seconds": timeout,
                    "max_long_edge": long_edge,
                }
                if "request_options" in raw:
                    options = raw["request_options"]
                    if not isinstance(options, Mapping):
                        raise ValueError(
                            f"任务快照 provider.{purpose} 高级请求参数必须是对象"
                        )
                    candidate["request_options"] = {
                        str(key): value for key, value in options.items()
                    }
                chain.append(candidate)
            normalized[str(purpose)] = chain
        return normalized

    def resolve_task_provider_snapshot(
        self, job: Mapping[str, Any], scope: str
    ) -> dict[str, list[dict[str, Any]]]:
        """双读阶段二单对象与阶段三数组，并统一返回用途到列表的映射。

        Args:
            job: 含配置版本和快照 JSON 的任务记录。
            scope: 当前任务配置作用域，用于复用完整快照校验。

        Returns:
            按用途组织的公开厂商有序列表；旧无 provider 快照为空映射。
        """
        self.resolve_task_snapshot(job, scope)
        snapshot = json.loads(str(job.get("config_snapshot_json", "")))
        if not snapshot:
            return {}
        return self._normalize_provider_snapshot(snapshot.get("provider", {}))

    def list_admin_settings(self) -> dict[str, Any]:
        """返回管理视图元数据；敏感项只暴露是否已配置。

        `source` 是胜出的来源（数据库热配置、启动值、注册默认值），但 Web 进程把
        Flask 配置默认值也算作启动值，所以它不能回答「这项是不是部署方设的」。
        另外两个布尔值专门给页面用：`from_environment` 表示该键真的出现在进程
        环境里；`environment_overridden` 表示这个环境值已被数据库覆盖压住——这正是
        「改了 .env 重启却没生效」的成因，只看 `source` 是看不出来的。
        """
        state = self._state()
        items: list[dict[str, Any]] = []
        for key, definition in self.registry.items():
            value, source = self._resolved(key, state)
            from_environment = key in self._environment_keys
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
                # 显示层字段：页面按 display_unit 填写，接口与存储仍是基准单位。
                # display_value 与 display_minimum/maximum 已按刻度换算，模板直接用。
                "display_unit": definition.display_unit,
                "display_scale": definition.display_scale,
                "base_unit": definition.base_unit,
                "display_value": to_display_value(definition, value),
                "display_minimum": (
                    None
                    if definition.minimum is None
                    else definition.minimum / definition.display_scale
                ),
                "display_maximum": (
                    None
                    if definition.maximum is None
                    else definition.maximum / definition.display_scale
                ),
                "choices": list(definition.choices),
                "choice_labels": {
                    str(value): label for value, label in definition.choice_labels
                },
                "source": source,
                "from_environment": from_environment,
                "environment_overridden": from_environment and source == "database",
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
                # 与写入路径共用 is_sensitive_key：写入已经脱敏，这里再判一次是为了
                # 覆盖历史记录——改造之前写下的审计行里仍是原值。
                sensitive = is_sensitive_key(key, self.registry)
                changes.append(
                    {
                        "key": key,
                        "name": definition.name if definition else key,
                        "old_value": REDACTED_TEXT if sensitive else old_values.get(key),
                        "new_value": REDACTED_TEXT if sensitive else new_values.get(key),
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
        route_keys = ("ANALYSIS_PROVIDER", "NARRATION_PROVIDER", "PANEL_PROVIDER")
        referenced = {
            name
            for key in route_keys
            if key in normalized
            for name in str(normalized[key]).split(";")
            if name
        }
        if referenced:
            placeholders = ",".join("?" for _ in referenced)
            with database_connection(self.repository.database_path, read_only=True) as connection:
                rows = connection.execute(
                    f"SELECT name FROM model_providers WHERE is_enabled=1 "
                    f"AND name IN ({placeholders})",
                    tuple(sorted(referenced)),
                ).fetchall()
            available = {str(row["name"]) for row in rows}
            for key in route_keys:
                if key not in normalized:
                    continue
                missing = [
                    name for name in str(normalized[key]).split(";")
                    if name and name not in available
                ]
                if missing:
                    errors[key] = f"厂商不存在或未启用: {', '.join(missing)}"
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
