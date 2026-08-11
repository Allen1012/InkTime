"""公开页面、接口、媒体和设备行为的 Service 层。"""

from __future__ import annotations

import html
import io
import json
import mimetypes
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from src.configuration import (
    PROJECT_ROOT,
    TRASH_DIRECTORY_NAME,
    bounded_boolean,
    current_setting,
    parse_image_dirs,
)

from .errors import ParameterError, PermissionDeniedError, ResourceNotFoundError
from .repositories import PhotoRepository


@dataclass(frozen=True)
class BinaryContent:
    """描述由 Blueprint 转换为响应的内存二进制内容。"""

    data: bytes
    mimetype: str


@dataclass(frozen=True)
class FileContent:
    """描述由 Blueprint 发送的磁盘文件及媒体类型。"""

    path: Path
    mimetype: str | None = None


class PhotoService:
    """承接公开照片查询、序列化、分类聚合和日期清单。"""

    def __init__(self, repository: PhotoRepository, database_path: Path) -> None:
        """初始化照片服务。

        Args:
            repository: 照片查询仓储。
            database_path: 当前应用使用的数据库路径，用于隔离日期缓存。
        """
        self._repository = repository
        self._database_path = database_path
        self._date_cache: dict[str, Any] = {}

    @staticmethod
    def _pagination(page: int, limit: int) -> None:
        """校验分页参数为正整数。"""
        if page < 1 or limit < 1:
            raise ParameterError("page 和 limit 必须为正整数")

    @staticmethod
    def _list_item(row: Mapping[str, Any]) -> dict[str, Any]:
        """把数据库行序列化为兼容的照片列表字段。"""
        path = row["path"] or ""
        return {
            "id": row["id"],
            "path": path,
            "title": path.split("/")[-1],
            "description": row["caption"],
            "date_taken": row["exif_datetime"],
            "location": row["exif_city"],
            "thumbnail_url": f"/api/photo/thumbnail?path={path}",
            "full_url": f"/api/photo/full?path={path}",
            "side_caption": row["side_caption"],
            "memory_score": row["memory_score"],
            "beauty_score": row["beauty_score"],
            "category": row["type"],
        }

    def list_photos(self, page: int, photo_filter: str, sort: str, limit: int) -> dict[str, Any]:
        """返回兼容现有公开接口的分页照片列表。

        Args:
            page: 从 1 开始的页码。
            photo_filter: 分类筛选值。
            sort: latest、oldest、memory 或 beauty。
            limit: 每页数量。

        Returns:
            包含 items、total、page 和 limit 的字典。
        """
        self._pagination(page, limit)
        if sort not in ("latest", "oldest", "memory", "beauty"):
            raise ParameterError("不支持的排序方式")
        rows, total = self._repository.list_photos(page, photo_filter, sort, limit)
        return {"items": [self._list_item(row) for row in rows], "total": total, "page": page, "limit": limit}

    def search(self, query: str, page: int, limit: int) -> dict[str, Any]:
        """搜索并序列化照片。

        Args:
            query: 搜索词。
            page: 从 1 开始的页码。
            limit: 每页数量。

        Returns:
            兼容现有接口的分页搜索结果。
        """
        self._pagination(page, limit)
        rows, total = self._repository.search_photos(query, page, limit)
        return {"items": [self._list_item(row) for row in rows], "total": total, "page": page, "limit": limit}

    def category_stats(self) -> dict[str, Any]:
        """拆分复合分类并返回分类统计。

        Returns:
            包含照片总数和分类列表的字典。
        """
        rows, total = self._repository.list_category_counts()
        counts: dict[str, int] = {}
        for row in rows:
            tags = re.split(r"[/，,、|；;]+", str(row["type"] or ""))
            for tag in dict.fromkeys(item.strip() for item in tags if item.strip()):
                counts[tag] = counts.get(tag, 0) + row["count"]
        categories = [
            {"id": name, "name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
        ]
        return {"total": total, "categories": categories}

    def category_photos(self, category: str, page: int, limit: int) -> dict[str, Any]:
        """返回指定分类的分页照片。

        Args:
            category: 分类名称，all 表示全部。
            page: 从 1 开始的页码。
            limit: 每页数量。

        Returns:
            兼容现有接口的分页结果。
        """
        self._pagination(page, limit)
        rows, total = self._repository.list_category_photos(category, page, limit)
        return {"items": [self._list_item(row) for row in rows], "total": total, "page": page, "limit": limit}

    def detail(self, photo_id: int) -> dict[str, Any]:
        """返回照片详情并保持现有中文 EXIF 键。

        Args:
            photo_id: 照片自增编号。

        Returns:
            兼容现有详情接口的照片字典。
        """
        row = self._repository.get_photo(photo_id)
        if row is None:
            raise ResourceNotFoundError("照片不存在")
        path = row["path"] or ""
        return {
            "id": photo_id,
            "path": path,
            "title": path.split("/")[-1],
            "description": row["caption"],
            "date_taken": row["exif_datetime"],
            "location": row["exif_city"],
            "category": row["type"],
            "camera": f"{row['exif_make']} {row['exif_model']}" if row["exif_make"] and row["exif_model"] else "未知",
            "resolution": f"{row['width']} x {row['height']}" if row["width"] and row["height"] else "未知",
            "image_url": f"/api/photo/full?path={path}",
            "memory_score": row["memory_score"],
            "beauty_score": row["beauty_score"],
            "score_reason": row["reason"],
            "exif_data": {
                "相机厂商": row["exif_make"] or "未知",
                "相机型号": row["exif_model"] or "未知",
                "焦距": f"{row['exif_focal_length']}mm" if row["exif_focal_length"] else "未知",
                "光圈": f"f/{row['exif_f_number']}" if row["exif_f_number"] else "未知",
                "快门速度": f"{row['exif_exposure_time']}s" if row["exif_exposure_time"] else "未知",
                "ISO": row["exif_iso"] or "未知",
                "拍摄时间": row["exif_datetime"] or "未知",
                "GPS 纬度": row["exif_gps_lat"] or "未知",
                "GPS 经度": row["exif_gps_lon"] or "未知",
                "GPS 海拔": f"{row['exif_gps_alt']}m" if row["exif_gps_alt"] else "未知",
            },
            "side_caption": row["side_caption"],
        }

    def date_list(self) -> list[str]:
        """从规范拍摄时间提取去重后的月日清单并做实例级缓存。

        Returns:
            排序后的 MM-DD 字符串列表。
        """
        if not self._database_path.exists():
            return []
        now = time.time()
        if now - float(self._date_cache.get("built_at", 0.0)) < 3600:
            return list(self._date_cache.get("items", []))
        values: set[str] = set()
        for row in self._repository.list_photo_dates():
            date_value = str(row["exif_datetime"] or "")
            if len(date_value) >= 10 and date_value[4] in (":", "-"):
                values.add(date_value[5:10].replace(":", "-"))
        items = sorted(values)
        self._date_cache = {"built_at": now, "items": items}
        return items

    def invalidate_date_cache(self) -> None:
        """照片拍摄日期提交成功后清除实例级月日缓存。"""
        self._date_cache.clear()

    def random_day(self) -> str:
        """从现有日期清单中随机选择一天。

        Returns:
            一个 MM-DD 日期。
        """
        items = self.date_list()
        if not items:
            raise ResourceNotFoundError("没有找到照片")
        return random.choice(items)


class AdminPhotoService:
    """提供阶段三后台统计、筛选列表和只读详情展示模型。"""

    MAX_PAGE_SIZE = 100
    SUPPORTED_SORTS = {"latest", "oldest", "memory", "beauty"}
    SUPPORTED_VIEWS = {"grid", "table"}
    SUPPORTED_ANALYSIS_STATUSES = {
        "legacy", "pending", "running", "succeeded", "failed"
    }

    def __init__(self, repository: PhotoRepository, media_service: "MediaService") -> None:
        """初始化后台照片服务。

        Args:
            repository: 复用请求级连接的照片仓储。
            media_service: 负责照片路径边界和文件存在性判断的媒体服务。
        """
        self._repository = repository
        self._media_service = media_service

    @staticmethod
    def _statistic(name: str, loader: Any) -> dict[str, Any]:
        """独立加载一个首页统计，数据库异常时只降级当前卡片。"""
        import logging
        import sqlite3

        try:
            return {"available": True, "data": loader()}
        except sqlite3.Error:
            logging.getLogger(__name__).exception(
                "Admin dashboard statistic unavailable, statistic=[%s]", name
            )
            return {"available": False, "data": None}

    @staticmethod
    def _categories(rows: Any) -> list[dict[str, Any]]:
        """把历史复合分类拆分为可筛选的去重标签。"""
        counts: dict[str, int] = {}
        for row in rows:
            tags = re.split(r"[/，,、|；;]+", str(row["type"] or ""))
            for tag in dict.fromkeys(item.strip() for item in tags if item.strip()):
                counts[tag] = counts.get(tag, 0) + int(row["count"])
        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def dashboard(self) -> dict[str, dict[str, Any]]:
        """返回可独立降级的后台首页统计卡片。"""
        return {
            "total": self._statistic("total", self._repository.count_admin_photos),
            "scores": self._statistic(
                "scores", lambda: dict(self._repository.admin_score_summary())
            ),
            "metadata": self._statistic(
                "metadata", lambda: dict(self._repository.admin_metadata_summary())
            ),
            "categories": self._statistic(
                "categories",
                lambda: self._categories(
                    self._repository.list_admin_category_counts()[0]
                ),
            ),
        }

    @staticmethod
    def _date_boundary(value: str, end_of_day: bool) -> str | None:
        """校验页面日期并转换为数据库使用的 EXIF 时间格式。"""
        if not value:
            return None
        try:
            time.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ParameterError("日期必须使用 YYYY-MM-DD 格式") from error
        suffix = "23:59:59" if end_of_day else "00:00:00"
        return f"{value.replace('-', ':')} {suffix}"

    def _file_state(self, path: str) -> dict[str, Any]:
        """返回不泄露内部异常的文件可用状态。"""
        try:
            resolved = self._media_service.resolve_photo(path)
        except (ParameterError, PermissionDeniedError, ResourceNotFoundError, OSError):
            return {"available": False, "size": None}
        return {"available": True, "size": resolved.stat().st_size}

    def _active_photo_path(self, photo_id: int) -> str:
        """返回未进入回收站的后台照片路径，不按分析状态过滤。"""
        row = self._repository.get_admin_photo(photo_id)
        if row is None:
            raise ResourceNotFoundError("照片不存在")
        return str(row["path"] or "")

    def admin_thumbnail(self, photo_id: int) -> BinaryContent:
        """生成受认证后台使用的活动照片缩略图。

        公开媒体接口仍只允许分析成功的照片；后台按编号确认记录未删除后，允许管理员查看
        pending、running 和 failed 照片，以便诊断或移入回收站。

        @param photo_id: photo_scores 表中的照片编号
        @return: JPEG 缩略图二进制内容
        """
        path = self._media_service.resolve_photo(self._active_photo_path(photo_id))
        with Image.open(path) as image:
            image.thumbnail((300, 200))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG")
        return BinaryContent(buffer.getvalue(), "image/jpeg")

    def admin_full_photo(self, photo_id: int) -> FileContent:
        """返回受认证后台使用的活动照片原图描述。

        路径必须来自未删除的照片记录，并继续接受 IMAGE_DIR 与 .trash 边界检查。

        @param photo_id: photo_scores 表中的照片编号
        @return: 可由 Blueprint 安全发送的原图文件描述
        """
        path = self._media_service.resolve_photo(self._active_photo_path(photo_id))
        return FileContent(path)

    def _list_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """把照片行转换为后台列表字段并附加文件状态。"""
        path = str(row["path"] or "")
        photo_id = int(row["id"])
        return {
            "id": photo_id,
            "path": path,
            "title": Path(path).name or "未命名照片",
            "description": row["caption"],
            "side_caption": row["side_caption"],
            "category": row["type"],
            "date_taken": row["exif_datetime"],
            "date_source": row["date_source"],
            "location": row["exif_city"],
            "memory_score": row["memory_score"],
            "beauty_score": row["beauty_score"],
            "analysis_status": row["analysis_status"],
            "analysis_error": row["analysis_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": row["version"],
            "thumbnail_url": f"/admin/photos/{photo_id}/thumbnail",
            "file": self._file_state(path),
        }

    def list_photos(
        self,
        page: int,
        limit: int,
        query: str,
        category: str,
        analysis_status: str,
        date_from: str,
        date_to: str,
        sort: str,
        view: str,
    ) -> dict[str, Any]:
        """返回受上限和白名单约束的后台照片分页结果。

        Args:
            page: 从 1 开始的页码。
            limit: 每页数量，最大 100。
            query: 文件路径、描述、旁白或城市搜索词。
            category: 分类标签。
            analysis_status: 分析状态白名单值，空字符串表示全部。
            date_from: 页面输入的起始日期。
            date_to: 页面输入的结束日期。
            sort: 排序白名单键。
            view: grid 或 table。

        Returns:
            列表、分页、筛选项和阶段边界说明。
        """
        if page < 1 or limit < 1 or limit > self.MAX_PAGE_SIZE:
            raise ParameterError("page 必须为正整数，limit 必须在 1 到 100 之间")
        if len(query) > 200:
            raise ParameterError("搜索词不能超过 200 个字符")
        if sort not in self.SUPPORTED_SORTS:
            raise ParameterError("不支持的排序方式")
        if view not in self.SUPPORTED_VIEWS:
            raise ParameterError("不支持的展示方式")
        normalized_status = analysis_status.strip()
        if normalized_status and normalized_status not in self.SUPPORTED_ANALYSIS_STATUSES:
            raise ParameterError("不支持的分析状态")
        normalized_from = self._date_boundary(date_from, False)
        normalized_to = self._date_boundary(date_to, True)
        if normalized_from and normalized_to and normalized_from > normalized_to:
            raise ParameterError("起始日期不能晚于结束日期")
        rows, total = self._repository.list_admin_photos(
            page,
            limit,
            query.strip(),
            category.strip(),
            normalized_status,
            normalized_from,
            normalized_to,
            sort,
        )
        category_rows, _total = self._repository.list_admin_category_counts()
        total_pages = max(1, (total + limit - 1) // limit)
        return {
            "items": [self._list_item(row) for row in rows],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "categories": self._categories(category_rows),
            "filters": {
                "query": query,
                "category": category,
                "analysis_status": normalized_status,
                "date_from": date_from,
                "date_to": date_to,
                "sort": sort,
                "view": view,
            },
        }

    def detail(self, photo_id: int) -> dict[str, Any]:
        """返回照片只读详情，文件缺失时仍保留数据库信息。"""
        row = self._repository.get_admin_photo(photo_id)
        if row is None:
            raise ResourceNotFoundError("照片不存在")
        item = self._list_item(row)
        metadata: list[dict[str, str]] = []
        try:
            parsed = json.loads(row["exif_json"] or "{}")
            if isinstance(parsed, Mapping):
                metadata = [
                    {"key": str(key)[:100], "value": str(value)[:300]}
                    for key, value in list(parsed.items())[:30]
                    if value not in (None, "", [], {})
                ]
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = []
        item.update(
            {
                "reason": row["reason"],
                "width": row["width"],
                "height": row["height"],
                "orientation": row["orientation"],
                "used_at": row["used_at"],
                "camera_make": row["exif_make"],
                "camera_model": row["exif_model"],
                "iso": row["exif_iso"],
                "exposure_time": row["exif_exposure_time"],
                "f_number": row["exif_f_number"],
                "focal_length": row["exif_focal_length"],
                "latitude": row["exif_gps_lat"],
                "longitude": row["exif_gps_lon"],
                "altitude": row["exif_gps_alt"],
                "metadata": metadata,
                "full_url": f"/admin/photos/{photo_id}/full",
            }
        )
        return item


class ConfigService:
    """提供公开只读设置，并隔离阶段一模拟更新接口。"""

    def __init__(self, configuration_service: Any) -> None:
        """保存统一配置服务，所有公开值均在请求期按同一版本读取。

        Args:
            configuration_service: 提供 get_many 的统一配置服务。
        """
        self._configuration = configuration_service

    def public_settings(self) -> dict[str, Any]:
        """按同一配置版本返回前端现有契约要求的裸设置对象。"""
        values = self._configuration.get_many(
            (
                "DAILY_PHOTO_QUANTITY",
                "IMAGE_DIR",
                "ENABLE_REVIEW_WEBUI",
                "DISPLAY_ROTATE_MODE",
                "DISPLAY_ROTATE_INTERVAL_SEC",
                "DISPLAY_KEEP_AWAKE",
                "DISPLAY_UI_HIDE_DELAY_SEC",
            )
        )
        return {
            "daily_photo_quantity": values["DAILY_PHOTO_QUANTITY"],
            "image_dir": str(values["IMAGE_DIR"]),
            "enable_review_webui": values["ENABLE_REVIEW_WEBUI"],
            "display_rotate_mode": values["DISPLAY_ROTATE_MODE"],
            "display_rotate_interval_sec": values["DISPLAY_ROTATE_INTERVAL_SEC"],
            "display_keep_awake": values["DISPLAY_KEEP_AWAKE"],
            "display_ui_hide_delay_sec": values["DISPLAY_UI_HIDE_DELAY_SEC"],
        }

    def simulate_update(self) -> dict[str, str]:
        """保持公开 POST /api/settings 的阶段一模拟响应。"""
        return {"status": "ok", "message": "设置更新成功"}


class MediaService:
    """处理照片路径边界、生命周期校验、缩略图和原图定位。"""

    def __init__(
        self,
        image_directory: Path,
        access_checker: Any | None = None,
        configuration_service: Any | None = None,
    ) -> None:
        """初始化媒体服务。

        Args:
            image_directory: 允许读取照片的根目录，可用分号分隔配置多个。
            access_checker: 接收请求原值、绝对路径和相对路径的活动照片校验函数。
            configuration_service: 可选统一配置服务；注入后根目录按当前生效的
                `IMAGE_DIR` 动态解析，后台改完无需重启。
        """
        self._fallback_image_dirs = parse_image_dirs(
            image_directory, base_dir=PROJECT_ROOT
        )
        self._access_checker = access_checker
        self._configuration = configuration_service

    @property
    def image_dirs(self) -> tuple[Path, ...]:
        """按当前生效配置返回全部照片根目录，损坏配置时回退到构造值。"""
        raw = current_setting(self._configuration, "IMAGE_DIR", None)
        if raw is None or not str(raw).strip():
            return self._fallback_image_dirs
        try:
            return parse_image_dirs(raw, base_dir=PROJECT_ROOT)
        except ValueError:
            return self._fallback_image_dirs

    def resolve_photo(self, raw_path: str, *, require_visible: bool = False) -> Path:
        """解析照片路径并拒绝任意根目录的回收站及非活动照片。

        Args:
            raw_path: 数据库或请求传入的绝对或相对路径。
            require_visible: 是否要求路径对应可公开的活动数据库记录。

        Returns:
            校验后的绝对路径。
        """
        if not raw_path:
            raise ParameterError("缺少路径参数")
        roots = self.image_dirs
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = roots[0] / path
        path = path.resolve()
        owning_root = next(
            (root for root in roots if path.is_relative_to(root)), None
        )
        if owning_root is None:
            raise PermissionDeniedError("照片路径超出允许范围")
        # 回收站按根隔离，必须用所属根自己的 .trash 判断，否则非主目录的已删除
        # 照片会被当成活动照片对外提供。
        if path.is_relative_to(owning_root / TRASH_DIRECTORY_NAME):
            raise ResourceNotFoundError("照片不存在")
        if require_visible and self._access_checker is not None:
            relative = str(path.relative_to(owning_root))
            if not self._access_checker((str(raw_path), str(path), relative)):
                raise ResourceNotFoundError("照片不存在")
        if not path.is_file():
            raise ResourceNotFoundError("文件不存在")
        return path

    def thumbnail(self, raw_path: str) -> BinaryContent:
        """生成仅限活动数据库照片的兼容 JPEG 缩略图。"""
        path = self.resolve_photo(raw_path, require_visible=True)
        with Image.open(path) as image:
            image.thumbnail((300, 200))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG")
        return BinaryContent(buffer.getvalue(), "image/jpeg")

    def full_photo(self, raw_path: str) -> FileContent:
        """返回仅限活动数据库照片的安全原图文件描述。"""
        return FileContent(self.resolve_photo(raw_path, require_visible=True))


class DisplayService:
    """封装展示模板选择、轮次选片和统计行为。"""

    def __init__(
        self, gallery_module: Any, database_path: Path, configuration_service: Any
    ) -> None:
        """初始化展示服务并保留请求期统一配置读取能力。

        Args:
            gallery_module: 保留原算法的 gallery 模块。
            database_path: 当前应用数据库路径。
            configuration_service: 提供请求期 get_many 的统一配置服务。
        """
        self._gallery = gallery_module
        self._database_path = database_path
        self._configuration = configuration_service
        self._templates = {"classic": "display.html", "dashboard": "dashboard.html"}

    def template_name(self, requested: str | None) -> str:
        """请求期读取默认模板，并把查询参数解析为允许的模板文件名。"""
        default_template = self._configuration.get_many(("DISPLAY_TEMPLATE",))[
            "DISPLAY_TEMPLATE"
        ]
        name = (requested or default_template).strip().lower()
        return self._templates.get(name) or self._templates.get(
            default_template, "display.html"
        )

    def next_photo(self, exclude_id: int | None) -> tuple[dict[str, Any], int]:
        """请求期读取选片参数，显式传给算法并保持现有响应契约。"""
        if self._gallery is None:
            return {"status": "error", "message": "展示选片模块未加载"}, 503
        settings = self._configuration.get_many(
            ("DISPLAY_MIN_SCORE", "DISPLAY_NEW_PHOTO_WEIGHT")
        )
        result = self._gallery.pick_next(
            self._database_path,
            exclude_id=exclude_id,
            min_score=settings["DISPLAY_MIN_SCORE"],
            new_photo_weight=settings["DISPLAY_NEW_PHOTO_WEIGHT"],
        )
        if not result.get("photo"):
            return {"status": "error", "message": result.get("error") or "没有可展示的照片", "stats": result.get("stats", {})}, 404
        return {"status": "ok", "data": result["photo"], "stats": result.get("stats", {})}, 200

    def stats(self) -> dict[str, Any] | tuple[dict[str, str], int]:
        """请求期读取展示参数并返回轮次统计，模块不可用时保持 503。"""
        if self._gallery is None:
            return {"status": "error", "message": "展示选片模块未加载"}, 503
        settings = self._configuration.get_many(
            ("DISPLAY_MIN_SCORE", "DISPLAY_NEW_PHOTO_WEIGHT")
        )
        return {
            "status": "ok",
            "data": self._gallery.get_stats(
                self._database_path,
                min_score=settings["DISPLAY_MIN_SCORE"],
                new_photo_weight=settings["DISPLAY_NEW_PHOTO_WEIGHT"],
            ),
        }

    def previous(self) -> tuple[dict[str, str], int]:
        """返回前端历史栈接管上一张行为的兼容响应。"""
        return {"status": "error", "message": "prev 由前端历史栈实现，不需要调用此接口"}, 410


class PanelService:
    """封装展示页信息面板聚合行为。"""

    def __init__(self, panel_module: Any | None, configuration_service: Any) -> None:
        """初始化面板服务并保留请求期统一配置读取能力。

        Args:
            panel_module: 提供聚合数据的 panel 模块，加载失败时为 None。
            configuration_service: 提供请求期 get_many 的统一配置服务。
        """
        self._panel = panel_module
        self._configuration = configuration_service

    def get_data(self, force: bool) -> dict[str, Any] | tuple[dict[str, str], int]:
        """一次读取面板配置并显式传入聚合链路，模块不可用时保持 503。"""
        if self._panel is None:
            return {"status": "error", "message": "信息面板模块未加载"}, 503
        settings = self._configuration.get_many(
            (
                "ONTHISDAY_COUNT",
                "ONTHISDAY_STRATEGY",
                "ONTHISDAY_MIN_YEAR",
                "PANEL_AI_MODEL",
                "API_URL",
                "API_KEY",
                "MODEL_NAME",
            )
        )
        return {
            "status": "ok",
            "data": self._panel.get_panel_data(
                force=force,
                count=settings["ONTHISDAY_COUNT"],
                strategy=settings["ONTHISDAY_STRATEGY"],
                min_year=settings["ONTHISDAY_MIN_YEAR"],
                panel_ai_model=settings["PANEL_AI_MODEL"],
                api_url=settings["API_URL"],
                api_key=settings["API_KEY"],
                model_name=settings["MODEL_NAME"],
            ),
        }


class RenderService:
    """封装现有电子墨水屏模拟渲染接口。"""

    def __init__(self, render_module: Any | None) -> None:
        """初始化渲染服务。

        Args:
            render_module: 可选的渲染模块，加载失败时为 None。
        """
        self._render_module = render_module

    def render(self) -> dict[str, str]:
        """保持阶段一模拟渲染响应，不启动真实渲染任务。"""
        if self._render_module is None:
            return {"status": "error", "message": "渲染模块未加载"}
        return {"status": "ok", "message": "渲染成功"}


class DeviceService:
    """处理电子相框下载密钥、产物屏蔽、编号和输出文件定位。"""

    def __init__(
        self,
        output_directory: Path,
        download_key: str,
        quantity: int,
        blocked_checker: Any | None = None,
    ) -> None:
        """初始化设备下载服务。

        Args:
            output_directory: 渲染输出根目录。
            download_key: 下载路径密钥。
            quantity: 可下载照片编号上限。
            blocked_checker: 返回产物是否因删除重渲染而被屏蔽的函数。
        """
        self._output_directory = output_directory.resolve()
        self._download_key = download_key
        self._quantity = quantity
        self._blocked_checker = blocked_checker

    def photo(self, key: str, index: int) -> FileContent:
        """定位指定编号的电子相框二进制文件。"""
        if key != self._download_key or index < 0 or index >= self._quantity:
            raise ResourceNotFoundError()
        return self._file(f"photo_{index}.bin")

    def latest(self, key: str) -> FileContent:
        """定位兼容路径 latest.bin。"""
        self._check_key(key)
        return self._file("latest.bin")

    def preview(self, key: str) -> FileContent:
        """定位兼容路径 preview.png。"""
        self._check_key(key)
        return self._file("preview.png")

    def _check_key(self, key: str) -> None:
        """校验下载路径密钥而不在错误中回显密钥。"""
        if key != self._download_key:
            raise ResourceNotFoundError()

    def _file(self, name: str) -> FileContent:
        """仅在产物未屏蔽时返回输出目录中的现有文件。"""
        if self._blocked_checker is not None and self._blocked_checker():
            raise ResourceNotFoundError("显示产物正在安全更新")
        path = (self._output_directory / name).resolve()
        if not path.is_relative_to(self._output_directory) or not path.is_file():
            raise ResourceNotFoundError()
        mimetype = "application/octet-stream" if path.suffix.lower() == ".bin" else mimetypes.guess_type(str(path))[0]
        return FileContent(path, mimetype)


class FileBrowserService:
    """封装受配置保护的输出目录浏览和文件读取。"""

    def __init__(
        self,
        output_directory: Path,
        enabled: bool,
        webui_enabled: bool,
        blocked_checker: Any | None = None,
        configuration_service: Any | None = None,
    ) -> None:
        """初始化目录浏览服务。

        Args:
            output_directory: 允许浏览的输出根目录。
            enabled: 未注入配置服务时是否开放目录浏览。
            webui_enabled: 未注入配置服务时是否启用公开 WebUI。
            blocked_checker: 返回受管理产物是否处于屏蔽状态的函数。
            configuration_service: 可选统一配置服务；注入后两个开关在每次浏览时
                按当前生效配置读取，后台改完立即生效。
        """
        self._output_directory = output_directory.resolve()
        self._fallback_enabled = bool(enabled)
        self._fallback_webui_enabled = bool(webui_enabled)
        self._blocked_checker = blocked_checker
        self._configuration = configuration_service

    @property
    def _enabled(self) -> bool:
        """按当前生效配置返回是否开放产物目录浏览。"""
        return bounded_boolean(
            current_setting(
                self._configuration, "ENABLE_FILE_BROWSER", self._fallback_enabled
            ),
            self._fallback_enabled,
        )

    @property
    def _webui_enabled(self) -> bool:
        """按当前生效配置返回是否启用照片浏览页面。"""
        return bounded_boolean(
            current_setting(
                self._configuration, "ENABLE_REVIEW_WEBUI", self._fallback_webui_enabled
            ),
            self._fallback_webui_enabled,
        )

    def browse(self, subpath: str) -> FileContent | str:
        """返回目录 HTML 或目录中的文件描述。

        Args:
            subpath: 相对于输出根目录的路径。

        Returns:
            目录时返回 HTML，文件时返回文件描述。
        """
        if not self._enabled or not self._webui_enabled:
            raise ResourceNotFoundError()
        if self._blocked_checker is not None and self._blocked_checker():
            raise ResourceNotFoundError("显示产物正在安全更新")
        path = (self._output_directory / subpath).resolve()
        if not path.is_relative_to(self._output_directory):
            raise ParameterError("目录路径无效")
        if path.is_file():
            mimetype = "application/octet-stream" if path.suffix.lower() == ".bin" else mimetypes.guess_type(str(path))[0]
            return FileContent(path, mimetype)
        if not path.is_dir():
            raise ResourceNotFoundError()
        items = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative = child.relative_to(self._output_directory)
            href = "/files/" + str(relative).replace("\\", "/")
            name = child.name + ("/" if child.is_dir() else "")
            items.append(f'<li><a href="{html.escape(href)}">{html.escape(name)}</a></li>')
        up = ""
        if path != self._output_directory:
            parent = path.parent.relative_to(self._output_directory)
            up = f'<a href="/files/{html.escape(str(parent).replace(chr(92), "/"))}">⬅ 返回上级</a><br><br>'
        current = "." if path == self._output_directory else html.escape(str(path.relative_to(self._output_directory)))
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>InkTime Files</title><style>body{font-family:system-ui,sans-serif;padding:24px}"
            "ul{line-height:1.8}code{background:#f2f2f2;padding:2px 6px;border-radius:4px}</style>"
            f"</head><body><h3>输出目录浏览</h3><p>当前：<code>{current}</code></p>{up}<ul>"
            + "".join(items) + "</ul></body></html>"
        )
