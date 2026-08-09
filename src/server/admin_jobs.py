"""上传、持久化后台任务和工作进程的业务实现。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from src.database import database_connection, write_transaction
from .errors import ParameterError

LOGGER = logging.getLogger(__name__)
JOB_TYPES = {"analyze_photo", "generate_narration", "backfill_content_hash"}
JOB_STATUSES = {"pending", "running", "succeeded", "failed", "canceled"}
_FORMATS = {"JPEG": (".jpg", ".jpeg"), "PNG": (".png",), "WEBP": (".webp",)}
_RESULT_COLUMNS = (
    "caption", "type", "memory_score", "beauty_score", "reason", "width", "height",
    "orientation", "exif_json", "raw_json", "exif_datetime", "exif_make", "exif_model",
    "exif_iso", "exif_exposure_time", "exif_f_number", "exif_focal_length", "exif_gps_lat",
    "exif_gps_lon", "exif_gps_alt", "side_caption", "exif_city", "date_source",
)
_UPLOAD_METADATA_COLUMNS = (
    "exif_json", "exif_datetime", "exif_make", "exif_model", "exif_iso",
    "exif_exposure_time", "exif_f_number", "exif_focal_length", "exif_gps_lat",
    "exif_gps_lon", "exif_gps_alt", "date_source",
)


class UploadValidationError(ValueError):
    """表示上传文件违反数量、大小、格式或解码限制。"""


class JobTransitionError(ParameterError):
    """表示后台任务请求了不合法的状态转换。"""


@dataclass(frozen=True)
class JobRuntimeConfig:
    """保存 Web 与工作进程共享且经过严格校验的任务运行参数。"""

    max_attempts: int = 3
    lease_seconds: int = 120
    renew_seconds: int = 30
    poll_seconds: float = 2.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "JobRuntimeConfig":
        """严格解析任务参数并拒绝无法安全续租的组合。

        Args:
            values: 包含 JOB_MAX_ATTEMPTS、JOB_LEASE_SECONDS、JOB_RENEW_SECONDS 和
                JOB_POLL_SECONDS 的配置映射。

        Returns:
            已校验的不可变任务配置。
        """
        try:
            maximum = int(values.get("JOB_MAX_ATTEMPTS", 3))
            lease = int(values.get("JOB_LEASE_SECONDS", 120))
            renew = int(values.get("JOB_RENEW_SECONDS", 30))
            poll = float(values.get("JOB_POLL_SECONDS", 2))
        except (TypeError, ValueError) as error:
            raise RuntimeError("后台任务配置必须是数字") from error
        if not 1 <= maximum <= 3:
            raise RuntimeError("JOB_MAX_ATTEMPTS 必须在 1 到 3 之间")
        if lease < 1 or renew < 1 or poll <= 0:
            raise RuntimeError("后台任务租约、续租和轮询间隔必须为正数")
        if renew >= lease:
            raise RuntimeError("JOB_RENEW_SECONDS 必须小于 JOB_LEASE_SECONDS")
        return cls(maximum, lease, renew, poll)


def _utc_now() -> datetime:
    """返回带时区的当前协调世界时。"""
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    """把协调世界时转换成 SQLite 可按字典序比较的字符串。"""
    return (value or _utc_now()).isoformat(timespec="seconds")


def _optional_number(value: Any) -> float | None:
    """把 Pillow 有理数等数值转换为浮点数，非法值返回空值。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_decimal(values: Any, reference: Any) -> float | None:
    """把 EXIF 度分秒坐标转换为带方向的十进制度。"""
    if not values or len(values) != 3:
        return None
    parts = [_optional_number(item) for item in values]
    if any(item is None for item in parts):
        return None
    result = float(parts[0]) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0
    if str(reference).upper() in {"S", "W"}:
        result = -result
    return result


