"""后台照片编辑、乐观锁、批量操作和审计业务。"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .errors import ConflictError, ParameterError, ResourceNotFoundError
from .repositories.photo_management_repository import PhotoManagementRepository


class AdminPhotoManagementService:
    """校验照片编辑请求并保证照片更新与审计原子提交。"""

    MAX_BATCH_SIZE = 100
    ANALYSIS_STATUSES = {"pending", "running", "succeeded", "failed"}
    SCORE_FIELDS = {"memory_score", "beauty_score"}
    TEXT_LIMITS = {
        "caption": 500,
        "side_caption": 100,
        "reason": 1000,
        "exif_city": 100,
    }
    INPUT_FIELDS = {
        "caption",
        "side_caption",
        "memory_score",
        "beauty_score",
        "reason",
        "exif_city",
        "category",
        "date_taken",
        "analysis_status",
        "curation",
    }

    @staticmethod
    def _score(field: str, value: Any) -> float | None:
        """校验可空的零至一百分数，拒绝布尔值、非数字和非有限值。"""
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterError(f"{field} 必须是 0 到 100 之间的数字或空值")
        normalized = float(value)
        if not math.isfinite(normalized) or not 0 <= normalized <= 100:
            raise ParameterError(f"{field} 必须是 0 到 100 之间的有限数字")
        return normalized

    def __init__(
        self,
        repository: PhotoManagementRepository,
        invalidate_date_cache: Callable[[], None],
    ) -> None:
        """初始化照片管理服务。

        Args:
            repository: 照片写入与审计仓储。
            invalidate_date_cache: 成功修改日期后清除公开月日缓存的回调。
        """
        self._repository = repository
        self._invalidate_date_cache = invalidate_date_cache

    @staticmethod
    def _timestamp() -> str:
        """返回秒级协调世界时审计时间。"""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _positive_integer(value: Any, name: str) -> int:
        """把请求值规范化为正整数。"""
        if isinstance(value, bool):
            raise ParameterError(f"{name} 必须为正整数")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as error:
            raise ParameterError(f"{name} 必须为正整数") from error
        if normalized < 1:
            raise ParameterError(f"{name} 必须为正整数")
        return normalized

    @classmethod
    def _text(cls, field: str, value: Any) -> str:
        """校验可编辑文本字段并去除首尾空白。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ParameterError(f"{field} 必须是文本")
        normalized = value.strip()
        if len(normalized) > cls.TEXT_LIMITS[field]:
            raise ParameterError(
                f"{field} 不能超过 {cls.TEXT_LIMITS[field]} 个字符"
            )
        return normalized

    @staticmethod
    def _category(value: Any) -> str:
        """规范化自定义分类，限制标签数量和单个标签长度。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ParameterError("category 必须是文本")
        tags: list[str] = []
        seen: set[str] = set()
        for raw_tag in re.split(r"[/，,、|\\；;]+", value):
            tag = raw_tag.strip()
            if not tag or tag in seen:
                continue
            if len(tag) > 30:
                raise ParameterError("每个分类标签不能超过 30 个字符")
            seen.add(tag)
            tags.append(tag)
        if len(tags) > 10:
            raise ParameterError("分类标签最多 10 个")
        return "/".join(tags)

    @classmethod
    def _analysis_status(cls, value: Any) -> str:
        """校验管理员可设置的新分析状态，不允许重新伪造历史状态。"""
        if not isinstance(value, str) or value not in cls.ANALYSIS_STATUSES:
            raise ParameterError(
                "analysis_status 只允许 pending、running、succeeded、failed"
            )
        return value

    @staticmethod
    def _curation(value: Any) -> int:
        """把收录状态入参规范成数据库里的 0 或 1。

        接受 included/excluded 两个语义值，同时兼容表单与 JSON 常见的 1/0、true/false。
        落库用整数是为了和 is_deleted 保持同一种布尔表达方式，也让 CHECK 约束生效。
        """
        text = str(value).strip().lower()
        if text in {"included", "1", "true", "yes", "on"}:
            return 1
        if text in {"excluded", "0", "false", "no", "off"}:
            return 0
        raise ParameterError("收录状态只允许 included 或 excluded")

    @staticmethod
    def _date_shape(value: Any) -> str | None:
        """校验日期输入格式，具体的仅日期补时在事务内完成。"""
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ParameterError("date_taken 必须是日期文本")
        normalized = value.strip()
        formats = (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y:%m:%d %H:%M:%S",
        )
        for date_format in formats:
            try:
                datetime.strptime(normalized, date_format)
                return normalized
            except ValueError:
                continue
        raise ParameterError("date_taken 格式必须为 YYYY-MM-DD 或完整日期时间")

    @classmethod
    def _normalize_payload(cls, values: Mapping[str, Any]) -> dict[str, Any]:
        """拒绝未知字段并完成不依赖数据库当前值的格式校验。"""
        unknown_fields = set(values) - cls.INPUT_FIELDS
        if unknown_fields:
            raise ParameterError(f"不支持的照片字段: {sorted(unknown_fields)}")
        normalized: dict[str, Any] = {}
        for field in cls.TEXT_LIMITS:
            if field in values:
                normalized[field] = cls._text(field, values[field])
        if "category" in values:
            normalized["category"] = cls._category(values["category"])
        if "analysis_status" in values:
            normalized["analysis_status"] = cls._analysis_status(
                values["analysis_status"]
            )
        if "curation" in values:
            normalized["curation"] = cls._curation(values["curation"])
        for field in cls.SCORE_FIELDS:
            if field in values:
                normalized[field] = cls._score(field, values[field])
        if "date_taken" in values:
            normalized["date_taken"] = cls._date_shape(values["date_taken"])
        if not normalized:
            raise ParameterError("至少提供一个可编辑字段")
        return normalized

    @staticmethod
    def _database_datetime(value: str | None, current_value: Any) -> str | None:
        """转换为 EXIF 时间格式；仅日期输入沿用当前记录的时分秒。"""
        if value is None:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            time_part = "00:00:00"
            current = str(current_value or "")
            if re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", current):
                time_part = current.split(" ", 1)[1]
            return f"{value.replace('-', ':')} {time_part}"
        parsed = value.replace("T", " ").replace("-", ":", 2)
        if len(parsed) == 16:
            parsed += ":00"
        return parsed

    @staticmethod
    def _exif_json(raw_value: Any, date_value: str | None) -> str:
        """同步 EXIF JSON 的两种历史日期键与日期来源键。"""
        try:
            parsed = json.loads(raw_value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed["datetime"] = date_value
        parsed["DateTime"] = date_value
        parsed["date_source"] = "manual"
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _database_updates(
        cls, row: Mapping[str, Any], values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """把接口字段转换为数据库字段并补齐日期缓存字段。"""
        updates = {
            field: values[field]
            for field in cls.TEXT_LIMITS
            if field in values
        }
        for field in cls.SCORE_FIELDS:
            if field in values:
                updates[field] = values[field]
        if "category" in values:
            updates["type"] = values["category"]
        if "analysis_status" in values:
            updates["analysis_status"] = values["analysis_status"]
        # 收录状态与批量入口写同一列，命名也保持 curation -> is_included 这一套映射
        if "curation" in values:
            updates["is_included"] = values["curation"]
        if "date_taken" in values:
            date_value = cls._database_datetime(
                values["date_taken"], row["exif_datetime"]
            )
            updates.update(
                {
                    "exif_datetime": date_value,
                    "date_source": "manual",
                    "exif_json": cls._exif_json(row["exif_json"], date_value),
                }
            )
        return updates

    @staticmethod
    def _audit_values(row: Mapping[str, Any], columns: Sequence[str]) -> dict[str, Any]:
        """提取本次真正涉及的业务字段，避免审计快照包含无关信息。"""
        return {column: row[column] for column in columns}

    def update_photo(
        self,
        photo_id: Any,
        expected_version: Any,
        values: Mapping[str, Any],
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """更新单张照片并在相同事务记录管理员审计。

        Args:
            photo_id: photo_scores 表的稳定自增编号。
            expected_version: 客户端读取到的乐观锁版本。
            values: 待修改的后台接口字段。
            admin_user_id: 当前管理员编号。
            admin_username: 当前管理员用户名快照。

        Returns:
            更新后的编号、版本和更新时间。
        """
        normalized_id = self._positive_integer(photo_id, "photo_id")
        normalized_version = self._positive_integer(expected_version, "version")
        normalized_values = self._normalize_payload(values)
        changed_date = "date_taken" in normalized_values
        timestamp = self._timestamp()
        with self._repository.transaction() as connection:
            row = self._repository.get_for_update(connection, normalized_id)
            if row is None or bool(row["is_deleted"]):
                raise ResourceNotFoundError("照片不存在")
            if int(row["version"]) != normalized_version:
                raise ConflictError("照片已被其他操作修改，请刷新后重试")
            updates = self._database_updates(row, normalized_values)
            updated = self._repository.optimistic_update(
                connection,
                normalized_id,
                normalized_version,
                updates,
                timestamp,
            )
            if updated is None:
                raise ConflictError("照片已被其他操作修改，请刷新后重试")
            columns = tuple(updates)
            self._repository.insert_audit(
                connection,
                normalized_id,
                admin_user_id,
                admin_username,
                "photo_update",
                self._audit_values(row, columns),
                self._audit_values(updated, columns),
                None,
                timestamp,
            )
        if changed_date:
            self._invalidate_date_cache()
        return {
            "id": normalized_id,
            "version": int(updated["version"]),
            "updated_at": updated["updated_at"],
        }

    def batch_update(
        self,
        action: Any,
        items: Any,
        value: Any,
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """批量设置分类、分析状态或收录状态，逐项保留不存在和版本冲突结果。

        Args:
            action: set_category、set_analysis_status 或 set_curation。
            items: 包含照片编号和预期版本的列表。
            value: 全批次共用的新分类、分析状态或收录状态。
            admin_user_id: 当前管理员编号。
            admin_username: 当前管理员用户名快照。

        Returns:
            批次编号、成功项和失败项；数据库异常会使整个事务回滚。
        """
        if action not in {"set_category", "set_analysis_status", "set_curation"}:
            raise ParameterError(
                "批量操作只允许 set_category、set_analysis_status 或 set_curation"
            )
        if not isinstance(items, list) or not 1 <= len(items) <= self.MAX_BATCH_SIZE:
            raise ParameterError("批量项目数量必须在 1 到 100 之间")
        normalized_items: list[tuple[int, int]] = []
        seen_ids: set[int] = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise ParameterError("每个批量项目必须包含 id 和 version")
            photo_id = self._positive_integer(item.get("id"), "id")
            version = self._positive_integer(item.get("version"), "version")
            if photo_id in seen_ids:
                raise ParameterError("同一批次不能包含重复照片编号")
            seen_ids.add(photo_id)
            normalized_items.append((photo_id, version))
        if action == "set_category":
            normalized_value = self._category(value)
            field = "type"
        elif action == "set_curation":
            normalized_value = self._curation(value)
            field = "is_included"
        else:
            normalized_value = self._analysis_status(value)
            field = "analysis_status"
        batch_id = uuid.uuid4().hex
        timestamp = self._timestamp()
        succeeded: list[dict[str, int]] = []
        failed: list[dict[str, Any]] = []
        with self._repository.transaction() as connection:
            for photo_id, version in normalized_items:
                row = self._repository.get_for_update(connection, photo_id)
                if row is None or bool(row["is_deleted"]):
                    failed.append(
                        {"id": photo_id, "code": "not_found", "message": "照片不存在"}
                    )
                    continue
                if int(row["version"]) != version:
                    failed.append(
                        {
                            "id": photo_id,
                            "code": "conflict",
                            "message": "照片版本已变化",
                            "current_version": int(row["version"]),
                        }
                    )
                    continue
                updated = self._repository.optimistic_update(
                    connection,
                    photo_id,
                    version,
                    {field: normalized_value},
                    timestamp,
                )
                if updated is None:
                    failed.append(
                        {"id": photo_id, "code": "conflict", "message": "照片版本已变化"}
                    )
                    continue
                self._repository.insert_audit(
                    connection,
                    photo_id,
                    admin_user_id,
                    admin_username,
                    f"batch_{action}",
                    {field: row[field]},
                    {field: updated[field]},
                    batch_id,
                    timestamp,
                )
                succeeded.append({"id": photo_id, "version": int(updated["version"])})
        return {
            "batch_id": batch_id,
            "succeeded": succeeded,
            "failed": failed,
            "success_count": len(succeeded),
            "failure_count": len(failed),
        }
