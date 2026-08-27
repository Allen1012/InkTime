"""公开页面、接口、媒体和设备行为的 Service 层。"""

from __future__ import annotations

import hashlib
import html
import io
import json
import mimetypes
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from PIL import Image, ImageOps

from src.configuration import (
    PROJECT_ROOT,
    TRASH_DIRECTORY_NAME,
    bounded_boolean,
    bounded_int,
    current_setting,
    is_within_windows,
    next_window_start,
    parse_image_dirs,
    parse_time_windows,
)

from .errors import ParameterError, PermissionDeniedError, ResourceNotFoundError
from .repositories import PhotoRepository
from .weather import get_weather


@dataclass(frozen=True)
class BinaryContent:
    """描述由 Blueprint 转换为响应的内存二进制内容。

    `etag` 供路由层做条件请求：缩略图每次都要解码原图，重复生成很贵。
    """

    data: bytes
    mimetype: str
    etag: str | None = None


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
    SUPPORTED_SORTS = {
        "latest", "oldest", "added_newest", "added_oldest", "memory", "beauty",
        # 展示次数排序：shown_least 用来捞出还没轮到的照片
        "shown_most", "shown_least",
    }
    SUPPORTED_VIEWS = {"grid", "table"}
    # 收录状态筛选值；空字符串表示不筛选，故不列入白名单
    SUPPORTED_CURATIONS = {"included", "excluded"}
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
        """返回可独立降级的后台首页统计卡片。

        每张卡片单独查询、单独降级：某一项因数据库问题取不到时只显示该项
        「暂不可用」，不会让整个首页打不开。
        """
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
            "trash": self._statistic("trash", self._repository.count_trashed_photos),
            "analysis": self._statistic(
                "analysis",
                lambda: dict(self._repository.admin_analysis_status_summary()),
            ),
            "jobs": self._statistic(
                "jobs", lambda: dict(self._repository.admin_job_status_summary())
            ),
            "recent": self._statistic("recent", self._recent_photo_count),
            "missing_date": self._statistic(
                "missing_date", self._repository.count_photos_missing_date
            ),
        }

    # 首页「最近新增」的统计窗口，同时用于卡片文案，避免两处写死不同的天数
    RECENT_WINDOW_DAYS = 7

    def _recent_photo_count(self) -> int:
        """统计最近 RECENT_WINDOW_DAYS 天入库的活动照片数。"""
        since = datetime.now(timezone.utc) - timedelta(days=self.RECENT_WINDOW_DAYS)
        return self._repository.count_photos_created_since(
            since.isoformat(timespec="seconds")
        )

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
        # 复用 MediaService 的生成实现，而不是再抄一份：先前两条路径各自硬编码尺寸，
        # 改了配置只有公开接口生效，后台照片管理页看着毫无变化。
        return self._media_service.render_thumbnail(
            self.admin_thumbnail_path(photo_id)
        )

    def admin_thumbnail_path(self, photo_id: int) -> Path:
        """返回后台缩略图对应的照片路径，供路由先算缓存校验值再决定是否生成。"""
        return self._media_service.resolve_photo(self._active_photo_path(photo_id))

    def admin_full_photo(self, photo_id: int) -> FileContent:
        """返回受认证后台使用的活动照片原图描述。

        路径必须来自未删除的照片记录，并继续接受 IMAGE_DIR 与 .trash 边界检查。

        @param photo_id: photo_scores 表中的照片编号
        @return: 可由 Blueprint 安全发送的原图文件描述
        """
        path = self._media_service.resolve_photo(self._active_photo_path(photo_id))
        return FileContent(path)

    @staticmethod
    def _display_state(show_count: Any, last_shown_at: Any) -> str:
        """把展示统计的两个原始值归纳成三态，供模板直接映射文案。

        三态必须分开，合成一个数字会误导：`untracked` 是这张照片压根没进过候选池
        （分析未成功或回忆分低于 DISPLAY_MIN_SCORE），`never` 是在池里待选但一次
        没轮到。前者要改分或重新分析，后者只需等待，处置动作完全不同。

        Args:
            show_count: display_stats.show_count，为 None 表示没有统计行。
            last_shown_at: display_stats.last_shown_at，空表示从未真正展示。

        Returns:
            untracked、never 或 shown。
        """
        if show_count is None:
            return "untracked"
        if not str(last_shown_at or "").strip():
            return "never"
        return "shown"

    def _list_item(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """把照片行转换为后台列表字段，附加文件状态与展示统计。

        展示统计来自 join 的 display_stats，列表与详情两条查询都必须带上这两列。
        """
        path = str(row["path"] or "")
        photo_id = int(row["id"])
        stored_name = Path(path).name
        # 上传的照片落盘名是随机十六进制串（防重名与路径穿越），对人没有辨识度。
        # 原始文件名一直存在 original_filename 里，展示时优先用它；扫描入库的照片
        # 没有这个字段，回退到磁盘名——那本来就是用户自己起的名字。
        original_name = str(row["original_filename"] or "").strip()
        return {
            "id": photo_id,
            "path": path,
            "title": original_name or stored_name or "未命名照片",
            "stored_filename": stored_name,
            "original_filename": original_name,
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
            # show_count 不是真实播放次数：新照片入池时继承池内当前最小值作为基线
            # （见 gallery._sync_new_photos），之后每展示一次加一。它衡量的是轮转
            # 均衡度，跨照片比较才有意义，绝对值不能当「被看过几次」解读。
            "display_count": row["show_count"],
            "display_last_shown_at": row["last_shown_at"],
            "display_state": self._display_state(
                row["show_count"], row["last_shown_at"]
            ),
            "is_included": bool(row["is_included"]),
        }

    def _normalize_admin_filters(
        self,
        query: str,
        category: str,
        analysis_status: str,
        date_from: str,
        date_to: str,
        sort: str,
        curation: str = "",
    ) -> dict[str, Any]:
        """归一化并校验后台列表筛选参数。

        分页列表与详情页的相邻照片查询共用同一份归一化：「上一张/下一张」的定义
        是「当前列表里的前后一张」，两处若各自解释参数，翻页就会跳出当前筛选。

        Args:
            query: 搜索词。
            category: 分类标签。
            analysis_status: 分析状态白名单值，空字符串表示全部。
            date_from: 页面输入的起始日期。
            date_to: 页面输入的结束日期。
            sort: 排序白名单键。
            curation: 收录状态筛选值。

        Returns:
            可直接传给仓储的筛选字典。

        Raises:
            ParameterError: 搜索词过长、排序或状态不在白名单、日期区间颠倒。
        """
        if len(query) > 200:
            raise ParameterError("搜索词不能超过 200 个字符")
        if sort not in self.SUPPORTED_SORTS:
            raise ParameterError("不支持的排序方式")
        normalized_status = analysis_status.strip()
        if normalized_status and normalized_status not in self.SUPPORTED_ANALYSIS_STATUSES:
            raise ParameterError("不支持的分析状态")
        normalized_curation = str(curation or "").strip()
        if normalized_curation and normalized_curation not in self.SUPPORTED_CURATIONS:
            raise ParameterError("不支持的收录状态")
        normalized_from = self._date_boundary(date_from, False)
        normalized_to = self._date_boundary(date_to, True)
        if normalized_from and normalized_to and normalized_from > normalized_to:
            raise ParameterError("起始日期不能晚于结束日期")
        return {
            "query": query.strip(),
            "category": category.strip(),
            "analysis_status": normalized_status,
            "date_from": normalized_from,
            "date_to": normalized_to,
            "sort": sort,
            "curation": normalized_curation,
        }

    def adjacent_photos(
        self,
        photo_id: int,
        query: str = "",
        category: str = "",
        analysis_status: str = "",
        date_from: str = "",
        date_to: str = "",
        sort: str = "latest",
        missing_date: bool = False,
        curation: str = "",
    ) -> dict[str, int | None]:
        """返回当前筛选与排序下目标照片的前后邻居编号。

        Args:
            photo_id: 当前照片编号。
            query: 与列表页一致的搜索词。
            category: 分类标签。
            analysis_status: 分析状态白名单值。
            date_from: 页面输入的起始日期。
            date_to: 页面输入的结束日期。
            sort: 排序白名单键。
            missing_date: 只看缺拍摄时间的照片。
            curation: 收录状态筛选值。

        Returns:
            含 previous_id 与 next_id 的字典，处于两端时对应项为 None。
        """
        filters = self._normalize_admin_filters(
            query, category, analysis_status, date_from, date_to, sort, curation
        )
        try:
            normalized_id = int(photo_id)
        except (TypeError, ValueError) as error:
            raise ParameterError("photo_id 必须是正整数") from error
        if normalized_id < 1:
            raise ParameterError("photo_id 必须是正整数")
        previous_id, next_id = self._repository.find_adjacent_admin_photos(
            normalized_id,
            filters["query"],
            filters["category"],
            filters["analysis_status"],
            filters["date_from"],
            filters["date_to"],
            filters["sort"],
            missing_date=missing_date,
            curation=filters["curation"],
        )
        return {"previous_id": previous_id, "next_id": next_id}

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
        missing_date: bool = False,
        curation: str = "",
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
            missing_date: 只看缺拍摄时间的照片。
            curation: 收录状态筛选，included、excluded 或空字符串表示全部。

        Returns:
            列表、分页、筛选项和阶段边界说明。
        """
        if page < 1 or limit < 1 or limit > self.MAX_PAGE_SIZE:
            raise ParameterError("page 必须为正整数，limit 必须在 1 到 100 之间")
        if view not in self.SUPPORTED_VIEWS:
            raise ParameterError("不支持的展示方式")
        filters = self._normalize_admin_filters(
            query, category, analysis_status, date_from, date_to, sort, curation
        )
        rows, total = self._repository.list_admin_photos(
            page,
            limit,
            filters["query"],
            filters["category"],
            filters["analysis_status"],
            filters["date_from"],
            filters["date_to"],
            filters["sort"],
            missing_date=missing_date,
            curation=filters["curation"],
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
                "analysis_status": filters["analysis_status"],
                "date_from": date_from,
                "date_to": date_to,
                "sort": sort,
                "view": view,
                "missing_date": missing_date,
                "curation": filters["curation"],
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
        cache_directory: Path | None = None,
    ) -> None:
        """初始化媒体服务。

        Args:
            image_directory: 允许读取照片的根目录，可用分号分隔配置多个。
            access_checker: 接收请求原值、绝对路径和相对路径的活动照片校验函数。
            configuration_service: 可选统一配置服务；注入后根目录按当前生效的
                `IMAGE_DIR` 动态解析，后台改完无需重启。
            cache_directory: 缩略图磁盘缓存目录；为空表示不使用磁盘缓存。
        """
        self._fallback_image_dirs = parse_image_dirs(
            image_directory, base_dir=PROJECT_ROOT
        )
        self._access_checker = access_checker
        self._configuration = configuration_service
        # 缓存目录刻意不放照片目录内：那里的 JPEG 会被扫描当成照片入库
        self._cache_directory = (
            Path(cache_directory).expanduser().resolve() if cache_directory else None
        )

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

    def _resolve_within_roots(self, raw_path: str) -> tuple[Path, Path]:
        """把路径解析为绝对路径，并确认它落在某个照片根目录内。

        目录穿越这道防线对活动照片和已隐藏照片是完全一样的，所以抽出来共用；
        两者的区别只在于要不要接受所属根的回收站目录。

        Args:
            raw_path: 数据库或请求传入的绝对或相对路径。

        Returns:
            解析后的绝对路径与它所属的照片根目录。

        Raises:
            ParameterError: 路径为空。
            PermissionDeniedError: 路径不在任何照片根目录内。
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
        return path, owning_root

    def resolve_photo(self, raw_path: str, *, require_visible: bool = False) -> Path:
        """解析照片路径并拒绝任意根目录的回收站及非活动照片。

        Args:
            raw_path: 数据库或请求传入的绝对或相对路径。
            require_visible: 是否要求路径对应可公开的活动数据库记录。

        Returns:
            校验后的绝对路径。
        """
        path, owning_root = self._resolve_within_roots(raw_path)
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

    def resolve_hidden_photo(self, raw_path: str) -> Path:
        """解析已隐藏照片路径，允许它位于所属根的回收站目录内。

        早期版本的隐藏会把文件真搬进 `.trash/<id>/`，那些记录的 `trash_path` 非空，
        用 `resolve_photo()` 一律被判为「照片不存在」——整页预览都取不到图。
        这里放开的只有回收站这一条，目录穿越防线仍然生效。

        仅供已认证后台的已隐藏照片预览使用：调用方必须先按编号确认记录确实是
        `is_deleted = 1`，路径因此来自数据库而非请求参数，公开接口不受影响。

        Args:
            raw_path: 已隐藏照片记录中的 `trash_path` 或 `path`。

        Returns:
            校验后的绝对路径。

        Raises:
            ParameterError: 路径为空。
            PermissionDeniedError: 路径不在任何照片根目录内。
            ResourceNotFoundError: 文件已不在磁盘上。
        """
        path, _ = self._resolve_within_roots(raw_path)
        if not path.is_file():
            raise ResourceNotFoundError("文件不存在")
        return path

    def thumbnail(self, raw_path: str) -> BinaryContent:
        """生成仅限活动数据库照片的兼容 JPEG 缩略图。"""
        return self.render_thumbnail(self.resolve_photo(raw_path, require_visible=True))

    def thumbnail_etag(self, path: Path) -> str:
        """构造缩略图的缓存校验值，**不生成图片**。

        路由必须先拿校验值再决定是否生成：先前的实现把校验值算在生成方法内部，导致
        返回 304 之前图片已经生成完毕，只省了带宽、一点 CPU 都没省——而 304 恰恰是
        浏览器缓存过期后重新验证的常用路径。

        取源文件的修改时间与体积，并带上影响输出的两个参数：改了尺寸或质量后浏览器
        必须重新拉取，否则会继续用旧缓存里的模糊图。

        Args:
            path: 已通过边界与可见性校验的照片绝对路径。

        Returns:
            弱校验值字符串。
        """
        max_edge, quality = self._thumbnail_settings()
        # 必须与 render_thumbnail 里的校验值逐字一致，否则条件请求永远不命中，
        # 或者更糟：路由用旧校验值回了 304，浏览器继续显示上一版渲染逻辑的图。
        return (
            f'W/"thumb{self._THUMBNAIL_RENDER_VERSION}'
            f'-{self._source_signature(path)}-{max_edge}-{quality}"'
        )

    # 缩略图渲染逻辑的版本号。改动会影响输出像素的逻辑（方向纠正、缩放算法、编码参数
    # 之类）时必须递增：缓存键与浏览器校验值都含它，递增即让全部旧缓存与旧 304 失效。
    # 不加这个版本号，键就只由源文件与尺寸质量构成——源文件没变时，修好的逻辑会被旧
    # 缓存直接盖过去，表现为「代码改了、页面没变」。
    # 2: 缩略图生成补上 EXIF 方向纠正（此前列表页与详情页方向会不一致）
    _THUMBNAIL_RENDER_VERSION = 2

    def render_thumbnail(self, path: Path) -> BinaryContent:
        """按当前配置返回缩略图，优先命中磁盘缓存。

        与 `thumbnail()` 的区别只是入口：后者负责公开接口的可见性校验，后台按编号定位
        的路由已自行校验过权限，两者共用这里的生成逻辑，避免尺寸配置只在一处生效。

        Args:
            path: 已通过边界与可见性校验的照片绝对路径。

        Returns:
            含 JPEG 字节、媒体类型与缓存校验值的二进制内容。
        """
        max_edge, quality = self._thumbnail_settings()
        signature = self._source_signature(path)
        etag = (
            f'W/"thumb{self._THUMBNAIL_RENDER_VERSION}-{signature}-{max_edge}-{quality}"'
        )
        cache_file = self._cache_file(path, signature, max_edge, quality)
        if cache_file is not None and cache_file.is_file():
            try:
                return BinaryContent(cache_file.read_bytes(), "image/jpeg", etag)
            except OSError as error:
                LOGGER.warning("Thumbnail cache read failed, regenerating, error=[%s]", error)
        data = self._generate_thumbnail_bytes(path, max_edge, quality)
        if cache_file is not None:
            self._store_cache(cache_file, data)
        return BinaryContent(data, "image/jpeg", etag)

    def _generate_thumbnail_bytes(self, path: Path, max_edge: int, quality: int) -> bytes:
        """实时生成缩略图字节。

        用 `draft()` 让 libjpeg 直接以缩小比例解码：四千像素级原图整幅解码再缩放很贵，
        而缩略图请求在翻页时非常密集。缩放用 LANCZOS，比默认算法更锐利。小图不放大。

        **必须按 EXIF 方向转置像素。** 手机与相机常把照片按传感器方向存成横向像素，
        再用 EXIF Orientation 标记应该转多少度显示。详情页发送的是原图字节、标记还在，
        浏览器会自动纠正；而这里输出的 JPEG 不带 EXIF，浏览器无从纠正，不转置就会定格
        在未旋转的方向。表现就是同一张照片在列表页横着、点进详情页却竖着。

        转置放在缩放之前：`thumbnail()` 按长边限制等比缩放，先转置才能保证长边取的是
        显示方向的长边，否则竖图会被按横向的长边计算、缩出偏小的结果。
        """
        with Image.open(path) as image:
            image.draft("RGB", (max_edge, max_edge))
            # exif_transpose 返回新对象（无方向标记时返回副本），原图对象不受影响
            oriented = ImageOps.exif_transpose(image) or image
            if max(oriented.size) > max_edge:
                oriented.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buffer = io.BytesIO()
            oriented.convert("RGB").save(
                buffer, format="JPEG", quality=quality, optimize=True
            )
        return buffer.getvalue()

    def _thumbnail_settings(self) -> tuple[int, int]:
        """按当前生效配置返回缩略图长边与质量。"""
        if self._configuration is None:
            return 640, 82
        values = self._configuration.get_many(
            ("THUMBNAIL_MAX_EDGE", "THUMBNAIL_QUALITY")
        )
        max_edge = bounded_int(values["THUMBNAIL_MAX_EDGE"], 64, 2048, 640)
        quality = bounded_int(values["THUMBNAIL_QUALITY"], 40, 95, 82)
        return max_edge, quality

    @staticmethod
    def _source_signature(path: Path) -> str:
        """用修改时间与体积标识源文件版本，源图变化即产生新键。"""
        try:
            status = path.stat()
            return f"{status.st_mtime_ns}-{status.st_size}"
        except OSError:
            return "unknown"

    def _cache_enabled(self) -> bool:
        """按当前配置返回是否启用磁盘缓存。"""
        if self._cache_directory is None:
            return False
        if self._configuration is None:
            return True
        return bounded_boolean(
            self._configuration.get_many(("THUMBNAIL_CACHE_ENABLED",))[
                "THUMBNAIL_CACHE_ENABLED"
            ],
            True,
        )

    def _cache_file(
        self, path: Path, signature: str, max_edge: int, quality: int
    ) -> Path | None:
        """返回缩略图缓存文件路径；未启用缓存时返回空。

        键名由源路径摘要、源文件版本、两个输出参数与渲染逻辑版本号构成：换照片、改照片、
        调尺寸质量或改渲染逻辑都会落到新文件，不需要显式失效。按摘要前两位分桶，避免单
        目录堆积过多文件。

        缓存目录刻意不放在照片目录内：那里的 JPEG 会被扫描当成照片入库。
        """
        if not self._cache_enabled():
            return None
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        name = (
            f"{digest}-{signature}-{max_edge}-{quality}"
            f"-v{self._THUMBNAIL_RENDER_VERSION}.jpg"
        )
        return self._cache_directory / digest[:2] / name

    def _store_cache(self, cache_file: Path, data: bytes) -> None:
        """原子写入缓存，并清掉同一张照片的旧变体。

        缓存写失败不影响本次响应：它只是加速手段，任何异常都只记录告警。
        """
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_file.with_name(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, cache_file)
            prefix = cache_file.name.split("-", 1)[0]
            for stale in cache_file.parent.glob(f"{prefix}-*.jpg"):
                if stale.name != cache_file.name:
                    stale.unlink(missing_ok=True)
        except OSError as error:
            LOGGER.warning("Thumbnail cache write failed, error=[%s]", error)

    def full_photo(self, raw_path: str) -> FileContent:
        """返回仅限活动数据库照片的安全原图文件描述。"""
        return FileContent(self.resolve_photo(raw_path, require_visible=True))


class DisplayService:
    """封装展示模板选择、轮次选片和统计行为。"""

    def __init__(
        self,
        gallery_module: Any,
        database_path: Path,
        configuration_service: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """初始化展示服务并保留请求期统一配置读取能力。

        Args:
            gallery_module: 保留原算法的 gallery 模块。
            database_path: 当前应用数据库路径。
            configuration_service: 提供请求期 get_many 的统一配置服务。
            clock: 可选本地时间提供器，用于生效时间段判定与隔离验证注入固定时刻。
        """
        self._gallery = gallery_module
        self._database_path = database_path
        self._configuration = configuration_service
        self._clock = clock or datetime.now
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

    def _idle_response(self) -> tuple[dict[str, Any], int]:
        """构造休息期响应：按配置决定画面，且完全不触碰展示计数。

        `photo` 模式指定的照片不可用时回退为停在最后一张；没有任何展示历史时进一步
        回退为休息文案，避免返回空白画面。

        Returns:
            响应体与状态码。状态码保持 200，由 `status=idle` 表达语义。
        """
        settings = self._configuration.get_many(
            (
                "DISPLAY_IDLE_MODE",
                "DISPLAY_IDLE_PHOTO_ID",
                "DISPLAY_REST_TEXT",
                "DISPLAY_ACTIVE_WINDOWS",
            )
        )
        mode = str(settings["DISPLAY_IDLE_MODE"]).strip().lower()
        photo: dict[str, Any] | None = None
        if mode == "photo":
            photo_id = int(settings["DISPLAY_IDLE_PHOTO_ID"] or 0)
            if photo_id > 0:
                photo = self._gallery.peek_photo(
                    self._database_path, photo_id=photo_id
                )
            if photo is None:
                mode = "freeze"
        if mode == "freeze":
            photo = self._gallery.peek_photo(self._database_path)
            if photo is None:
                mode = "rest"

        now = self._clock()
        windows = parse_time_windows(settings["DISPLAY_ACTIVE_WINDOWS"])
        resume_at = next_window_start(now, windows)
        remaining = (
            int((resume_at - now).total_seconds()) if resume_at is not None else 0
        )
        return {
            "status": "idle",
            "reason": "outside_active_windows",
            "idle_mode": mode,
            "message": str(settings["DISPLAY_REST_TEXT"]),
            "resume_at": resume_at.isoformat(timespec="seconds") if resume_at else None,
            # 退避上限五分钟：既不再按轮播节奏打扰服务端，又能在管理员中途改配置后
            # 最多五分钟内恢复轮播。
            "next_check_after_sec": max(5, min(300, remaining)) if remaining else 300,
            "data": photo,
        }, 200

    def next_photo(self, exclude_id: int | None) -> tuple[dict[str, Any], int]:
        """请求期读取选片参数，显式传给算法并保持现有响应契约。

        生效时间段之外不进入选片，因此既不切换画面也不消耗展示次数；判定使用服务端
        本地时间，避免展示设备时钟或时区不准导致休息时段偏移。
        """
        if self._gallery is None:
            return {"status": "error", "message": "展示选片模块未加载"}, 503
        settings = self._configuration.get_many(
            ("DISPLAY_MIN_SCORE", "DISPLAY_NEW_PHOTO_WEIGHT", "DISPLAY_ACTIVE_WINDOWS")
        )
        windows = parse_time_windows(settings["DISPLAY_ACTIVE_WINDOWS"])
        if not is_within_windows(self._clock(), windows):
            return self._idle_response()
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
                "ONTHISDAY_SOURCE",
                "ONTHISDAY_STRATEGY",
                "ONTHISDAY_MIN_YEAR",
                "PANEL_AI_MODEL",
                "API_URL",
                "API_KEY",
                "MODEL_NAME",
                "WEATHER_ENABLED",
                "WEATHER_LOCATION",
                "WEATHER_LOCATION_NAME",
                "WEATHER_CACHE_MINUTES",
                "HOME_LAT",
                "HOME_LON",
            )
        )
        data = self._panel.get_panel_data(
            force=force,
            count=settings["ONTHISDAY_COUNT"],
            strategy=settings["ONTHISDAY_STRATEGY"],
            min_year=settings["ONTHISDAY_MIN_YEAR"],
            panel_ai_model=settings["PANEL_AI_MODEL"],
            api_url=settings["API_URL"],
            api_key=settings["API_KEY"],
            model_name=settings["MODEL_NAME"],
            source=settings["ONTHISDAY_SOURCE"],
        )
        # 天气在服务层合并，不改动动态加载的 panel 模块契约；取数内部已完全降级，
        # 因此这里不需要额外的异常处理，天气故障不会影响其余面板段。
        data["weather"] = get_weather(
            enabled=bool(settings["WEATHER_ENABLED"]),
            home_lat=float(settings["HOME_LAT"]),
            home_lon=float(settings["HOME_LON"]),
            location=settings["WEATHER_LOCATION"],
            location_name=str(settings["WEATHER_LOCATION_NAME"]),
            cache_minutes=int(settings["WEATHER_CACHE_MINUTES"]),
        )
        return {"status": "ok", "data": data}


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
        """按当前生效配置返回产物目录浏览的总开关。"""
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