def _stream_sha256(path: Path, interrupted: Callable[[], bool] | None = None) -> str | None:
    """流式计算文件摘要，并允许调用方在块边界中断。

    Args:
        path: 待读取文件。
        interrupted: 每读取一个块后调用的中断判断。

    Returns:
        十六进制 SHA-256；被中断时返回空值。
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            if interrupted is not None and interrupted():
                return None


class AdminJobRepository:
    """使用独立短事务持久化照片、后台任务和任务事件。"""

    def __init__(
        self,
        database_path: Path,
        max_attempts: int = 3,
        snapshot_provider: Callable[[str, Any], tuple[int, str]] | None = None,
    ) -> None:
        """保存数据库路径、任务上限及可选的事务内配置快照提供器。

        Args:
            database_path: 后台任务使用的 SQLite 文件。
            max_attempts: 新任务最大尝试次数，允许 1 至 3。
            snapshot_provider: 接收作用域和当前连接，返回版本与稳定 JSON 文本。
        """
        if not 1 <= int(max_attempts) <= 3:
            raise ValueError("max_attempts 必须在 1 到 3 之间")
        self.database_path = Path(database_path).expanduser().resolve()
        self.max_attempts = int(max_attempts)
        self.snapshot_provider = snapshot_provider

    @staticmethod
    def _record_event(
        connection: Any,
        job_id: int,
        event_type: str,
        old_status: str | None,
        new_status: str | None,
        *,
        admin_user_id: int | None = None,
        worker_id: str | None = None,
        reason_code: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """在任务状态事务内记录不含异常正文的稳定审计事件。"""
        connection.execute(
            "INSERT INTO admin_job_events (job_id,event_type,old_status,new_status,admin_user_id,"
            "worker_id,reason_code,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, event_type, old_status, new_status, admin_user_id, worker_id,
             reason_code[:100] if reason_code else None, created_at or _timestamp()),
        )

    @staticmethod
    def _payload(job: Mapping[str, Any]) -> dict[str, Any]:
        """安全解析由本系统生成的任务载荷。"""
        try:
            value = json.loads(str(job["payload_json"]))
            return value if isinstance(value, dict) else {}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _set_photo_state(
        connection: Any,
        job: Mapping[str, Any],
        status: str,
        error_code: str | None,
        now: str,
    ) -> int | None:
        """按任务持有版本转换分析任务关联照片状态并返回新版本。"""
        if job["job_type"] != "analyze_photo":
            return int(job["photo_version"])
        cursor = connection.execute(
            "UPDATE photo_scores SET analysis_status=?,analysis_error=?,updated_at=?,version=version+1 "
            "WHERE id=? AND version=? AND is_deleted=0",
            (status, error_code, now, job["photo_id"], job["photo_version"]),
        )
        return int(job["photo_version"]) + 1 if cursor.rowcount == 1 else None

    @staticmethod
    def _cancel_running(
        connection: Any, job: Mapping[str, Any], worker_id: str, now: str
    ) -> bool:
        """在同一事务终结运行任务和照片，供取消与完成竞态共同复用。"""
        new_version = AdminJobRepository._set_photo_state(
            connection, job, "failed", "job_canceled", now
        )
        if job["job_type"] == "analyze_photo" and new_version is None:
            return False
        cursor = connection.execute(
            "UPDATE admin_jobs SET status='canceled',progress=0,lease_owner=NULL,lease_expires_at=NULL,"
            "error_code='job_canceled',error_summary='任务已取消',photo_version=?,updated_at=?,finished_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (new_version, now, now, job["id"], worker_id),
        )
        if cursor.rowcount == 1:
            AdminJobRepository._record_event(
                connection, int(job["id"]), "canceled", "running", "canceled",
                worker_id=worker_id, reason_code="job_canceled", created_at=now,
            )
            return True
        return False

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """按新到旧返回后台任务。

        Args:
            limit: 最大返回条数，限制为 1 至 200。

        Returns:
            包含任务字段和照片路径的字典列表。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT j.*,p.path AS photo_path FROM admin_jobs j JOIN photo_scores p ON p.id=j.photo_id "
                "ORDER BY j.id DESC LIMIT ?", (max(1, min(int(limit), 200)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        """读取任务和关联照片路径。

        Args:
            job_id: admin_jobs 主键。

        Returns:
            任务字典；不存在时返回空值。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT j.*,p.path AS photo_path FROM admin_jobs j JOIN photo_scores p ON p.id=j.photo_id "
                "WHERE j.id=?", (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def enqueue(
        self, photo_id: int, job_type: str, created_by: int,
        payload: Mapping[str, Any], priority: int = 100,
    ) -> dict[str, Any]:
        """按照片当前版本创建 pending 任务并阻止重复活跃任务。

        Args:
            photo_id: 关联的 photo_scores 主键。
            job_type: analyze_photo、generate_narration 或 backfill_content_hash。
            created_by: 创建任务的管理员编号。
            payload: 不含敏感值的任务参数。
            priority: 数值越大越优先；历史摘要回填使用低优先级。

        Returns:
            新任务；已有相同活跃任务时返回该任务并标记 duplicate。
        """
        if job_type not in JOB_TYPES:
            raise JobTransitionError("unsupported_job_type")
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            photo = connection.execute(
                "SELECT id,version FROM photo_scores WHERE id=? AND is_deleted=0", (photo_id,)
            ).fetchone()
            if photo is None:
                raise JobTransitionError("photo_not_found")
            existing = connection.execute(
                "SELECT * FROM admin_jobs WHERE photo_id=? AND job_type=? AND status IN ('pending','running') "
                "ORDER BY id DESC LIMIT 1", (photo_id, job_type),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["duplicate"] = True
                return result
            photo_version = int(photo["version"])
            if job_type == "analyze_photo":
                connection.execute(
                    "UPDATE photo_scores SET analysis_status='pending',analysis_error=NULL,updated_at=?,"
                    "version=version+1 WHERE id=? AND version=?", (now, photo_id, photo_version),
                )
                photo_version += 1
            cursor = connection.execute(
                "INSERT INTO admin_jobs (job_type,status,payload_json,priority,progress,created_by,photo_id,"
                "photo_version,attempts,max_attempts,cancel_requested,created_at,updated_at) "
                "VALUES (?,'pending',?,?,0,?,?,?,0,?,0,?,?)",
                (job_type, json.dumps(dict(payload), ensure_ascii=False, sort_keys=True), int(priority),
                 created_by, photo_id, photo_version, self.max_attempts, now, now),
            )
            result = dict(connection.execute(
                "SELECT * FROM admin_jobs WHERE id=?", (cursor.lastrowid,)
            ).fetchone())
            result["duplicate"] = False
            return result

    def enqueue_hash_backfill(self, created_by: int, limit: int = 1000) -> dict[str, int]:
        """按稳定照片编号为缺少最终文件摘要的历史照片创建低优先级任务。

        每张照片使用独立短写事务，并依赖活跃任务唯一索引防止重复排队。

        Args:
            created_by: 发起回填的管理员编号。
            limit: 单次最多扫描的照片数量。

        Returns:
            created、duplicate 和 scanned 计数。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT id FROM photo_scores WHERE is_deleted=0 AND content_sha256 IS NULL "
                "ORDER BY id LIMIT ?", (max(1, min(int(limit), 10000)),),
            ).fetchall()
        created = 0
        duplicates = 0
        for row in rows:
            result = self.enqueue(
                int(row["id"]), "backfill_content_hash", created_by,
                {"is_new_upload": False}, priority=0,
            )
            if result["duplicate"]:
                duplicates += 1
            else:
                created += 1
        return {"created": created, "duplicate": duplicates, "scanned": len(rows)}

    def create_uploaded_photos_and_jobs(
        self, items: Iterable[Mapping[str, Any]], created_by: int
    ) -> list[dict[str, Any]]:
        """在单一事务内按最终文件摘要去重并创建整批照片与分析任务。

        Args:
            items: 含 path、original_filename、content_sha256 和 original_metadata 的项目。
            created_by: 当前管理员编号。

        Returns:
            与输入同序的 accepted 或 duplicate 结果。
        """
        now = _timestamp()
        results: list[dict[str, Any]] = []
        with write_transaction(self.database_path) as connection:
            for item in items:
                digest = str(item["content_sha256"])
                duplicate = connection.execute(
                    "SELECT id FROM photo_scores WHERE content_sha256=? AND is_deleted=0 ORDER BY id LIMIT 1",
                    (digest,),
                ).fetchone()
                if duplicate:
                    results.append({"status": "duplicate", "photo_id": int(duplicate["id"]),
                                    "job_id": None, "path": None})
                    continue
                metadata = dict(item.get("original_metadata") or {})
                columns = ",".join(_UPLOAD_METADATA_COLUMNS)
                placeholders = ",".join("?" for _ in _UPLOAD_METADATA_COLUMNS)
                values = [metadata.get(column) for column in _UPLOAD_METADATA_COLUMNS]
                cursor = connection.execute(
                    "INSERT INTO photo_scores (path,original_filename,content_sha256,analysis_status,"
                    f"analysis_error,is_deleted,created_at,updated_at,version,{columns}) "
                    f"VALUES (?,?,?,'pending',NULL,0,?,?,1,{placeholders})",
                    (str(item["path"]), str(item["original_filename"]), digest, now, now, *values),
                )
                photo_id = int(cursor.lastrowid)
                payload = json.dumps(
                    {"is_new_upload": True, "original_metadata": metadata},
                    ensure_ascii=False, sort_keys=True,
                )
                job_cursor = connection.execute(
                    "INSERT INTO admin_jobs (job_type,status,payload_json,priority,progress,created_by,photo_id,"
                    "photo_version,attempts,max_attempts,cancel_requested,created_at,updated_at) "
                    "VALUES ('analyze_photo','pending',?,100,0,?,?,1,0,?,0,?,?)",
                    (payload, created_by, photo_id, self.max_attempts, now, now),
                )
                results.append({"status": "accepted", "photo_id": photo_id,
                                "job_id": int(job_cursor.lastrowid), "path": str(item["path"])})
        return results

    def recover_expired_leases(self) -> int:
        """逐项恢复过期租约；未耗尽任务回 pending，耗尽任务进入 failed。

        Returns:
            本次恢复或终结数量。
        """
        now = _timestamp()
        count = 0
        with write_transaction(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM admin_jobs WHERE status='running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=? ORDER BY id", (now,),
            ).fetchall()
            for row in rows:
                job = dict(row)
                exhausted = int(job["attempts"]) >= int(job["max_attempts"])
                new_status = "failed" if exhausted else "pending"
                new_version = self._set_photo_state(
                    connection, job, new_status,
                    "max_attempts_exceeded" if exhausted else None, now,
                )
                if job["job_type"] == "analyze_photo" and new_version is None:
                    new_status = "failed"
                    new_version = int(job["photo_version"])
                connection.execute(
                    "UPDATE admin_jobs SET status=?,progress=0,photo_version=?,lease_owner=NULL,"
                    "lease_expires_at=NULL,error_code=?,error_summary=?,updated_at=?,finished_at=? WHERE id=?",
                    (new_status, new_version, "max_attempts_exceeded" if exhausted else None,
                     "任务租约过期且已达到最大尝试次数" if exhausted else None,
                     now, now if exhausted else None, job["id"]),
                )
                self._record_event(
                    connection, int(job["id"]), "lease_recovered", "running", new_status,
                    worker_id=str(job["lease_owner"] or "") or None,
                    reason_code="max_attempts_exceeded" if exhausted else "lease_expired",
                    created_at=now,
                )
                count += 1
        return count

    def claim_next(self, worker_id: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        """原子认领最高优先级任务，首次认领同时固化同事务配置快照。

        Args:
            worker_id: 当前工作进程稳定标识。
            lease_seconds: 租约秒数。

        Returns:
            认领后的任务及照片路径；无任务时返回空值。
        """
        now_value = _utc_now()
        now = _timestamp(now_value)
        expires = _timestamp(now_value + timedelta(seconds=max(1, lease_seconds)))
        with write_transaction(self.database_path) as connection:
            candidate = connection.execute(
                "SELECT j.*,p.version AS current_photo_version FROM admin_jobs j "
                "JOIN photo_scores p ON p.id=j.photo_id AND p.is_deleted=0 "
                "WHERE j.status='pending' AND j.attempts<j.max_attempts "
                "ORDER BY j.priority DESC,j.created_at,j.id LIMIT 1"
            ).fetchone()
            if candidate is None:
                return None
            job = dict(candidate)
            current_version = int(job["current_photo_version"])
            if job["job_type"] == "analyze_photo":
                photo_cursor = connection.execute(
                    "UPDATE photo_scores SET analysis_status='running',analysis_error=NULL,updated_at=?,"
                    "version=version+1 WHERE id=? AND version=? AND is_deleted=0",
                    (now, job["photo_id"], current_version),
                )
            else:
                photo_cursor = connection.execute(
                    "UPDATE photo_scores SET updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND is_deleted=0",
                    (now, job["photo_id"], current_version),
                )
            if photo_cursor.rowcount != 1:
                return None
            claimed_version = current_version + 1
            first_claim = job.get("started_at") is None
            scope = {
                "analyze_photo": "analysis",
                "generate_narration": "analysis",
                "backfill_content_hash": "worker",
            }.get(str(job["job_type"]))
            if first_claim and self.snapshot_provider is not None and scope is not None:
                config_version, snapshot_json = self.snapshot_provider(scope, connection)
                cursor = connection.execute(
                    "UPDATE admin_jobs SET status='running',progress=1,attempts=attempts+1,photo_version=?,"
                    "lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=?,"
                    "config_version=?,config_snapshot_json=? WHERE id=? AND status='pending'",
                    (
                        claimed_version, worker_id, expires, now, now, config_version,
                        snapshot_json, job["id"],
                    ),
                )
            else:
                cursor = connection.execute(
                    "UPDATE admin_jobs SET status='running',progress=1,attempts=attempts+1,photo_version=?,"
                    "lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (claimed_version, worker_id, expires, now, now, job["id"]),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("job_claim_lost_inside_transaction")
            self._record_event(
                connection, int(job["id"]), "claimed", "pending", "running",
                worker_id=worker_id, reason_code="worker_claim", created_at=now,
            )
            row = connection.execute(
                "SELECT j.*,p.path AS photo_path FROM admin_jobs j JOIN photo_scores p ON p.id=j.photo_id "
                "WHERE j.id=?", (job["id"],),
            ).fetchone()
            return dict(row)

    def renew_lease(self, job_id: int, worker_id: str, lease_seconds: int = 120) -> bool:
        """续租仍归当前工作进程所有的 running 任务。

        Args:
            job_id: 任务主键。
            worker_id: 当前工作进程标识。
            lease_seconds: 新租约秒数。

        Returns:
            成功续租返回 True。
        """
        now = _utc_now()
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND status='running' "
                "AND lease_owner=?",
                (_timestamp(now + timedelta(seconds=lease_seconds)), _timestamp(now), job_id, worker_id),
            )
            return cursor.rowcount == 1

    def is_cancel_requested(self, job_id: int, worker_id: str) -> bool:
        """判断任务是否取消或工作进程已失去所有权。

        Args:
            job_id: 任务主键。
            worker_id: 当前工作进程标识。

        Returns:
            请求取消或失去所有权时返回 True。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT cancel_requested,status,lease_owner FROM admin_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return row is None or row["status"] != "running" or row["lease_owner"] != worker_id or bool(row["cancel_requested"])

    def should_yield(self, job_id: int, worker_id: str, priority: int) -> bool:
        """检查低优先级任务是否应为待处理高优先级任务让出。

        Args:
            job_id: 当前任务主键。
            worker_id: 当前工作进程标识。
            priority: 当前任务优先级。

        Returns:
            有更高优先级 pending 任务时返回 True。
        """
        with database_connection(self.database_path, read_only=True) as connection:
            owner = connection.execute(
                "SELECT status,lease_owner,cancel_requested FROM admin_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if owner is None or owner["status"] != "running" or owner["lease_owner"] != worker_id or owner["cancel_requested"]:
                return True
            return connection.execute(
                "SELECT 1 FROM admin_jobs WHERE status='pending' AND priority>? LIMIT 1", (priority,)
            ).fetchone() is not None

    def defer(self, job: Mapping[str, Any], worker_id: str, reason_code: str) -> bool:
        """把可中断任务放回 pending，并退还本次未完成尝试次数。

        Args:
            job: 当前任务快照。
            worker_id: 当前工作进程标识。
            reason_code: worker_stopping 或 high_priority_available。

        Returns:
            实际让出时返回 True。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET status='pending',progress=0,attempts=MAX(0,attempts-1),"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
                (now, job["id"], worker_id),
            )
            if cursor.rowcount == 1:
                self._record_event(
                    connection, int(job["id"]), "yielded", "running", "pending",
                    worker_id=worker_id, reason_code=reason_code, created_at=now,
                )
                return True
            return False

    def mark_canceled(self, job_id: int, worker_id: str) -> bool:
        """把当前工作进程的协作取消任务终结为 canceled，分析照片标记为 failed。

        Args:
            job_id: 任务主键。
            worker_id: 当前工作进程标识。

        Returns:
            状态实际更新时返回 True。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id or not row["cancel_requested"]:
                return False
            return self._cancel_running(connection, dict(row), worker_id, now)

    def complete(self, job: Mapping[str, Any], worker_id: str, result: Mapping[str, Any]) -> bool:
        """按认领版本写入分析结果，取消优先，版本冲突不覆盖管理员编辑。

        Args:
            job: claim_next 返回的任务快照。
            worker_id: 当前工作进程标识。
            result: 单张分析结果或新旁白。

        Returns:
            照片和任务均成功更新时返回 True。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return False
            current = dict(row)
            if current["cancel_requested"]:
                return self._cancel_running(connection, current, worker_id, now) and False
            if current["job_type"] == "generate_narration":
                cursor = connection.execute(
                    "UPDATE photo_scores SET side_caption=?,analysis_error=NULL,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND is_deleted=0",
                    (result["side_caption"], now, current["photo_id"], current["photo_version"]),
                )
            else:
                assignments = ",".join(f"{column}=?" for column in _RESULT_COLUMNS)
                values = [result.get(column) for column in _RESULT_COLUMNS]
                values.extend((now, current["photo_id"], current["photo_version"]))
                cursor = connection.execute(
                    f"UPDATE photo_scores SET {assignments},analysis_status='succeeded',analysis_error=NULL,"
                    "updated_at=?,version=version+1 WHERE id=? AND version=? AND is_deleted=0", values,
                )
            if cursor.rowcount != 1:
                connection.execute(
                    "UPDATE admin_jobs SET status='failed',error_code='photo_version_conflict',"
                    "error_summary='照片版本已变化，结果未写入',lease_owner=NULL,lease_expires_at=NULL,"
                    "updated_at=?,finished_at=? WHERE id=?", (now, now, current["id"]),
                )
                self._record_event(
                    connection, int(current["id"]), "version_conflict", "running", "failed",
                    worker_id=worker_id, reason_code="photo_version_conflict", created_at=now,
                )
                return False
            connection.execute(
                "UPDATE admin_jobs SET status='succeeded',progress=100,error_code=NULL,error_summary=NULL,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?",
                (now, now, current["id"]),
            )
            self._record_event(
                connection, int(current["id"]), "succeeded", "running", "succeeded",
                worker_id=worker_id, reason_code="completed", created_at=now,
            )
            return True

    def complete_backfill(self, job: Mapping[str, Any], worker_id: str, digest: str) -> bool:
        """按版本条件保存最终正式文件摘要并终结回填任务。

        Args:
            job: 当前回填任务快照。
            worker_id: 当前工作进程标识。
            digest: 正式文件字节的 SHA-256。

        Returns:
            摘要和任务都成功提交时返回 True。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return False
            current = dict(row)
            if current["cancel_requested"]:
                return self._cancel_running(connection, current, worker_id, now) and False
            cursor = connection.execute(
                "UPDATE photo_scores SET content_sha256=?,updated_at=?,version=version+1 "
                "WHERE id=? AND version=? AND is_deleted=0 AND content_sha256 IS NULL",
                (digest, now, current["photo_id"], current["photo_version"]),
            )
            if cursor.rowcount != 1:
                connection.execute(
                    "UPDATE admin_jobs SET status='failed',error_code='photo_version_conflict',"
                    "error_summary='照片版本已变化，摘要未写入',lease_owner=NULL,lease_expires_at=NULL,"
                    "updated_at=?,finished_at=? WHERE id=?", (now, now, current["id"]),
                )
                self._record_event(
                    connection, int(current["id"]), "version_conflict", "running", "failed",
                    worker_id=worker_id, reason_code="photo_version_conflict", created_at=now,
                )
                return False
            connection.execute(
                "UPDATE admin_jobs SET status='succeeded',progress=100,lease_owner=NULL,lease_expires_at=NULL,"
                "updated_at=?,finished_at=? WHERE id=?", (now, now, current["id"]),
            )
            self._record_event(
                connection, int(current["id"]), "succeeded", "running", "succeeded",
                worker_id=worker_id, reason_code="content_hash_backfilled", created_at=now,
            )
            return True

    def fail_attempt(self, job: Mapping[str, Any], worker_id: str, error_code: str) -> str:
        """记录稳定错误码；取消优先，自动重试回 pending，最终失败保留业务字段。

        Args:
            job: 当前任务快照。
            worker_id: 当前工作进程标识。
            error_code: 不含异常正文的稳定错误类型。

        Returns:
            更新后的任务状态。
        """
        now = _timestamp()
        stable_error = error_code[:100]
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                return str(job["status"])
            current = dict(row)
            if current["cancel_requested"]:
                self._cancel_running(connection, current, worker_id, now)
                return "canceled"
            final = int(current["attempts"]) >= int(current["max_attempts"])
            status = "failed" if final else "pending"
            new_version = self._set_photo_state(
                connection, current, status, stable_error if final else None, now,
            )
            if current["job_type"] == "analyze_photo" and new_version is None:
                status = "failed"
                stable_error = "photo_version_conflict"
                new_version = int(current["photo_version"])
            connection.execute(
                "UPDATE admin_jobs SET status=?,progress=0,error_code=?,error_summary=?,photo_version=?,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?",
                (status, stable_error, "后台处理失败" if status == "failed" else "等待自动重试",
                 new_version, now, now if status == "failed" else None, current["id"]),
            )
            self._record_event(
                connection, int(current["id"]), "failed" if status == "failed" else "automatic_retry",
                "running", status, worker_id=worker_id, reason_code=stable_error, created_at=now,
            )
            return status

    def cancel(self, job_id: int, admin_user_id: int) -> str:
        """立即取消 pending，或为 running 设置协作取消标记并记录管理员。

        Args:
            job_id: 任务主键。
            admin_user_id: 发起取消的管理员编号。

        Returns:
            canceled 或 cancel_requested。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobTransitionError("job_not_found")
            job = dict(row)
            if job["status"] == "pending":
                new_version = self._set_photo_state(connection, job, "failed", "job_canceled", now)
                if job["job_type"] == "analyze_photo" and new_version is None:
                    raise JobTransitionError("photo_version_conflict")
                connection.execute(
                    "UPDATE admin_jobs SET status='canceled',progress=0,error_code='job_canceled',"
                    "error_summary='任务已取消',photo_version=?,finished_at=?,updated_at=? WHERE id=?",
                    (new_version, now, now, job_id),
                )
                self._record_event(
                    connection, job_id, "canceled", "pending", "canceled",
                    admin_user_id=admin_user_id, reason_code="admin_canceled", created_at=now,
                )
                return "canceled"
            if job["status"] == "running":
                connection.execute(
                    "UPDATE admin_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now, job_id)
                )
                self._record_event(
                    connection, job_id, "cancel_requested", "running", "running",
                    admin_user_id=admin_user_id, reason_code="admin_requested", created_at=now,
                )
                return "cancel_requested"
            raise JobTransitionError("job_cannot_be_canceled")

    def retry(self, job_id: int, admin_user_id: int) -> dict[str, Any]:
        """把未达到上限的 failed 或 canceled 任务恢复 pending 并刷新照片版本。

        Args:
            job_id: 任务主键。
            admin_user_id: 发起重试的管理员编号。

        Returns:
            更新后的任务。
        """
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobTransitionError("job_not_found")
            job = dict(row)
            if job["status"] not in ("failed", "canceled") or job["attempts"] >= job["max_attempts"]:
                raise JobTransitionError("job_cannot_be_retried")
            duplicate = connection.execute(
                "SELECT id FROM admin_jobs WHERE photo_id=? AND job_type=? AND status IN ('pending','running') "
                "AND id<>?", (job["photo_id"], job["job_type"], job_id),
            ).fetchone()
            if duplicate:
                raise JobTransitionError("active_job_exists")
            photo = connection.execute(
                "SELECT version FROM photo_scores WHERE id=? AND is_deleted=0", (job["photo_id"],)
            ).fetchone()
            if photo is None:
                raise JobTransitionError("photo_not_found")
            photo_version = int(photo["version"])
            if job["job_type"] == "analyze_photo":
                connection.execute(
                    "UPDATE photo_scores SET analysis_status='pending',analysis_error=NULL,updated_at=?,"
                    "version=version+1 WHERE id=? AND version=?", (now, job["photo_id"], photo_version),
                )
                photo_version += 1
            connection.execute(
                "UPDATE admin_jobs SET status='pending',progress=0,cancel_requested=0,error_code=NULL,"
                "error_summary=NULL,lease_owner=NULL,lease_expires_at=NULL,photo_version=?,updated_at=?,"
                "finished_at=NULL WHERE id=?", (photo_version, now, job_id),
            )
            self._record_event(
                connection, job_id, "retried", str(job["status"]), "pending",
                admin_user_id=admin_user_id, reason_code="admin_retry", created_at=now,
            )
            return dict(connection.execute("SELECT * FROM admin_jobs WHERE id=?", (job_id,)).fetchone())


class UploadService:
    """整批校验上传、原子发布最终文件并协调数据库补偿。"""

    TEMP_SUFFIX = ".inktime-upload.tmp"

    def __init__(
        self, image_dir: Path, repository: AdminJobRepository, max_files: int = 10,
        max_bytes: int = 20 * 1024 * 1024, max_pixels: int = 80_000_000,
    ) -> None:
        """保存上传边界，并清理仅由本系统命名的孤儿临时文件。

        Args:
            image_dir: 系统管理上传目录的根目录。
            repository: 照片与任务仓储。
            max_files: 单批最大文件数量。
            max_bytes: 单文件最大字节数。
            max_pixels: 解码后最大像素数。
        """
        self.image_dir = Path(image_dir).expanduser().resolve()
        self.staging_dir = self.image_dir / ".upload-staging"
        self.repository = repository
        self.max_files = min(10, max(1, int(max_files)))
        self.max_bytes = min(20 * 1024 * 1024, max(1, int(max_bytes)))
        self.max_pixels = min(80_000_000, max(1, int(max_pixels)))
        self.cleanup_orphan_temp_files()

    def cleanup_orphan_temp_files(self) -> int:
        """删除 uploads 树中符合系统专属后缀的孤儿临时文件。

        Returns:
            成功删除数量；不会匹配或删除其他文件。
        """
        upload_root = self.image_dir / "uploads"
        if not upload_root.exists():
            return 0
        removed = 0
        for path in upload_root.rglob(f"*{self.TEMP_SUFFIX}"):
            if path.is_file() and path.name.startswith("."):
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            LOGGER.warning("Removed orphan upload temp files, count=[%s], image_dir=[%s]", removed, self.image_dir)
        return removed

    def upload(self, files: Iterable[Any], created_by: int) -> dict[str, Any]:
        """先完整校验和规范化整批，再原子发布并用单事务创建记录。

        Args:
            files: Werkzeug FileStorage 兼容对象。
            created_by: 当前管理员编号。

        Returns:
            包含逐项 accepted/duplicate 结果及计数的批次结果。
        """
        items = [item for item in files if item and getattr(item, "filename", "")]
        if not items:
            raise UploadValidationError("至少上传一张图片")
        if len(items) > self.max_files:
            raise UploadValidationError("每批最多上传 10 张图片")
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[dict[str, Any]] = []
        published: list[Path] = []
        try:
            for item in items:
                prepared.append(self._prepare_file(item))
            for item in prepared:
                os.replace(item["temporary_path"], item["path"])
                published.append(item["path"])
            try:
                results = self.repository.create_uploaded_photos_and_jobs(prepared, created_by)
            except Exception:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
            for prepared_item, result in zip(prepared, results):
                result["original_filename"] = prepared_item["original_filename"]
                if result["status"] == "duplicate":
                    prepared_item["path"].unlink(missing_ok=True)
            counts = {"accepted": 0, "duplicate": 0, "failed": 0}
            for result in results:
                counts[result["status"]] += 1
            return {"items": results, "counts": counts, "total": len(results)}
        finally:
            for item in prepared:
                Path(item["temporary_path"]).unlink(missing_ok=True)
                Path(item["source_path"]).unlink(missing_ok=True)

    def _prepare_file(self, file_storage: Any) -> dict[str, Any]:
        """流式暂存并在正式年月目录生成已同步的规范化临时文件。"""
        original_name = Path(str(file_storage.filename)).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in {extension for extensions in _FORMATS.values() for extension in extensions}:
            raise UploadValidationError("只支持 JPEG、PNG 和 WebP")
        source_path = self.staging_dir / f"{uuid.uuid4().hex}.source"
        size = 0
        try:
            with source_path.open("xb") as output:
                while True:
                    chunk = file_storage.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadValidationError("单张图片不能超过 20 MiB")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            now = _utc_now()
            final_dir = self.image_dir / "uploads" / f"{now.year:04d}" / f"{now.month:02d}"
            final_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = final_dir / f".{uuid.uuid4().hex}{self.TEMP_SUFFIX}"
            image_format, metadata = self._normalize_image(source_path, temporary_path, suffix)
            canonical_suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[image_format]
            final_path = final_dir / f"{uuid.uuid4().hex}{canonical_suffix}"
            digest = _stream_sha256(temporary_path)
            if digest is None:
                raise RuntimeError("normalized_digest_interrupted")
            return {
                "source_path": source_path,
                "temporary_path": temporary_path,
                "path": final_path,
                "original_filename": original_name,
                "content_sha256": digest,
                "original_metadata": metadata,
            }
        except Exception:
            source_path.unlink(missing_ok=True)
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            raise

    def _extract_original_metadata(self, image: Image.Image) -> dict[str, Any]:
        """提取重编码前的安全 EXIF 拍摄字段，不保存 Orientation。"""
        exif = image.getexif()
        gps: Mapping[int, Any] = {}
        try:
            gps = exif.get_ifd(34853) or {}
        except (AttributeError, KeyError, TypeError, ValueError):
            gps = {}
        latitude = _gps_decimal(gps.get(2), gps.get(1))
        longitude = _gps_decimal(gps.get(4), gps.get(3))
        altitude = _optional_number(gps.get(6))
        if altitude is not None and int(gps.get(5, 0) or 0) == 1:
            altitude = -altitude
        exif_datetime = exif.get(36867) or exif.get(36868) or exif.get(306)
        safe = {
            "datetime": str(exif_datetime) if exif_datetime else None,
            "make": str(exif.get(271)).strip() if exif.get(271) else None,
            "model": str(exif.get(272)).strip() if exif.get(272) else None,
            "iso": int(exif.get(34855)) if exif.get(34855) is not None else None,
            "exposure_time": _optional_number(exif.get(33434)),
            "f_number": _optional_number(exif.get(33437)),
            "focal_length": _optional_number(exif.get(37386)),
            "gps_lat": latitude,
            "gps_lon": longitude,
            "gps_alt": altitude,
            "date_source": "exif" if exif_datetime else None,
        }
        exif_json = {key: value for key, value in safe.items() if value is not None}
        return {
            "exif_json": json.dumps(exif_json, ensure_ascii=False, sort_keys=True),
            "exif_datetime": safe["datetime"],
            "exif_make": safe["make"],
            "exif_model": safe["model"],
            "exif_iso": safe["iso"],
            "exif_exposure_time": safe["exposure_time"],
            "exif_f_number": safe["f_number"],
            "exif_focal_length": safe["focal_length"],
            "exif_gps_lat": latitude,
            "exif_gps_lon": longitude,
            "exif_gps_alt": altitude,
            "date_source": safe["date_source"],
        }

    def _normalize_image(self, source: Path, destination: Path, suffix: str) -> tuple[str, dict[str, Any]]:
        """解码图片、提取拍摄语义、固化方向并同步规范化文件到磁盘。"""
        try:
            with Image.open(source) as image:
                image_format = str(image.format or "").upper()
                if image_format not in _FORMATS or suffix not in _FORMATS[image_format]:
                    raise UploadValidationError("图片扩展名与实际格式不一致")
                if getattr(image, "n_frames", 1) != 1 or bool(getattr(image, "is_animated", False)):
                    raise UploadValidationError("不支持动画或多页图片")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > self.max_pixels:
                    raise UploadValidationError("解码后图片像素超过 8000 万")
                metadata = self._extract_original_metadata(image)
                image.load()
                normalized = ImageOps.exif_transpose(image)
                with destination.open("xb") as output:
                    if image_format == "JPEG":
                        normalized.convert("RGB").save(output, format="JPEG", quality=95)
                    elif image_format == "PNG":
                        normalized.save(output, format="PNG")
                    else:
                        normalized.save(output, format="WEBP", lossless=True)
                    output.flush()
                    os.fsync(output.fileno())
                return image_format, metadata
        except UploadValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            raise UploadValidationError("图片损坏、不是图片或无法安全解码") from error


class AdminJobService:
    """提供后台页面和接口可调用的任务业务操作。"""

    def __init__(self, repository: AdminJobRepository) -> None:
        """保存任务仓储。

        Args:
            repository: 后台任务仓储。
        """
        self.repository = repository

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回任务列表。

        Args:
            limit: 最大条数。

        Returns:
            任务字典列表。
        """
        return self.repository.list_jobs(limit)

    def enqueue_analysis(self, photo_ids: Iterable[int], created_by: int) -> list[dict[str, Any]]:
        """为单张或批量照片排队完整重新分析。

        Args:
            photo_ids: 照片编号集合。
            created_by: 创建任务的管理员编号。

        Returns:
            每张照片对应的任务或重复任务信息。
        """
        ids = list(dict.fromkeys(int(value) for value in photo_ids))
        if not ids or len(ids) > 100:
            raise JobTransitionError("photo_batch_size_invalid")
        return [self.repository.enqueue(photo_id, "analyze_photo", created_by, {"is_new_upload": False}) for photo_id in ids]

    def enqueue_narration(self, photo_id: int, created_by: int) -> dict[str, Any]:
        """为单张照片排队重新生成旁白。

        Args:
            photo_id: 照片编号。
            created_by: 创建任务的管理员编号。

        Returns:
            新任务或已有重复任务。
        """
        return self.repository.enqueue(photo_id, "generate_narration", created_by, {"is_new_upload": False})

    def enqueue_hash_backfill(self, created_by: int, limit: int = 1000) -> dict[str, int]:
        """创建可恢复且低优先级的历史最终文件摘要回填任务。

        Args:
            created_by: 创建任务的管理员编号。
            limit: 单次最多扫描数量。

        Returns:
            创建、重复和扫描计数。
        """
        return self.repository.enqueue_hash_backfill(created_by, limit)

    def cancel(self, job_id: int, admin_user_id: int) -> str:
        """取消 pending 或请求 running 协作取消。

        Args:
            job_id: 任务编号。
            admin_user_id: 发起操作的管理员编号。

        Returns:
            canceled 或 cancel_requested。
        """
        return self.repository.cancel(job_id, admin_user_id)

    def retry(self, job_id: int, admin_user_id: int) -> dict[str, Any]:
        """重试合法且未达到上限的终态任务。

        Args:
            job_id: 任务编号。
            admin_user_id: 发起操作的管理员编号。

        Returns:
            恢复后的任务。
        """
        return self.repository.retry(job_id, admin_user_id)


class AnalysisWorker:
    """轮询、续租并安全停止的单进程后台工作器。"""

    def __init__(
        self,
        repository: AdminJobRepository,
        analyzer: Callable[..., Mapping[str, Any]],
        narration_generator: Callable[..., str],
        worker_id: str | None = None,
        lease_seconds: int = 120,
        renew_seconds: int = 30,
        poll_seconds: float = 2.0,
        configuration_service: Any | None = None,
    ) -> None:
        """保存工作器依赖、可选统一配置服务并校验租约参数。

        Args:
            repository: 后台任务仓储。
            analyzer: 无数据库副作用的单张分析函数。
            narration_generator: 无数据库副作用的旁白函数。
            worker_id: 可选工作器标识。
            lease_seconds: 任务租约秒数。
            renew_seconds: 续租间隔秒数。
            poll_seconds: 空队列轮询间隔。
            configuration_service: 可选统一配置服务；为空时保持旧单参数调用。
        """
        if renew_seconds >= lease_seconds:
            raise ValueError("renew_seconds 必须小于 lease_seconds")
        self.repository = repository
        self.analyzer = analyzer
        self.narration_generator = narration_generator
        self.configuration_service = configuration_service
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = int(lease_seconds)
        self.renew_seconds = int(renew_seconds)
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()

    def request_stop(self, *_args: Any) -> None:
        """请求在当前任务安全边界停止，不强杀已发出的模型请求。"""
        self._stop.set()

    def run_forever(self) -> None:
        """持续恢复租约并认领任务，收到停止信号后安全退出。"""
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        while not self._stop.is_set():
            recovered = self.repository.recover_expired_leases()
            if recovered:
                LOGGER.warning("Recovered expired job leases, worker_id=[%s], count=[%s]", self.worker_id, recovered)
            job = self.repository.claim_next(self.worker_id, self.lease_seconds)
            if job is None:
                self._stop.wait(self.poll_seconds)
                continue
            self._execute(job)

    def run_once(self) -> bool:
        """认领并处理至多一个任务，便于受控运行和验证。

        Returns:
            实际认领任务时返回 True。
        """
        self.repository.recover_expired_leases()
        job = self.repository.claim_next(self.worker_id, self.lease_seconds)
        if job is None:
            return False
        self._execute(job)
        return True

    @staticmethod
    def _merge_upload_metadata(job: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """用重编码前保存的拍摄字段覆盖规范化文件产生的弱兜底值。"""
        payload = AdminJobRepository._payload(job)
        metadata = payload.get("original_metadata")
        if not isinstance(metadata, dict):
            return result
        for column in _UPLOAD_METADATA_COLUMNS:
            value = metadata.get(column)
            if value is not None:
                result[column] = value
        return result

    def _resolve_settings(self, job: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """按任务类型解析固化配置；未注入配置服务时保持旧执行方式。"""
        if self.configuration_service is None:
            return None
        scope = {
            "analyze_photo": "analysis",
            "generate_narration": "analysis",
            "backfill_content_hash": "worker",
        }.get(str(job["job_type"]))
        if scope is None:
            return None
        return self.configuration_service.resolve_task_snapshot(job, scope)

    def _execute_backfill(self, job: Mapping[str, Any]) -> None:
        """流式回填最终文件摘要，在取消、停止或高优先级任务出现时让出。"""
        job_id = int(job["id"])
        priority = int(job["priority"])
        reason: str | None = None

        def interrupted() -> bool:
            """在每个文件块边界判断停止、取消或高优先级让出。"""
            nonlocal reason
            if self._stop.is_set():
                reason = "worker_stopping"
                return True
            if self.repository.is_cancel_requested(job_id, self.worker_id):
                reason = "job_canceled"
                return True
            if self.repository.should_yield(job_id, self.worker_id, priority):
                reason = "high_priority_available"
                return True
            return False

        digest = _stream_sha256(Path(str(job["photo_path"])), interrupted)
        if digest is not None:
            self.repository.complete_backfill(job, self.worker_id, digest)
        elif reason == "job_canceled":
            self.repository.mark_canceled(job_id, self.worker_id)
        else:
            self.repository.defer(job, self.worker_id, reason or "worker_stopping")

    def _execute(self, job: Mapping[str, Any]) -> None:
        """解析任务快照后在事务外处理，并用心跳线程维持任务所有权。"""
        job_id = int(job["id"])
        photo_id = int(job["photo_id"])
        if self.repository.is_cancel_requested(job_id, self.worker_id):
            self.repository.mark_canceled(job_id, self.worker_id)
            return
        try:
            settings = self._resolve_settings(job)
        except ValueError as error:
            status = self.repository.fail_attempt(
                job, self.worker_id, "invalid_config_snapshot"
            )
            LOGGER.error(
                "Invalid background job config snapshot, job_id=[%s], photo_id=[%s], status=[%s]",
                job_id,
                photo_id,
                status,
                exc_info=error,
            )
            return

        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            """按固定间隔续租，停止事件触发后立即退出。"""
            while not heartbeat_stop.wait(self.renew_seconds):
                if not self.repository.renew_lease(job_id, self.worker_id, self.lease_seconds):
                    LOGGER.warning("Job lease renewal lost ownership, job_id=[%s], photo_id=[%s]", job_id, photo_id)
                    return

        thread = threading.Thread(target=heartbeat, name=f"inktime-job-{job_id}-heartbeat", daemon=True)
        thread.start()
        try:
            path = Path(str(job["photo_path"]))
            if job["job_type"] == "backfill_content_hash":
                self._execute_backfill(job)
            elif job["job_type"] == "generate_narration":
                if settings is None:
                    narration = self.narration_generator(path)
                else:
                    narration = self.narration_generator(
                        path,
                        settings=settings,
                        api_key=self.configuration_service.get("API_KEY"),
                    )
                self.repository.complete(
                    job, self.worker_id, {"side_caption": narration}
                )
            elif job["job_type"] == "analyze_photo":
                if settings is None:
                    analyzed = self.analyzer(path)
                else:
                    analyzed = self.analyzer(
                        path,
                        settings=settings,
                        api_key=self.configuration_service.get("API_KEY"),
                    )
                result = self._merge_upload_metadata(job, dict(analyzed))
                self.repository.complete(job, self.worker_id, result)
            else:
                status = self.repository.fail_attempt(
                    job, self.worker_id, "unsupported_job_type"
                )
                LOGGER.error(
                    "Unsupported background job type, job_id=[%s], photo_id=[%s], status=[%s]",
                    job_id,
                    photo_id,
                    status,
                )
        except Exception as error:
            error_code = type(error).__name__[:100]
            status = self.repository.fail_attempt(job, self.worker_id, error_code)
            LOGGER.exception(
                "Background job failed, job_id=[%s], photo_id=[%s], status=[%s], error_code=[%s]",
                job_id, photo_id, status, error_code,
            )
        finally:
            heartbeat_stop.set()
            thread.join(timeout=max(1.0, self.renew_seconds + 1.0))
