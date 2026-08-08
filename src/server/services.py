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
        """从 EXIF JSON 提取去重后的月日清单并做实例级缓存。

        Returns:
            排序后的 MM-DD 字符串列表。
        """
        if not self._database_path.exists():
            return []
        now = time.time()
        if now - float(self._date_cache.get("built_at", 0.0)) < 3600:
            return list(self._date_cache.get("items", []))
        values: set[str] = set()
        for row in self._repository.list_exif_json():
            try:
                date_value = json.loads(row["exif_json"] or "{}").get("DateTime", "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if date_value and len(date_value) >= 10 and date_value[7] == "-":
                values.add(date_value[5:10])
        items = sorted(values)
        self._date_cache = {"built_at": now, "items": items}
        return items

    def random_day(self) -> str:
        """从现有日期清单中随机选择一天。

        Returns:
            一个 MM-DD 日期。
        """
        items = self.date_list()
        if not items:
            raise ResourceNotFoundError("没有找到照片")
        return random.choice(items)


class ConfigService:
    """提供公开只读设置，并隔离阶段一模拟更新接口。"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        """保存当前应用配置视图。

        Args:
            config: 当前 Flask 应用配置。
        """
        self._config = config

    def public_settings(self) -> dict[str, Any]:
        """返回前端现有契约要求的裸设置对象。"""
        return {
            "daily_photo_quantity": self._config["DAILY_PHOTO_QUANTITY"],
            "image_dir": str(self._config["IMAGE_DIR"]),
            "enable_review_webui": self._config["ENABLE_REVIEW_WEBUI"],
            "display_rotate_mode": self._config["DISPLAY_ROTATE_MODE"],
            "display_rotate_interval_sec": self._config["DISPLAY_ROTATE_INTERVAL_SEC"],
            "display_keep_awake": self._config["DISPLAY_KEEP_AWAKE"],
            "display_ui_hide_delay_sec": self._config["DISPLAY_UI_HIDE_DELAY_SEC"],
        }

    def simulate_update(self) -> dict[str, str]:
        """保持公开 POST /api/settings 的阶段一模拟响应。"""
        return {"status": "ok", "message": "设置更新成功"}


class MediaService:
    """处理照片路径边界、缩略图和原图定位。"""

    def __init__(self, image_directory: Path) -> None:
        """初始化媒体服务。

        Args:
            image_directory: 允许读取照片的根目录。
        """
        self._image_directory = image_directory.resolve()

    def resolve_photo(self, raw_path: str) -> Path:
        """解析照片路径并强制限制在 IMAGE_DIR 之下。

        Args:
            raw_path: 数据库或请求传入的绝对/相对路径。

        Returns:
            校验后的绝对路径。
        """
        if not raw_path:
            raise ParameterError("缺少路径参数")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self._image_directory / path
        path = path.resolve()
        if not path.is_relative_to(self._image_directory):
            raise PermissionDeniedError("照片路径超出允许范围")
        if not path.is_file():
            raise ResourceNotFoundError("文件不存在")
        return path

    def thumbnail(self, raw_path: str) -> BinaryContent:
        """生成兼容现有尺寸的 JPEG 缩略图。

        Args:
            raw_path: 待处理照片路径。

        Returns:
            JPEG 二进制内容。
        """
        path = self.resolve_photo(raw_path)
        with Image.open(path) as image:
            image.thumbnail((300, 200))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG")
        return BinaryContent(buffer.getvalue(), "image/jpeg")

    def full_photo(self, raw_path: str) -> FileContent:
        """返回通过安全边界校验的原图文件。

        Args:
            raw_path: 待读取照片路径。

        Returns:
            可由 Blueprint 发送的文件描述。
        """
        return FileContent(self.resolve_photo(raw_path))


class DisplayService:
    """封装展示模板选择、轮次选片和统计行为。"""

    def __init__(self, gallery_module: Any, database_path: Path, default_template: str) -> None:
        """初始化展示服务。

        Args:
            gallery_module: 保留原算法的 gallery 模块。
            database_path: 当前应用数据库路径。
            default_template: 默认展示模板名称。
        """
        self._gallery = gallery_module
        self._database_path = database_path
        self._default_template = default_template
        self._templates = {"classic": "display.html", "dashboard": "dashboard.html"}

    def template_name(self, requested: str | None) -> str:
        """把模板查询参数解析为允许的模板文件名。"""
        name = (requested or self._default_template).strip().lower()
        return self._templates.get(name) or self._templates[self._default_template]

    def next_photo(self, exclude_id: int | None) -> tuple[dict[str, Any], int]:
        """选取下一张并保持公开响应字段和业务状态码。"""
        if self._gallery is None:
            return {"status": "error", "message": "展示选片模块未加载"}, 503
        result = self._gallery.pick_next(self._database_path, exclude_id=exclude_id)
        if not result.get("photo"):
            return {"status": "error", "message": result.get("error") or "没有可展示的照片", "stats": result.get("stats", {})}, 404
        return {"status": "ok", "data": result["photo"], "stats": result.get("stats", {})}, 200

    def stats(self) -> dict[str, Any] | tuple[dict[str, str], int]:
        """返回轮次展示统计，模块不可用时保持 503。"""
        if self._gallery is None:
            return {"status": "error", "message": "展示选片模块未加载"}, 503
        return {"status": "ok", "data": self._gallery.get_stats(self._database_path)}

    def previous(self) -> tuple[dict[str, str], int]:
        """返回前端历史栈接管上一张行为的兼容响应。"""
        return {"status": "error", "message": "prev 由前端历史栈实现，不需要调用此接口"}, 410


class PanelService:
    """封装展示页信息面板聚合行为。"""

    def __init__(self, panel_module: Any | None) -> None:
        """初始化面板服务。

        Args:
            panel_module: 提供聚合数据的 panel 模块，加载失败时为 None。
        """
        self._panel = panel_module

    def get_data(self, force: bool) -> dict[str, Any] | tuple[dict[str, str], int]:
        """获取面板数据，模块不可用时保持 503。"""
        if self._panel is None:
            return {"status": "error", "message": "信息面板模块未加载"}, 503
        return {"status": "ok", "data": self._panel.get_panel_data(force=force)}


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
    """处理电子相框下载密钥、编号和输出文件定位。"""

    def __init__(self, output_directory: Path, download_key: str, quantity: int) -> None:
        """初始化设备下载服务。

        Args:
            output_directory: 渲染输出根目录。
            download_key: 下载路径密钥。
            quantity: 可下载照片编号上限。
        """
        self._output_directory = output_directory.resolve()
        self._download_key = download_key
        self._quantity = quantity

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
        """返回输出目录中的现有文件。"""
        path = (self._output_directory / name).resolve()
        if not path.is_relative_to(self._output_directory) or not path.is_file():
            raise ResourceNotFoundError()
        mimetype = "application/octet-stream" if path.suffix.lower() == ".bin" else mimetypes.guess_type(str(path))[0]
        return FileContent(path, mimetype)


class FileBrowserService:
    """封装受配置保护的输出目录浏览和文件读取。"""

    def __init__(self, output_directory: Path, enabled: bool, webui_enabled: bool) -> None:
        """初始化目录浏览服务。

        Args:
            output_directory: 允许浏览的输出根目录。
            enabled: 是否开放目录浏览。
            webui_enabled: 是否启用公开 WebUI。
        """
        self._output_directory = output_directory.resolve()
        self._enabled = enabled
        self._webui_enabled = webui_enabled

    def browse(self, subpath: str) -> FileContent | str:
        """返回目录 HTML 或目录中的文件描述。

        Args:
            subpath: 相对于输出根目录的路径。

        Returns:
            目录时返回 HTML，文件时返回文件描述。
        """
        if not self._enabled or not self._webui_enabled:
            raise ResourceNotFoundError()
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
