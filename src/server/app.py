"""InkTime Web 服务的真正 Flask 应用工厂。"""

from __future__ import annotations

import importlib
import mimetypes
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, current_app, g

from src.configuration import (
    IMAGE_DIR_SEPARATOR,
    SETTING_REGISTRY,
    ConfigurationService,
    bounded_int,
    parse_image_dirs,
)
from src.database import connect_database
from src.migrations import assert_current_schema

from .admin_jobs import AdminJobRepository, AdminJobService, LibraryScanService, UploadService
from .auth import AuthenticationService, register_authentication
from .blueprints import admin_api_blueprint, admin_page_blueprint, public_blueprint
from .errors import register_error_handlers
from .extensions import csrf, login_manager
from .formatting import readable_time
from .photo_management import AdminPhotoManagementService
from .photo_lifecycle import (
    DisplayArtifactGuard,
    MaintenanceJobRepository,
    MaintenanceJobService,
    PhotoLifecycleService,
)
from .repositories import AdminUserRepository, PhotoManagementRepository, PhotoRepository
from .services import (
    AdminPhotoService,
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
APP_ENVIRONMENTS = ("development", "testing", "production")


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
    app_environment = _environment_string("APP_ENV", "development").strip().lower()
    rotate_mode = _environment_string("DISPLAY_ROTATE_MODE", "interval").strip().lower()
    if rotate_mode not in ROTATE_MODES:
        rotate_mode = "interval"
    display_template = _environment_string("DISPLAY_TEMPLATE", "classic").strip().lower()
    if display_template not in DISPLAY_TEMPLATES:
        display_template = "classic"
    return {
        "APP_ENV": app_environment,
        # 开发环境下改动模板即时生效，生产保持缓存以免每次请求都做磁盘检查。
        "TEMPLATES_AUTO_RELOAD": app_environment == "development",
        "SECRET_KEY": _environment_string("SECRET_KEY", ""),
        "SESSION_COOKIE_HTTPONLY": _environment_boolean("SESSION_COOKIE_HTTPONLY", True),
        "SESSION_COOKIE_SAMESITE": _environment_string("SESSION_COOKIE_SAMESITE", "Lax"),
        "SESSION_COOKIE_SECURE": _environment_boolean(
            "SESSION_COOKIE_SECURE", app_environment == "production"
        ),
        "PERMANENT_SESSION_LIFETIME": timedelta(
            seconds=max(1, _environment_integer("PERMANENT_SESSION_LIFETIME", 28800))
        ),
        "WTF_CSRF_TIME_LIMIT": max(
            1, _environment_integer("WTF_CSRF_TIME_LIMIT", 3600)
        ),
        "WTF_CSRF_CHECK_DEFAULT": False,
        "ADMIN_LOGIN_MAX_FAILURES": max(
            1, _environment_integer("ADMIN_LOGIN_MAX_FAILURES", 5)
        ),
        "ADMIN_LOGIN_FAILURE_WINDOW_SECONDS": max(
            1, _environment_integer("ADMIN_LOGIN_FAILURE_WINDOW_SECONDS", 300)
        ),
        "PROJECT_NAME": _environment_string("PROJECT_NAME", "InkTime 相册"),
        "DB_PATH": _absolute_path(_environment_string("DB_PATH", "./data/photos.db")),
        "IMAGE_DIR": _absolute_path(_environment_string("IMAGE_DIR", "./data/photos")),
        "BIN_OUTPUT_DIR": _absolute_path(_environment_string("BIN_OUTPUT_DIR", "./data/output")),
        "DOWNLOAD_KEY": _environment_string("DOWNLOAD_KEY", ""),
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
        "PANEL_AI_MODEL": _environment_string("PANEL_AI_MODEL", ""),
        "UPLOAD_MAX_FILES": min(10, max(1, _environment_integer("UPLOAD_MAX_FILES", 10))),
        "UPLOAD_MAX_BYTES": min(104857600, max(1, _environment_integer("UPLOAD_MAX_BYTES", 67108864))),
        "UPLOAD_MAX_PIXELS": min(80_000_000, max(1, _environment_integer("UPLOAD_MAX_PIXELS", 80_000_000))),
        "JOB_MAX_ATTEMPTS": min(3, max(1, _environment_integer("JOB_MAX_ATTEMPTS", 3))),
        "JOB_LEASE_SECONDS": max(1, _environment_integer("JOB_LEASE_SECONDS", 120)),
        "JOB_RENEW_SECONDS": max(1, _environment_integer("JOB_RENEW_SECONDS", 30)),
        "JOB_POLL_SECONDS": max(0.1, _environment_float("JOB_POLL_SECONDS", 2.0)),
        "TRASH_RETENTION_DAYS": _environment_integer("TRASH_RETENTION_DAYS", 30),
    }


def _duration(value: Any, key: str) -> timedelta:
    """把秒数或 timedelta 规范化为 Flask 可直接使用的持续时间。"""
    if isinstance(value, timedelta):
        return value
    try:
        return timedelta(seconds=max(1, int(value)))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{key} 必须是正整数秒数") from error


def _positive_seconds(value: Any, key: str) -> int:
    """把秒数或 timedelta 规范化为扩展可直接使用的正整数秒数。"""
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
    else:
        try:
            seconds = int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{key} 必须是正整数秒数") from error
    if seconds < 1:
        raise RuntimeError(f"{key} 必须是正整数秒数")
    return seconds


def _normalize_security_config(app: Flask) -> None:
    """拒绝不受支持或不安全的部署配置，并规范化会话安全值。"""
    app_environment = str(app.config["APP_ENV"]).strip().lower()
    if app_environment not in APP_ENVIRONMENTS:
        raise RuntimeError(
            "APP_ENV 只允许 development、testing、production"
        )
    app.config["APP_ENV"] = app_environment

    secret_key = str(app.config.get("SECRET_KEY") or "").strip()
    if app_environment == "production" and not secret_key:
        raise RuntimeError("生产环境必须配置非空 SECRET_KEY")
    app.config["SECRET_KEY"] = secret_key or secrets.token_urlsafe(48)

    secure_cookie_value = app.config["SESSION_COOKIE_SECURE"]
    if isinstance(secure_cookie_value, str):
        normalized_secure_cookie = secure_cookie_value.strip().lower()
        if normalized_secure_cookie in {"1", "true", "yes", "on"}:
            secure_cookie = True
        elif normalized_secure_cookie in {"", "0", "false", "no", "off"}:
            secure_cookie = False
        else:
            raise RuntimeError("SESSION_COOKIE_SECURE 必须是布尔值")
    else:
        secure_cookie = bool(secure_cookie_value)
    if app_environment == "production" and not secure_cookie:
        raise RuntimeError("生产环境 SESSION_COOKIE_SECURE 必须为 True")
    app.config["SESSION_COOKIE_SECURE"] = secure_cookie

    same_site = str(app.config["SESSION_COOKIE_SAMESITE"]).strip().capitalize()
    if same_site not in {"Lax", "Strict", "None"}:
        raise RuntimeError("SESSION_COOKIE_SAMESITE 必须是 Lax、Strict 或 None")
    app.config["SESSION_COOKIE_SAMESITE"] = same_site
    app.config["SESSION_COOKIE_HTTPONLY"] = bool(app.config["SESSION_COOKIE_HTTPONLY"])
    app.config["SESSION_COOKIE_SECURE"] = bool(app.config["SESSION_COOKIE_SECURE"])
    app.config["PERMANENT_SESSION_LIFETIME"] = _duration(
        app.config["PERMANENT_SESSION_LIFETIME"], "PERMANENT_SESSION_LIFETIME"
    )
    app.config["WTF_CSRF_TIME_LIMIT"] = _positive_seconds(
        app.config["WTF_CSRF_TIME_LIMIT"], "WTF_CSRF_TIME_LIMIT"
    )
    app.config["ADMIN_LOGIN_MAX_FAILURES"] = max(
        1, int(app.config["ADMIN_LOGIN_MAX_FAILURES"])
    )
    app.config["ADMIN_LOGIN_FAILURE_WINDOW_SECONDS"] = max(
        1, int(app.config["ADMIN_LOGIN_FAILURE_WINDOW_SECONDS"])
    )

    download_key = str(app.config.get("DOWNLOAD_KEY") or "").strip()
    if app_environment == "production" and (
        not download_key or download_key == "inktime" or len(download_key) < 24
    ):
        raise RuntimeError("生产环境 DOWNLOAD_KEY 必须是至少 24 个字符的随机值")
    app.config["DOWNLOAD_KEY"] = download_key

    upload_limits = (
        ("UPLOAD_MAX_FILES", 1, 10),
        ("UPLOAD_MAX_BYTES", 1, 104857600),
        ("UPLOAD_MAX_PIXELS", 1, 80_000_000),
    )
    for key, minimum, maximum in upload_limits:
        try:
            value = int(app.config[key])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{key} 必须是整数") from error
        app.config[key] = min(maximum, max(minimum, value))
    app.config["MAX_CONTENT_LENGTH"] = (
        app.config["UPLOAD_MAX_FILES"] * app.config["UPLOAD_MAX_BYTES"]
        + 1024 * 1024
    )

    try:
        job_max_attempts = int(app.config["JOB_MAX_ATTEMPTS"])
        job_lease_seconds = int(app.config["JOB_LEASE_SECONDS"])
        job_renew_seconds = int(app.config["JOB_RENEW_SECONDS"])
        job_poll_seconds = float(app.config["JOB_POLL_SECONDS"])
    except (TypeError, ValueError) as error:
        raise RuntimeError("后台任务配置必须是数字") from error
    if not 1 <= job_max_attempts <= 3:
        raise RuntimeError("JOB_MAX_ATTEMPTS 必须在 1 到 3 之间")
    if job_lease_seconds < 1 or job_renew_seconds < 1 or job_poll_seconds <= 0:
        raise RuntimeError("后台任务租约、续租和轮询间隔必须为正数")
    if job_renew_seconds >= job_lease_seconds:
        raise RuntimeError("JOB_RENEW_SECONDS 必须小于 JOB_LEASE_SECONDS")
    app.config["JOB_MAX_ATTEMPTS"] = job_max_attempts
    app.config["JOB_LEASE_SECONDS"] = job_lease_seconds
    app.config["JOB_RENEW_SECONDS"] = job_renew_seconds
    app.config["JOB_POLL_SECONDS"] = job_poll_seconds
    try:
        retention_days = int(app.config["TRASH_RETENTION_DAYS"])
    except (TypeError, ValueError) as error:
        raise RuntimeError("TRASH_RETENTION_DAYS 必须是整数") from error
    if not 1 <= retention_days <= 3650:
        raise RuntimeError("TRASH_RETENTION_DAYS 必须在 1 到 3650 之间")
    app.config["TRASH_RETENTION_DAYS"] = retention_days


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


def _configuration_initial_values(app: Flask) -> dict[str, Any]:
    """提取注册表内的启动配置，并转换路径与持续时间为可校验标量。

    Flask 配置只覆盖 Web 关心的配置项，分析与渲染类配置只存在于进程环境中。
    两者按「Flask 配置优先、进程环境兜底」合并，避免后台配置页把这些项显示成
    注册表默认值，进而在保存时把 `.env` 中的真实值覆盖掉。
    """
    values: dict[str, Any] = {}
    for key in SETTING_REGISTRY:
        if key in os.environ:
            values[key] = os.environ[key]
    for key in SETTING_REGISTRY:
        if key not in app.config:
            continue
        value = app.config[key]
        if isinstance(value, timedelta):
            value = int(value.total_seconds())
        elif isinstance(value, Path):
            value = str(value)
        values[key] = value
    return values


def _register_services(app: Flask, gallery_module: Any | None, panel_module: Any | None) -> None:
    """为单个应用实例创建并注册 Repository、统一配置与 Service 对象。"""
    # 统一配置服务先于其余服务创建：任务、上传、生命周期与目录浏览都要注入它，
    # 才能在方法内按需取值，而不是把上限与开关冻结在构造参数上。
    configuration_service = ConfigurationService(
        app.config["DB_PATH"], environment=_configuration_initial_values(app)
    )
    photo_repository = PhotoRepository(get_database)
    photo_management_repository = PhotoManagementRepository(get_database)
    admin_user_repository = AdminUserRepository(get_database)
    photo_service = PhotoService(photo_repository, app.config["DB_PATH"])
    media_service = MediaService(
        app.config["IMAGE_DIR"],
        photo_repository.is_visible_path,
        configuration_service=configuration_service,
        # 与数据库同级的 data/cache 下，既在照片目录之外，也随项目数据一起备份或清理
        cache_directory=Path(app.config["DB_PATH"]).parent / "cache" / "thumbnails",
    )
    admin_job_repository = AdminJobRepository(
        app.config["DB_PATH"],
        app.config["JOB_MAX_ATTEMPTS"],
        configuration_service=configuration_service,
    )
    photo_job_service = AdminJobService(admin_job_repository)
    maintenance_repository = MaintenanceJobRepository(
        app.config["DB_PATH"],
        app.config["JOB_MAX_ATTEMPTS"],
        configuration_service=configuration_service,
    )
    lifecycle_service = PhotoLifecycleService(
        app.config["DB_PATH"],
        app.config["IMAGE_DIR"],
        maintenance_repository,
        photo_service.invalidate_date_cache,
        app.config["TRASH_RETENTION_DAYS"],
        configuration_service=configuration_service,
    )
    recovered_operations = lifecycle_service.recover_incomplete_operations()
    app.logger.info(
        "Photo lifecycle operation recovery completed, recovered_count=[%s]",
        recovered_operations,
    )
    artifact_guard = DisplayArtifactGuard(app.config["DB_PATH"])
    app.extensions["inktime_services"] = {
        "photo": photo_service,
        "admin_photo": AdminPhotoService(photo_repository, media_service),
        "admin_photo_management": AdminPhotoManagementService(
            photo_management_repository,
            photo_service.invalidate_date_cache,
        ),
        "photo_lifecycle": lifecycle_service,
        "auth": AuthenticationService(
            admin_user_repository,
            app.config["DB_PATH"],
            app.config["SECRET_KEY"],
            app.config["ADMIN_LOGIN_MAX_FAILURES"],
            app.config["ADMIN_LOGIN_FAILURE_WINDOW_SECONDS"],
        ),
        "configuration": configuration_service,
        "config": ConfigService(configuration_service),
        "media": media_service,
        "display": DisplayService(
            gallery_module, app.config["DB_PATH"], configuration_service
        ),
        "panel": PanelService(panel_module, configuration_service),
        "render": RenderService(_load_render_module(app)),
        "device": DeviceService(
            app.config["BIN_OUTPUT_DIR"],
            app.config["DOWNLOAD_KEY"],
            app.config["DAILY_PHOTO_QUANTITY"],
            artifact_guard.blocked,
        ),
        "files": FileBrowserService(
            app.config["BIN_OUTPUT_DIR"],
            app.config["ENABLE_FILE_BROWSER"],
            app.config["ENABLE_REVIEW_WEBUI"],
            artifact_guard.blocked,
            configuration_service=configuration_service,
        ),
        "photo_jobs": photo_job_service,
        "admin_jobs": MaintenanceJobService(
            app.config["DB_PATH"], maintenance_repository, photo_job_service
        ),
        "uploads": UploadService(
            app.config["IMAGE_DIR"], admin_job_repository,
            app.config["UPLOAD_MAX_FILES"], app.config["UPLOAD_MAX_BYTES"],
            app.config["UPLOAD_MAX_PIXELS"],
            configuration_service=configuration_service,
        ),
        "library_scan": LibraryScanService(
            app.config["IMAGE_DIR"], app.config["DB_PATH"], app.config["JOB_MAX_ATTEMPTS"],
            configuration_service=configuration_service,
        ),
    }


def _register_request_limit_sync(app: Flask) -> None:
    """每次请求开始时按当前生效配置同步 Werkzeug 的请求体上限。

    `MAX_CONTENT_LENGTH` 由 Werkzeug 在解析请求体时读取，属于派生值而非注册表
    配置项。若只改数据库里的上传上限，请求仍会被应用启动时算出的旧上限拦截，
    因此这里在每次请求前重算，使上传上限真正做到改完即生效。

    Args:
        app: 已注册统一配置服务的应用实例。
    """

    @app.before_request
    def sync_max_content_length() -> None:
        """按当前上传上限重算允许的最大请求体字节数。"""
        configuration = app.extensions["inktime_services"]["configuration"]
        limits = configuration.get_many(("UPLOAD_MAX_FILES", "UPLOAD_MAX_BYTES"))
        max_files = bounded_int(limits["UPLOAD_MAX_FILES"], 1, 10, 10)
        max_bytes = bounded_int(
            limits["UPLOAD_MAX_BYTES"], 1, 104857600, 67108864
        )
        app.config["MAX_CONTENT_LENGTH"] = max_files * max_bytes + 1024 * 1024


def create_app(config_overrides: Mapping[str, Any] | None = None) -> Flask:
    """创建并完整初始化一个全新的 Flask 应用实例。

    配置先从已有环境变量、项目根目录 .env 和代码默认值合并，再应用显式覆盖；
    覆盖能力用于临时数据库或嵌入式验证，不会修改环境或活动数据库。

    Args:
        config_overrides: 可选的 Flask 配置覆盖映射。

    Returns:
        已通过数据库结构门禁，并注册数据库生命周期、Service、认证、Blueprint 和错误处理器的新应用。
    """
    _load_environment_file()
    app = Flask(
        __name__,
        template_folder=str(SERVER_DIRECTORY / "templates"),
        static_folder=str(SERVER_DIRECTORY / "static"),
        static_url_path="/static",
    )
    app.config.from_mapping(
        {
            key: os.environ.get(key, definition.default)
            for key, definition in SETTING_REGISTRY.items()
        }
    )
    app.config.from_mapping(_default_config())
    if config_overrides:
        app.config.from_mapping(config_overrides)
    _normalize_security_config(app)
    for key in ("DB_PATH", "BIN_OUTPUT_DIR"):
        app.config[key] = _absolute_path(app.config[key])
    # 照片目录支持分号分隔的多个根：这里统一规范化为绝对路径列表并做结构校验，
    # 存在性不在启动时强校验，避免网络存储尚未挂载导致服务起不来。
    try:
        image_dirs = parse_image_dirs(
            app.config["IMAGE_DIR"], base_dir=PROJECT_ROOT
        )
    except ValueError as error:
        raise RuntimeError(f"IMAGE_DIR 配置无效: {error}") from error
    app.config["IMAGE_DIRS"] = image_dirs
    app.config["IMAGE_DIR"] = IMAGE_DIR_SEPARATOR.join(str(item) for item in image_dirs)
    assert_current_schema(app.config["DB_PATH"])
    app.config["DISPLAY_ROTATE_INTERVAL_SEC"] = max(1, int(app.config["DISPLAY_ROTATE_INTERVAL_SEC"]))
    app.config["DISPLAY_UI_HIDE_DELAY_SEC"] = max(0, int(app.config["DISPLAY_UI_HIDE_DELAY_SEC"]))
    if app.config["DISPLAY_TEMPLATE"] not in DISPLAY_TEMPLATES:
        app.config["DISPLAY_TEMPLATE"] = "classic"

    login_manager.session_protection = "strong"
    login_manager.init_app(app)
    csrf.init_app(app)
    csrf.exempt(public_blueprint)

    app.config["BIN_OUTPUT_DIR"].mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("application/octet-stream", ".bin")
    app.teardown_appcontext(_close_database)

    # 展示层格式化：数据库里的时间保持原值，只在渲染时转成中文常见读法
    app.jinja_env.filters["readable_time"] = readable_time

    gallery_module = _load_server_module("gallery", app)
    panel_module = _load_server_module("panel", app)
    _register_services(app, gallery_module, panel_module)
    _register_request_limit_sync(app)
    register_authentication(app)
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
