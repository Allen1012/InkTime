"""InkTime Web 服务的真正 Flask 应用工厂。"""

from __future__ import annotations

import importlib
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, current_app, g

from src.database import connect_database

from .blueprints import admin_api_blueprint, admin_page_blueprint, public_blueprint
from .errors import register_error_handlers
from .repositories import PhotoRepository
from .services import (
    ConfigService,
    DeviceService,
    DisplayService,
    FileBrowserService,
    MediaService,
    PanelService,
    PhotoService,
    RenderService,
)


SERVER_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIRECTORY.parent.parent
ROTATE_MODES = ("interval", "hourly", "minutely", "daily", "off")
DISPLAY_TEMPLATES = ("classic", "dashboard")


def _environment_string(key: str, default: str) -> str:
    """读取非空环境变量字符串。"""
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _environment_boolean(key: str, default: bool) -> bool:
    """读取常见真值形式的布尔环境变量。"""
    value = os.environ.get(key)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _environment_integer(key: str, default: int) -> int:
    """读取整数环境变量，格式错误时使用默认值。"""
    try:
        return int(str(os.environ.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _environment_float(key: str, default: float) -> float:
    """读取浮点环境变量，格式错误时使用默认值。"""
    try:
        return float(str(os.environ.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _absolute_path(value: str | Path) -> Path:
    """把相对路径按项目根目录解析为绝对路径。"""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _load_environment_file() -> None:
    """在工厂调用期间加载项目根目录 .env，且不覆盖已有环境变量。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    environment_file = PROJECT_ROOT / ".env"
    if environment_file.exists():
        load_dotenv(environment_file, override=False)


def _default_config() -> dict[str, Any]:
    """从当前环境构造一份新的 Flask 配置映射。"""
    rotate_mode = _environment_string("DISPLAY_ROTATE_MODE", "interval").strip().lower()
    if rotate_mode not in ROTATE_MODES:
        rotate_mode = "interval"
    display_template = _environment_string("DISPLAY_TEMPLATE", "classic").strip().lower()
    if display_template not in DISPLAY_TEMPLATES:
        display_template = "classic"
    return {
        "PROJECT_NAME": _environment_string("PROJECT_NAME", "InkTime 相册"),
        "DB_PATH": _absolute_path(_environment_string("DB_PATH", "./data/photos.db")),
        "IMAGE_DIR": _absolute_path(_environment_string("IMAGE_DIR", "./data/photos")),
        "BIN_OUTPUT_DIR": _absolute_path(_environment_string("BIN_OUTPUT_DIR", "./data/output")),
        "DOWNLOAD_KEY": _environment_string("DOWNLOAD_KEY", "inktime"),
        "FLASK_HOST": _environment_string("FLASK_HOST", "0.0.0.0"),
        "FLASK_PORT": _environment_integer("FLASK_PORT", 5005),
        "DAILY_PHOTO_QUANTITY": _environment_integer("DAILY_PHOTO_QUANTITY", 5),
        "ENABLE_REVIEW_WEBUI": _environment_boolean("ENABLE_REVIEW_WEBUI", True),
        "ENABLE_FILE_BROWSER": _environment_boolean("ENABLE_FILE_BROWSER", False),
        "DISPLAY_ROTATE_MODE": rotate_mode,
        "DISPLAY_ROTATE_INTERVAL_SEC": max(1, _environment_integer("DISPLAY_ROTATE_INTERVAL_SEC", 60)),
        "DISPLAY_KEEP_AWAKE": _environment_boolean("DISPLAY_KEEP_AWAKE", True),
        "DISPLAY_UI_HIDE_DELAY_SEC": max(0, _environment_integer("DISPLAY_UI_HIDE_DELAY_SEC", 3)),
        "DISPLAY_TEMPLATE": display_template,
        "ONTHISDAY_COUNT": max(1, _environment_integer("ONTHISDAY_COUNT", 2)),
        "ONTHISDAY_STRATEGY": _environment_string("ONTHISDAY_STRATEGY", "curated"),
        "ONTHISDAY_MIN_YEAR": _environment_integer("ONTHISDAY_MIN_YEAR", 1900),
        "DISPLAY_MIN_SCORE": _environment_float("DISPLAY_MIN_SCORE", 70.0),
        "DISPLAY_NEW_PHOTO_WEIGHT": _environment_float("DISPLAY_NEW_PHOTO_WEIGHT", 3.0),
    }


def get_database():
    """返回当前请求复用的统一 SQLite 连接。

    Returns:
        当前请求的 SQLite 连接，首次调用时按应用 DB_PATH 创建。
    """
    if "inktime_database" not in g:
        g.inktime_database = connect_database(current_app.config["DB_PATH"])
    return g.inktime_database


def _close_database(_exception: BaseException | None = None) -> None:
    """请求结束时统一关闭可能创建的 SQLite 连接。"""
    connection = g.pop("inktime_database", None)
    if connection is not None:
        connection.close()


def _load_server_module(name: str, app: Flask) -> Any | None:
    """在工厂初始化阶段加载可降级的服务器模块，并记录失败上下文。"""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except Exception:
        app.logger.exception("Optional server module load failed, module=[%s]", name)
        return None


def _load_render_module(app: Flask) -> Any | None:
    """在工厂初始化阶段加载历史渲染模块，失败时记录日志并允许降级。"""
    render_directory = PROJECT_ROOT / "src" / "render"
    inserted = str(render_directory) not in sys.path
    if inserted:
        sys.path.insert(0, str(render_directory))
    try:
        return importlib.import_module("render_daily_photo")
    except Exception:
        app.logger.exception("Optional render module load failed, module=[render_daily_photo]")
        return None
    finally:
        if inserted:
            sys.path.remove(str(render_directory))


def _register_services(app: Flask, gallery_module: Any | None, panel_module: Any | None) -> None:
    """为单个应用实例创建并注册 Repository 与 Service 对象。"""
    repository = PhotoRepository(get_database)
    app.extensions["inktime_services"] = {
        "photo": PhotoService(repository, app.config["DB_PATH"]),
        "config": ConfigService(app.config),
        "media": MediaService(app.config["IMAGE_DIR"]),
        "display": DisplayService(gallery_module, app.config["DB_PATH"], app.config["DISPLAY_TEMPLATE"]),
        "panel": PanelService(panel_module),
        "render": RenderService(_load_render_module(app)),
        "device": DeviceService(app.config["BIN_OUTPUT_DIR"], app.config["DOWNLOAD_KEY"], app.config["DAILY_PHOTO_QUANTITY"]),
        "files": FileBrowserService(app.config["BIN_OUTPUT_DIR"], app.config["ENABLE_FILE_BROWSER"], app.config["ENABLE_REVIEW_WEBUI"]),
    }


def create_app(config_overrides: Mapping[str, Any] | None = None) -> Flask:
    """创建并完整初始化一个全新的 Flask 应用实例。

    配置先从已有环境变量、项目根目录 .env 和代码默认值合并，再应用显式覆盖；
    覆盖能力用于临时数据库或嵌入式验证，不会修改环境或活动数据库。

    Args:
        config_overrides: 可选的 Flask 配置覆盖映射。

    Returns:
        已注册数据库生命周期、Service、Blueprint 和错误处理器的新应用。
    """
    _load_environment_file()
    app = Flask(
        __name__,
        template_folder=str(SERVER_DIRECTORY / "templates"),
        static_folder=str(SERVER_DIRECTORY / "static"),
        static_url_path="/static",
    )
    app.config.from_mapping(_default_config())
    if config_overrides:
        app.config.from_mapping(config_overrides)
    for key in ("DB_PATH", "IMAGE_DIR", "BIN_OUTPUT_DIR"):
        app.config[key] = _absolute_path(app.config[key])
    app.config["DISPLAY_ROTATE_INTERVAL_SEC"] = max(1, int(app.config["DISPLAY_ROTATE_INTERVAL_SEC"]))
    app.config["DISPLAY_UI_HIDE_DELAY_SEC"] = max(0, int(app.config["DISPLAY_UI_HIDE_DELAY_SEC"]))
    if app.config["DISPLAY_TEMPLATE"] not in DISPLAY_TEMPLATES:
        app.config["DISPLAY_TEMPLATE"] = "classic"

    app.config["BIN_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("application/octet-stream", ".bin")
    app.teardown_appcontext(_close_database)

    gallery_module = _load_server_module("gallery", app)
    panel_module = _load_server_module("panel", app)
    if panel_module is not None:
        panel_module.configure(
            count=app.config["ONTHISDAY_COUNT"],
            strategy=app.config["ONTHISDAY_STRATEGY"],
            min_year=app.config["ONTHISDAY_MIN_YEAR"],
        )
    if gallery_module is not None:
        gallery_module.configure(
            min_score=app.config["DISPLAY_MIN_SCORE"],
            new_photo_weight=app.config["DISPLAY_NEW_PHOTO_WEIGHT"],
        )
    _register_services(app, gallery_module, panel_module)
    app.register_blueprint(public_blueprint)
    app.register_blueprint(admin_page_blueprint)
    app.register_blueprint(admin_api_blueprint)
    register_error_handlers(app)

    app.logger.info(
        "InkTime application created, host=[%s], port=[%s], webui=[%s], file_browser=[%s]",
        app.config["FLASK_HOST"],
        app.config["FLASK_PORT"],
        app.config["ENABLE_REVIEW_WEBUI"],
        app.config["ENABLE_FILE_BROWSER"],
    )
    return app
