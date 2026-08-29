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

    # 收录跃迁后允许自动排队分析的分析状态，必须与 AdminJobRepository.enqueue_included
    # 的筛选条件逐字一致：succeeded 无需重跑，running 已在队列里，legacy 是历史已有结果、
    # 要重跑得走显式的重新分析入口。两处只要有一处漏改，就会出现「按张数放行不选它、
    # 改成已收录却给它排队」这种自相矛盾的付费行为。
    ENQUEUEABLE_ANALYSIS_STATUSES = frozenset({"pending", "failed"})

    @classmethod
    def _curation_transition(
        cls, row: Mapping[str, Any], updates: Mapping[str, Any]
    ) -> str | None:
        """判断本次更新是否真正改变了收录状态，并给出方向。

        判定依据是「库里原值到新值的跃迁」，而不是「本次提交的值是不是已收录」。
        照片详情页每次保存都会提交收录字段，按提交值判断会让一张已收录且已分析成功
        的照片，每改一次分类就重新排一次付费分析。

        Args:
            row: 更新前的照片行，提供收录状态与分析状态原值。
            updates: 即将写入的数据库列，键为列名。

        Returns:
            activated 表示未收录改为已收录且分析状态允许排队；deactivated 表示已收录
            改为未收录；未改动收录状态、或改为已收录但分析状态不允许排队时返回空值。
        """
        if "is_included" not in updates:
            return None
        was_included = bool(int(row["is_included"]))
        now_included = bool(int(updates["is_included"]))
        if was_included == now_included:
            return None
        if not now_included:
            return "deactivated"
        status = str(updates.get("analysis_status", row["analysis_status"]) or "")
        return "activated" if status in cls.ENQUEUEABLE_ANALYSIS_STATUSES else None

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

        本方法**不**排队或撤销分析任务，只在返回值里报告收录状态的跃迁方向。排队要另开
        写事务，在本方法的事务内嵌套第二个写连接会在 SQLite 上互相锁死；由调用方在事务
        提交后按 `curation_transition` 处理，是唯一不牺牲原子性的做法。代价是提交与排队
        之间存在空隙，由照片管理页的按张数放行作为兜底收敛手段。

        Returns:
            更新后的编号、版本、更新时间，以及本次收录状态的跃迁方向。
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
            # 必须在乐观更新之前取原值：更新之后 row 里的收录状态已经没有比较价值。
            curation_transition = self._curation_transition(row, updates)
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
            "curation_transition": curation_transition,
        }

    # 批量可改字段：入参语义键 -> 数据库列名。收录用 curation 而不是 is_included，
    # 与前端表单和单张编辑保持同一套对外命名。
    BATCH_FIELD_COLUMNS = {
        "category": "type",
        "analysis_status": "analysis_status",
        "curation": "is_included",
    }

    def batch_update(
        self,
        items: Any,
        changes: Any,
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """按本次提交实际给出的字段批量更新，逐项保留不存在和版本冲突结果。

        契约是「键存在即要改」：`changes` 里出现某个键就更新对应字段，不出现就不动。
        「不修改」由调用方省略键来表达，因此空字符串可以无歧义地表示清空分类，不必
        再为空值到底是哪种意思额外约定标志位。

        一次提交里的多个字段落在同一次乐观更新中，版本只递增一次。早期契约是一个
        动作配一个值，改三个属性得提交三次，而每次提交后列表页重定向都会清空勾选，
        等于要把同一批照片重新勾三遍。

        Args:
            items: 包含照片编号和预期版本的列表。
            changes: category、analysis_status、curation 的任意非空子集。
            admin_user_id: 当前管理员编号。
            admin_username: 当前管理员用户名快照。

        与 `update_photo` 一样，本方法不排队也不撤销分析任务，只报告哪些照片真正发生了
        收录跃迁，由调用方在事务提交后处理，原因见 `update_photo` 的说明。

        Returns:
            批次编号、成功项、失败项，以及本次真正由未收录改为已收录（curation_activated）
            和由已收录改为未收录（curation_deactivated）的照片编号；数据库异常会使整个
            事务回滚。

        Raises:
            ParameterError: 未指定任何字段、字段名不被允许或取值不合法。
        """
        if not isinstance(changes, Mapping) or not changes:
            raise ParameterError("必须至少指定一个要修改的字段")
        unknown = sorted(set(changes) - set(self.BATCH_FIELD_COLUMNS))
        if unknown:
            raise ParameterError(
                "批量操作只允许修改 category、analysis_status 或 curation"
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
        # 先整体归一化再进事务：任何一个字段取值不合法都不该改到任何一张照片
        updates: dict[str, Any] = {}
        if "category" in changes:
            updates["type"] = self._category(changes["category"])
        if "analysis_status" in changes:
            updates["analysis_status"] = self._analysis_status(changes["analysis_status"])
        if "curation" in changes:
            updates["is_included"] = self._curation(changes["curation"])
        batch_id = uuid.uuid4().hex
        timestamp = self._timestamp()
        succeeded: list[dict[str, int]] = []
        failed: list[dict[str, Any]] = []
        # 跃迁必须逐张判定：同一批里各张照片的收录原值与分析状态各不相同，按整批
        # 的 changes 判断会把「本来就已收录」的照片也算成新收录并重复排队付费。
        activated: list[int] = []
        deactivated: list[int] = []
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
                transition = self._curation_transition(row, updates)
                updated = self._repository.optimistic_update(
                    connection,
                    photo_id,
                    version,
                    updates,
                    timestamp,
                )
                if updated is None:
                    failed.append(
                        {"id": photo_id, "code": "conflict", "message": "照片版本已变化"}
                    )
                    continue
                if transition == "activated":
                    activated.append(photo_id)
                elif transition == "deactivated":
                    deactivated.append(photo_id)
                # 审计如实反映「这一次操作改了哪些字段」：一个批次一条记录、含全部
                # 变更字段。按字段拆成多条会让同一次操作在日志里像多次独立改动。
                self._repository.insert_audit(
                    connection,
                    photo_id,
                    admin_user_id,
                    admin_username,
                    "batch_update",
                    {column: row[column] for column in updates},
                    {column: updated[column] for column in updates},
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
            "curation_activated": activated,
            "curation_deactivated": deactivated,
        }
