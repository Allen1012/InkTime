"""上传、持久化后台任务和工作进程的业务实现。"""

from __future__ import annotations

import hashlib
import io
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

try:  # HEIC 解码依赖，缺失时 HEIC 上传会以「不支持」被拒绝，其余格式不受影响
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - 取决于部署环境是否安装 pillow-heif
    HEIF_SUPPORTED = False

from src.configuration import (
    PROJECT_ROOT,
    TRASH_DIRECTORY_NAME,
    bounded_float,
    bounded_int,
    current_setting,
    parse_image_dirs,
)
from src.database import database_connection, write_transaction
from .errors import ParameterError

LOGGER = logging.getLogger(__name__)
JOB_TYPES = {"analyze_photo", "generate_narration", "backfill_content_hash"}
JOB_STATUSES = {"pending", "running", "succeeded", "failed", "canceled"}
# 允许上传的扩展名，只作为廉价前置过滤；真实格式由 PIL 检测后按白名单判断。
# 不再要求扩展名与真实格式一致：微信与浏览器另存常见「内容是 WebP、名字叫 .jpg」，
# 而落盘文件名本来就按检测出的真实格式生成，原始扩展名不影响存储正确性。
_UPLOAD_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
# 受理的真实内容格式。MPO 是「JPEG 加多帧」，HEIF 是 iPhone 默认格式
_INPUT_FORMATS = ("JPEG", "MPO", "PNG", "WEBP", "HEIF")
# 输出格式：MPO 取首帧存为 JPEG；HEIC 必须转码，因为浏览器与墨水屏渲染都不支持
_OUTPUT_FORMATS = {
    "JPEG": "JPEG",
    "MPO": "JPEG",
    "PNG": "PNG",
    "WEBP": "WEBP",
    "HEIF": "JPEG",
}
_CANONICAL_SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
# 压缩阶梯：先降质，仍超目标再逐步缩小；顺序保证优先保留分辨率
_TOP_QUALITY = 95
_COMPRESS_QUALITIES = (88, 82, 76, 70, 62)
_COMPRESS_SCALES = (1.0, 0.8, 0.65, 0.5)
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


def _format_bytes(value: int) -> str:
    """把字节上限格式化为面向用户的可读大小。"""
    if value >= 1024 * 1024:
        megabytes = value / (1024 * 1024)
        text = f"{megabytes:.1f}".rstrip("0").rstrip(".")
        return f"{text} MiB"
    if value >= 1024:
        kilobytes = value / 1024
        text = f"{kilobytes:.1f}".rstrip("0").rstrip(".")
        return f"{text} KiB"
    return f"{value} 字节"


def _optional_integer(value: Any) -> int | None:
    """把 EXIF 整数字段转换为整数，非法值返回空。

    畸形 EXIF 很常见：手机导出的照片里 ISO 字段可能是 `b'\x00'` 这种字节串，直接
    `int()` 会抛 ValueError。元数据只是可选信息，不能因为一个字段把整张照片拖死。
    """
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            return int(value.decode("ascii", errors="strict").strip() or 0)
        return int(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def _optional_text(value: Any) -> str | None:
    """把 EXIF 文本字段转换为去空白字符串，空值与异常返回空。"""
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value)
    except Exception:
        return None
    text = text.strip().strip("\x00").strip()
    return text or None


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
        configuration_service: Any | None = None,
        retry_backoff_seconds: int = 30,
    ) -> None:
        """保存数据库路径、任务上限回退值及可选的事务内配置快照提供器。

        Args:
            database_path: 后台任务使用的 SQLite 文件。
            max_attempts: 未注入配置服务时使用的最大尝试次数，允许 1 至 3。
            snapshot_provider: 接收作用域和当前连接，返回版本与稳定 JSON 文本。
            configuration_service: 可选统一配置服务；注入后 `max_attempts` 每次
                读取都取当前生效的 `JOB_MAX_ATTEMPTS`，因此后台改完立即对新任务生效。
            retry_backoff_seconds: 未注入配置服务时的失败重试退避基数秒数。
        """
        if not 1 <= int(max_attempts) <= 3:
            raise ValueError("max_attempts 必须在 1 到 3 之间")
        self.database_path = Path(database_path).expanduser().resolve()
        self._fallback_max_attempts = int(max_attempts)
        self._fallback_retry_backoff = max(0, int(retry_backoff_seconds))
        self.configuration_service = configuration_service
        self.snapshot_provider = snapshot_provider

    @property
    def max_attempts(self) -> int:
        """按当前生效配置返回新任务的最大尝试次数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "JOB_MAX_ATTEMPTS", self._fallback_max_attempts
            ),
            1,
            3,
            self._fallback_max_attempts,
        )

    @property
    def retry_backoff_seconds(self) -> int:
        """按当前生效配置返回失败重试的退避基数秒数，零表示立即重试。"""
        return bounded_int(
            current_setting(
                self.configuration_service,
                "JOB_RETRY_BACKOFF_SECONDS",
                self._fallback_retry_backoff,
            ),
            0,
            3600,
            self._fallback_retry_backoff,
        )

    def _retry_gate(self, now: datetime) -> tuple[str, list[str]]:
        """构造失败重试的时间门禁条件。

        没有门禁时，`fail_attempt` 把任务打回 pending 后，工作循环下一圈会立刻重新
        认领它——因为只有队列为空才等待轮询间隔。于是三次尝试会在几秒内烧光：上游
        只要抖动一次或触发限流，照片就直接被判定失败。退避按尝试次数指数增长，
        既给上游恢复时间，也避免我们自己把限流打出来。

        使用现有的 `updated_at` 字段而不是新增列，因此**不需要数据库迁移**：
        `fail_attempt` 每次失败都会刷新它。

        Args:
            now: 当前协调世界时。

        Returns:
            可直接拼进 WHERE 的 SQL 条件与对应参数。
        """
        base = self.retry_backoff_seconds
        if base <= 0:
            return "", []
        # 可认领任务满足 attempts < max_attempts，而 max_attempts 上限为 3，
        # 因此只会出现 0、1、2 三种尝试次数；三次以上分支仅作兜底。
        # 人工重试会清空 error_code（见 retry），据此与自动重排队区分开：退避是为了
        # 防止自动重试把上游打爆，管理员点下重试就该立刻执行，不该让人干等。
        clauses = ["j.attempts = 0", "j.error_code IS NULL"]
        parameters: list[str] = []
        for attempts, multiplier in ((1, 1), (2, 2), (3, 4)):
            comparison = ">=" if attempts == 3 else "="
            clauses.append(f"(j.attempts {comparison} ? AND j.updated_at <= ?)")
            parameters.extend(
                [str(attempts), _timestamp(now - timedelta(seconds=base * multiplier))]
            )
        return " AND (" + " OR ".join(clauses) + ")", parameters

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
    def _coordinate_recovered_analysis(
        connection: Any,
        job: Mapping[str, Any],
        status: str,
        error_code: str | None,
        now: str,
    ) -> int | None:
        """按当前照片版本收口租约过期但仍为 running 的分析状态。

        旁白、摘要或管理员编辑可能合法推进全局照片版本，因此租约恢复不能继续使用
        任务认领时的旧版本；同类型活跃任务唯一约束保证此时仍可安全收口唯一分析任务。
        仅允许仍为 running 的分析状态转换，绝不覆盖已被明确改变的分析状态。
        """
        if job["job_type"] != "analyze_photo":
            return int(job["photo_version"])
        photo = connection.execute(
            "SELECT version,analysis_status FROM photo_scores WHERE id=? AND is_deleted=0",
            (job["photo_id"],),
        ).fetchone()
        if photo is None or photo["analysis_status"] != "running":
            return None
        current_version = int(photo["version"])
        cursor = connection.execute(
            "UPDATE photo_scores SET analysis_status=?,analysis_error=?,updated_at=?,version=version+1 "
            "WHERE id=? AND version=? AND is_deleted=0 AND analysis_status='running'",
            (status, error_code, now, job["photo_id"], current_version),
        )
        return current_version + 1 if cursor.rowcount == 1 else None

    @staticmethod
    def _cancel_running(
        connection: Any, job: Mapping[str, Any], worker_id: str, now: str
    ) -> bool:
        """终结当前工作进程的运行任务，版本变化时不覆盖较新的照片状态。"""
        new_version = AdminJobRepository._set_photo_state(
            connection, job, "failed", "job_canceled", now
        )
        if job["job_type"] == "analyze_photo" and new_version is None:
            new_version = int(job["photo_version"])
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
        """逐项恢复过期租约；取消请求优先闭合，其他任务按尝试次数恢复。"""
        now = _timestamp()
        count = 0
        with write_transaction(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM admin_jobs WHERE status='running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=? ORDER BY id", (now,),
            ).fetchall()
            for row in rows:
                job = dict(row)
                if job["cancel_requested"]:
                    photo = connection.execute(
                        "SELECT is_deleted,version,analysis_status FROM photo_scores WHERE id=?",
                        (job["photo_id"],),
                    ).fetchone()
                    deleted = photo is None or bool(photo["is_deleted"])
                    reason_code = "photo_deleted" if deleted else "job_canceled"
                    new_version = int(job["photo_version"])
                    if job["job_type"] == "analyze_photo" and deleted and photo is not None:
                        photo_cursor = connection.execute(
                            "UPDATE photo_scores SET analysis_status='failed',"
                            "analysis_error='job_canceled',updated_at=?,version=version+1 "
                            "WHERE id=? AND version=? AND is_deleted=1 "
                            "AND analysis_status IN ('pending','running')",
                            (now, job["photo_id"], photo["version"]),
                        )
                        if photo_cursor.rowcount == 1:
                            new_version = int(photo["version"]) + 1
                    elif job["job_type"] == "analyze_photo":
                        updated_version = self._set_photo_state(
                            connection, job, "failed", "job_canceled", now,
                        )
                        if updated_version is not None:
                            new_version = updated_version
                    connection.execute(
                        "UPDATE admin_jobs SET status='canceled',progress=0,photo_version=?,"
                        "lease_owner=NULL,lease_expires_at=NULL,error_code=?,error_summary=?,"
                        "updated_at=?,finished_at=? WHERE id=? AND status='running'",
                        (
                            new_version,
                            reason_code,
                            "照片已移入回收站" if deleted else "任务已取消",
                            now,
                            now,
                            job["id"],
                        ),
                    )
                    self._record_event(
                        connection, int(job["id"]), "canceled", "running", "canceled",
                        worker_id=str(job["lease_owner"] or "") or None,
                        reason_code=reason_code, created_at=now,
                    )
                    count += 1
                    continue

                exhausted = int(job["attempts"]) >= int(job["max_attempts"])
                recovered_status = "failed" if exhausted else "pending"
                recovered_error = "max_attempts_exceeded" if exhausted else None
                new_version = self._coordinate_recovered_analysis(
                    connection, job, recovered_status, recovered_error, now,
                )
                conflict = job["job_type"] == "analyze_photo" and new_version is None
                if conflict:
                    new_status = "failed"
                    new_version = int(job["photo_version"])
                    error_code = "photo_version_conflict"
                    error_summary = "照片分析状态已变化，任务未恢复"
                    finished_at = now
                    reason_code = "photo_version_conflict"
                else:
                    new_status = recovered_status
                    error_code = recovered_error
                    error_summary = "任务租约过期且已达到最大尝试次数" if exhausted else None
                    finished_at = now if exhausted else None
                    reason_code = "max_attempts_exceeded" if exhausted else "lease_expired"
                cursor = connection.execute(
                    "UPDATE admin_jobs SET status=?,progress=0,photo_version=?,lease_owner=NULL,"
                    "lease_expires_at=NULL,error_code=?,error_summary=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND status='running'",
                    (new_status, new_version, error_code, error_summary, now, finished_at, job["id"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("job_lease_recovery_lost_inside_transaction")
                self._record_event(
                    connection, int(job["id"]), "lease_recovered", "running", new_status,
                    worker_id=str(job["lease_owner"] or "") or None,
                    reason_code=reason_code, created_at=now,
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
        gate, gate_parameters = self._retry_gate(now_value)
        with write_transaction(self.database_path) as connection:
            candidate = connection.execute(
                "SELECT j.*,p.version AS current_photo_version FROM admin_jobs j "
                "JOIN photo_scores p ON p.id=j.photo_id AND p.is_deleted=0 "
                "WHERE j.status='pending' AND j.attempts<j.max_attempts"
                f"{gate} "
                "ORDER BY j.priority DESC,j.created_at,j.id LIMIT 1",
                gate_parameters,
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
        """仅为当前持有者的未过期、未取消运行任务续租。

        Args:
            job_id: 任务主键。
            worker_id: 当前工作进程标识。
            lease_seconds: 新租约秒数。

        Returns:
            成功续租返回 True。
        """
        now_value = _utc_now()
        now = _timestamp(now_value)
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND status='running' "
                "AND lease_owner=? AND cancel_requested=0 AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at>?",
                (
                    _timestamp(now_value + timedelta(seconds=max(1, lease_seconds))),
                    now,
                    job_id,
                    worker_id,
                    now,
                ),
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
        """仅把可中断的历史最终文件摘要回填任务放回 pending，并退还本次尝试。

        照片分析和旁白任务具有额外的照片状态约束，不允许通过主动让出改变生命周期。

        Args:
            job: 当前 backfill_content_hash 任务快照。
            worker_id: 当前工作进程标识。
            reason_code: worker_stopping 或 high_priority_available。

        Returns:
            实际让出时返回 True。
        """
        if job["job_type"] != "backfill_content_hash":
            raise JobTransitionError("job_cannot_be_deferred")
        now = _timestamp()
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE admin_jobs SET status='pending',progress=0,attempts=MAX(0,attempts-1),"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE id=? AND status='running' "
                "AND lease_owner=? AND job_type='backfill_content_hash'",
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

    def fail_attempt(
        self,
        job: Mapping[str, Any],
        worker_id: str,
        error_code: str,
        detail: str | None = None,
    ) -> str:
        """记录稳定错误码与可读详情；取消优先，自动重试回 pending。

        `detail` 会写入 `error_summary` 并直接展示在后台任务页。没有它的时候，页面上
        只有「后台处理失败」加一个异常类名，真正的原因（例如上游返回的
        `invalid_parameter_error: messages parameter length invalid`，实际是模型名配错）
        只能翻工作进程日志才能看到。

        Args:
            job: 当前任务快照。
            worker_id: 当前工作进程标识。
            error_code: 不含异常正文的稳定错误类型。
            detail: 可读失败原因，会被压成单行并截断到 300 字。

        Returns:
            更新后的任务状态。
        """
        now = _timestamp()
        stable_error = error_code[:100]
        readable = " ".join(str(detail).split())[:300] if detail else ""
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
                (status, stable_error,
                 readable or ("后台处理失败" if status == "failed" else "等待自动重试"),
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
                # started_at 一并清空：认领时只在首次（started_at IS NULL）固化配置
                # 快照，清空它意味着人工重试会按当前配置重新取快照。使用者对「重试」
                # 的预期就是「用现在的状态再来一次」，改完模型点重试却仍用旧配置最难
                # 排查。任务的执行历史仍由 admin_job_events 完整保留。
                "UPDATE admin_jobs SET status='pending',progress=0,cancel_requested=0,error_code=NULL,"
                "error_summary=NULL,lease_owner=NULL,lease_expires_at=NULL,photo_version=?,updated_at=?,"
                "finished_at=NULL,started_at=NULL WHERE id=?", (photo_version, now, job_id),
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
        max_bytes: int = 67108864, max_pixels: int = 80_000_000,
        configuration_service: Any | None = None,
    ) -> None:
        """保存上传边界回退值，并清理仅由本系统命名的孤儿临时文件。

        Args:
            image_dir: 照片目录配置，可用分号分隔多个；上传只写第一个主目录。
            repository: 照片与任务仓储。
            max_files: 未注入配置服务时的单批最大文件数量。
            max_bytes: 未注入配置服务时的单文件最大字节数。
            max_pixels: 未注入配置服务时的解码后最大像素数。
            configuration_service: 可选统一配置服务；注入后三项上限与主目录每次
                读取都取当前生效值，后台改完无需重启即对下一次上传生效。
        """
        self._fallback_image_dirs = parse_image_dirs(image_dir, base_dir=PROJECT_ROOT)
        self.repository = repository
        self.configuration_service = configuration_service
        self._fallback_max_files = min(10, max(1, int(max_files)))
        self._fallback_max_bytes = min(104857600, max(1, int(max_bytes)))
        self._fallback_max_pixels = min(80_000_000, max(1, int(max_pixels)))
        self.cleanup_orphan_temp_files()

    @property
    def image_dir(self) -> Path:
        """按当前生效配置返回主照片目录：上传与暂存只写这里。"""
        raw = current_setting(self.configuration_service, "IMAGE_DIR", None)
        if raw is None or not str(raw).strip():
            return self._fallback_image_dirs[0]
        try:
            return parse_image_dirs(raw, base_dir=PROJECT_ROOT)[0]
        except ValueError as error:
            LOGGER.error(
                "Invalid IMAGE_DIR configuration for uploads, falling back, error=[%s]",
                error,
            )
            return self._fallback_image_dirs[0]

    @property
    def staging_dir(self) -> Path:
        """返回主目录下的上传暂存目录。"""
        return self.image_dir / ".upload-staging"

    @property
    def max_files(self) -> int:
        """按当前生效配置返回单批允许的最大文件数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "UPLOAD_MAX_FILES", self._fallback_max_files
            ),
            1,
            10,
            self._fallback_max_files,
        )

    @property
    def max_bytes(self) -> int:
        """按当前生效配置返回单文件允许的最大字节数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "UPLOAD_MAX_BYTES", self._fallback_max_bytes
            ),
            1,
            104857600,
            self._fallback_max_bytes,
        )

    @property
    def target_bytes(self) -> int:
        """按当前生效配置返回单张照片的目标体积字节数，零表示不压缩。"""
        return bounded_int(
            current_setting(self.configuration_service, "UPLOAD_TARGET_BYTES", 0),
            0,
            104857600,
            0,
        )

    @property
    def max_long_edge(self) -> int:
        """按当前生效配置返回落盘图片的长边像素上限，零表示不缩放。"""
        return bounded_int(
            current_setting(self.configuration_service, "UPLOAD_MAX_LONG_EDGE", 0),
            0,
            20000,
            0,
        )

    @property
    def max_pixels(self) -> int:
        """按当前生效配置返回解码后允许的最大像素数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "UPLOAD_MAX_PIXELS", self._fallback_max_pixels
            ),
            1,
            80_000_000,
            self._fallback_max_pixels,
        )

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
        # 同一批次内固定一次上限快照，避免批中途配置变更导致前后文件判定不一致。
        max_files = self.max_files
        max_bytes = self.max_bytes
        max_pixels = self.max_pixels
        target_bytes = self.target_bytes
        max_long_edge = self.max_long_edge
        if len(items) > max_files:
            raise UploadValidationError(f"每批最多上传 {max_files} 张图片")
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[dict[str, Any]] = []
        # 逐文件独立处理：一张不合法不再拖累整批。手机相册里混进一个动图或不支持的
        # 格式很常见，让使用者重新勾选十张照片的代价远大于让合法文件先入库。
        failures: list[dict[str, Any]] = []
        for item in items:
            original_name = Path(str(getattr(item, "filename", "") or "")).name
            try:
                prepared.append(
                    self._prepare_file(
                        item,
                        max_bytes=max_bytes,
                        max_pixels=max_pixels,
                        target_bytes=target_bytes,
                        max_long_edge=max_long_edge,
                    )
                )
            except UploadValidationError as error:
                LOGGER.warning(
                    "Upload item rejected, filename=[%s], reason=[%s]",
                    original_name, error,
                )
                failures.append(
                    {
                        "status": "failed",
                        "photo_id": None,
                        "job_id": None,
                        "path": None,
                        "original_filename": original_name,
                        "message": str(error),
                    }
                )
        published: list[Path] = []
        try:
            if not prepared:
                return self._batch_result([], failures, items)
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
            return self._batch_result(results, failures, items)
        finally:
            for item in prepared:
                Path(item["temporary_path"]).unlink(missing_ok=True)
                Path(item["source_path"]).unlink(missing_ok=True)

    @staticmethod
    def _batch_result(
        results: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        items: list[Any],
    ) -> dict[str, Any]:
        """把成功与失败项按上传顺序合并，前端据此逐条标注状态。

        Args:
            results: 已落库项的结果。
            failures: 被拒绝项的结果。
            items: 原始上传项，用于恢复顺序。

        Returns:
            含逐项结果与计数的批次结果。
        """
        by_name: dict[str, list[dict[str, Any]]] = {}
        for entry in [*results, *failures]:
            by_name.setdefault(str(entry.get("original_filename") or ""), []).append(entry)
        ordered: list[dict[str, Any]] = []
        for item in items:
            name = Path(str(getattr(item, "filename", "") or "")).name
            bucket = by_name.get(name)
            if bucket:
                ordered.append(bucket.pop(0))
        # 兜底：文件名重复或异常导致未能配对的项也不能丢
        for bucket in by_name.values():
            ordered.extend(bucket)
        counts = {"accepted": 0, "duplicate": 0, "failed": 0}
        for entry in ordered:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return {"items": ordered, "counts": counts, "total": len(ordered)}

    def _prepare_file(
        self,
        file_storage: Any,
        *,
        max_bytes: int,
        max_pixels: int,
        target_bytes: int,
        max_long_edge: int,
    ) -> dict[str, Any]:
        """流式暂存并在正式年月目录生成已同步的规范化临时文件。

        Args:
            file_storage: Werkzeug FileStorage 兼容对象。
            max_bytes: 本批生效的单文件字节上限。
            max_pixels: 本批生效的解码像素上限。
            target_bytes: 本批生效的落盘目标体积，零表示不压缩。
            max_long_edge: 本批生效的长边像素上限，零表示不缩放。

        Returns:
            含暂存路径、最终路径、摘要与原始元数据的准备结果。
        """
        original_name = Path(str(file_storage.filename)).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in _UPLOAD_SUFFIXES:
            raise UploadValidationError("只支持 JPEG、PNG、WebP 和 HEIC")
        source_path = self.staging_dir / f"{uuid.uuid4().hex}.source"
        size = 0
        try:
            with source_path.open("xb") as output:
                while True:
                    chunk = file_storage.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadValidationError(
                            f"单张图片不能超过 {_format_bytes(max_bytes)}"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            now = _utc_now()
            final_dir = self.image_dir / "uploads" / f"{now.year:04d}" / f"{now.month:02d}"
            final_dir.mkdir(parents=True, exist_ok=True)
            temporary_path = final_dir / f".{uuid.uuid4().hex}{self.TEMP_SUFFIX}"
            image_format, metadata = self._normalize_image(
                source_path,
                temporary_path,
                suffix,
                max_pixels=max_pixels,
                target_bytes=target_bytes,
                max_long_edge=max_long_edge,
            )
            canonical_suffix = _CANONICAL_SUFFIXES[image_format]
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
        """提取重编码前的安全 EXIF 拍摄字段，不保存 Orientation。

        整段读取都做兜底：EXIF 由拍摄设备与各种修图软件写入，畸形字段在手机照片里
        很常见（例如 ISO 写成字节串）。元数据只是可选的锦上添花，任何解析问题都不
        应阻断照片本身入库，最坏情况是这张照片没有拍摄信息。
        """
        try:
            return self._read_exif(image)
        except Exception as error:  # 兜底：任何 EXIF 解析异常都不影响上传
            LOGGER.warning(
                "EXIF extraction failed, photo still accepted, error=[%s: %s]",
                type(error).__name__, error,
            )
            return {
                "exif_json": "{}", "exif_datetime": None, "exif_make": None,
                "exif_model": None, "exif_iso": None, "exif_exposure_time": None,
                "exif_f_number": None, "exif_focal_length": None, "exif_gps_lat": None,
                "exif_gps_lon": None, "exif_gps_alt": None, "date_source": None,
            }

    def _read_exif(self, image: Image.Image) -> dict[str, Any]:
        """读取并归一化 EXIF 拍摄字段。"""
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
        exif_datetime = _optional_text(
            exif.get(36867) or exif.get(36868) or exif.get(306)
        )
        safe = {
            "datetime": exif_datetime,
            "make": _optional_text(exif.get(271)),
            "model": _optional_text(exif.get(272)),
            "iso": _optional_integer(exif.get(34855)),
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

    def _normalize_image(
        self,
        source: Path,
        destination: Path,
        suffix: str,
        *,
        max_pixels: int,
        target_bytes: int,
        max_long_edge: int,
    ) -> tuple[str, dict[str, Any]]:
        """解码图片、提取拍摄语义、固化方向，按目标体积编码后同步落盘。

        **不要求扩展名与真实格式一致**：微信与浏览器另存的图片常见「内容是 WebP、
        文件名是 .jpg」，手机与电脑都能正常查看，没有理由拒绝。落盘文件名本来就按
        检测出的真实格式生成，原始扩展名不影响存储正确性；真正有安全意义的是内容
        格式必须在白名单内，这一条保持不变。

        Args:
            source: 暂存的原始文件。
            destination: 规范化后写入的临时目标。
            suffix: 上传时的原始扩展名，仅用于日志与兼容判断。
            max_pixels: 解码后允许的最大像素数。
            target_bytes: 目标体积字节数，零表示不压缩。
            max_long_edge: 长边像素上限，零表示不缩放。

        Returns:
            输出格式与原始拍摄元数据。
        """
        try:
            with Image.open(source) as image:
                image_format = str(image.format or "").upper()
                if image_format not in _INPUT_FORMATS:
                    raise UploadValidationError("只支持 JPEG、PNG、WebP 和 HEIC")
                # 多帧文件统一取首帧转为静态图：MPO 是「JPEG 加多帧」，HEIF 可能含连拍
                # 或深度图，手机相册里的动态照片、动图也属于这一类。展示与渲染链路只
                # 处理静态图，但没有理由因此拒收——取首帧就是使用者想要的那一张。
                frames = int(getattr(image, "n_frames", 1) or 1)
                if frames > 1 or bool(getattr(image, "is_animated", False)):
                    LOGGER.info(
                        "Multi-frame upload reduced to first frame, format=[%s], frames=[%s]",
                        image_format, frames,
                    )
                    image.seek(0)
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    raise UploadValidationError(f"解码后图片像素不能超过 {max_pixels:,}")
                metadata = self._extract_original_metadata(image)
                image.load()
                normalized = ImageOps.exif_transpose(image)
        except UploadValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            # 带上底层原因：只说「损坏或无法解码」时，使用者与维护者都无法判断到底是
            # 格式不被支持、文件被截断，还是编解码器缺失。
            reason = " ".join(str(error).split())[:150]
            raise UploadValidationError(
                f"无法解码该图片（{type(error).__name__}: {reason}）" if reason
                else f"无法解码该图片（{type(error).__name__}）"
            ) from error

        output_format = _OUTPUT_FORMATS[image_format]
        payload = self._encode_within_target(
            normalized,
            output_format,
            target_bytes=target_bytes,
            max_long_edge=max_long_edge,
        )
        with destination.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        return output_format, metadata

    def _encode_within_target(
        self,
        image: Image.Image,
        output_format: str,
        *,
        target_bytes: int,
        max_long_edge: int,
    ) -> bytes:
        """把图片编码为不超过目标体积的字节，先缩放再逐档降质。

        先按长边缩放能一次性砍掉大部分体积，也让后续多次试编码的成本可控：五十兆
        的原图解码后可能上亿像素，直接反复编码会很慢。JPEG 与 WebP 走质量阶梯，
        必要时再逐步缩小；PNG 只缩放不降质，因为截图类图片降质会让文字发虚，转成
        JPEG 更糟。

        Args:
            image: 已固化方向的图片。
            output_format: 输出格式，取值 JPEG、PNG 或 WEBP。
            target_bytes: 目标体积字节数，零表示不限制。
            max_long_edge: 长边像素上限，零表示不缩放。

        Returns:
            最终写盘的字节内容。
        """
        working = image
        if max_long_edge > 0 and max(working.size) > max_long_edge:
            working = ImageOps.contain(
                working, (max_long_edge, max_long_edge), Image.LANCZOS
            )

        best = self._encode(working, output_format, quality=_TOP_QUALITY)
        if target_bytes <= 0 or len(best) <= target_bytes or output_format == "PNG":
            if target_bytes > 0 and len(best) > target_bytes and output_format == "PNG":
                LOGGER.info(
                    "PNG kept above target to preserve text sharpness, size=[%s], target=[%s]",
                    len(best), target_bytes,
                )
            return best

        for scale in _COMPRESS_SCALES:
            candidate_image = working
            if scale < 1.0:
                size = (
                    max(1, int(working.width * scale)),
                    max(1, int(working.height * scale)),
                )
                candidate_image = working.resize(size, Image.LANCZOS)
            for quality in _COMPRESS_QUALITIES:
                candidate = self._encode(
                    candidate_image, output_format, quality=quality
                )
                if len(candidate) < len(best):
                    best = candidate
                if len(candidate) <= target_bytes:
                    return candidate
        LOGGER.warning(
            "Upload could not reach target size, kept smallest attempt, size=[%s], target=[%s]",
            len(best), target_bytes,
        )
        return best

    @staticmethod
    def _encode(image: Image.Image, output_format: str, *, quality: int) -> bytes:
        """按输出格式编码为字节；JPEG 需要先转 RGB 以去掉透明通道。"""
        buffer = io.BytesIO()
        if output_format == "JPEG":
            image.convert("RGB").save(
                buffer, format="JPEG", quality=quality, optimize=True, progressive=True
            )
        elif output_format == "PNG":
            image.save(buffer, format="PNG", optimize=True)
        else:
            # 无损 WebP 体积常与原图相当，需要压到目标时改用有损
            lossless = quality >= _TOP_QUALITY
            image.save(
                buffer, format="WEBP", lossless=lossless, quality=quality
            )
        return buffer.getvalue()


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
            lease_seconds: 未注入配置服务时的任务租约秒数。
            renew_seconds: 未注入配置服务时的续租间隔秒数。
            poll_seconds: 未注入配置服务时的空队列轮询间隔。
            configuration_service: 可选统一配置服务；注入后租约、续租与轮询间隔
                在每轮循环、每次续租时按当前生效配置读取，改完无需重启工作进程。
        """
        if renew_seconds >= lease_seconds:
            raise ValueError("renew_seconds 必须小于 lease_seconds")
        self.repository = repository
        self.analyzer = analyzer
        self.narration_generator = narration_generator
        self.configuration_service = configuration_service
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._fallback_lease_seconds = int(lease_seconds)
        self._fallback_renew_seconds = int(renew_seconds)
        self._fallback_poll_seconds = float(poll_seconds)
        self._stop = threading.Event()

    @property
    def lease_seconds(self) -> int:
        """按当前生效配置返回任务租约秒数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "JOB_LEASE_SECONDS", self._fallback_lease_seconds
            ),
            2,
            86400,
            self._fallback_lease_seconds,
        )

    @property
    def renew_seconds(self) -> int:
        """按当前生效配置返回续租间隔，并强制小于当前租约时长。

        注册表逐项校验无法表达跨项约束，因此这里兜底收敛：一旦续租间隔被改成
        大于等于租约时长，取租约减一秒并告警，避免心跳线程永远来不及续租。
        """
        lease = self.lease_seconds
        renew = bounded_int(
            current_setting(
                self.configuration_service, "JOB_RENEW_SECONDS", self._fallback_renew_seconds
            ),
            1,
            86400,
            self._fallback_renew_seconds,
        )
        if renew >= lease:
            LOGGER.warning(
                "Job renew interval not shorter than lease, clamped, worker_id=[%s], "
                "renew_seconds=[%s], lease_seconds=[%s]",
                self.worker_id, renew, lease,
            )
            return max(1, lease - 1)
        return renew

    @property
    def poll_seconds(self) -> float:
        """按当前生效配置返回空队列轮询间隔。"""
        return bounded_float(
            current_setting(
                self.configuration_service, "JOB_POLL_SECONDS", self._fallback_poll_seconds
            ),
            0.1,
            3600.0,
            self._fallback_poll_seconds,
        )

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
            status = self.repository.fail_attempt(
                job, self.worker_id, error_code, detail=str(error)
            )
            LOGGER.exception(
                "Background job failed, job_id=[%s], photo_id=[%s], status=[%s], error_code=[%s]",
                job_id, photo_id, status, error_code,
            )
        finally:
            heartbeat_stop.set()
            thread.join(timeout=max(1.0, self.renew_seconds + 1.0))


class LibraryScanService:
    """扫描照片目录，把尚未入库的图片登记为待分析记录并排队分析。

    与上传入口的区别：文件已经位于照片目录内，因此只做登记与排队，不移动、
    不重编码文件。拍摄时间、GPS、城市等元数据由后续的分析任务统一提取，
    因此这里沿用重新分析所用的 is_new_upload=False 约定。
    """

    _SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    _TRASH_DIRECTORY_NAME = TRASH_DIRECTORY_NAME

    def __init__(
        self,
        image_directory: Any,
        database_path: Any,
        max_attempts: int,
        batch_limit: int = 500,
        configuration_service: Any | None = None,
    ) -> None:
        """记录扫描根目录、数据库位置、任务重试上限回退值与单次登记上限。

        Args:
            image_directory: 扫描根目录，可用分号分隔配置多个。
            database_path: 照片与任务所在的 SQLite 文件。
            max_attempts: 未注入配置服务时的任务重试上限。
            batch_limit: 单次登记上限。
            configuration_service: 可选统一配置服务；注入后 `max_attempts` 按当前
                生效的 `JOB_MAX_ATTEMPTS` 动态读取，扫描根目录按 `IMAGE_DIR` 动态解析。
        """
        self._fallback_image_dirs = parse_image_dirs(
            image_directory, base_dir=PROJECT_ROOT
        )
        self.database_path = database_path
        self._fallback_max_attempts = max(1, min(int(max_attempts), 3))
        self.configuration_service = configuration_service
        self.batch_limit = max(1, min(int(batch_limit), 2000))

    @property
    def image_dirs(self) -> tuple[Path, ...]:
        """按当前生效配置返回全部扫描根目录。"""
        raw = current_setting(self.configuration_service, "IMAGE_DIR", None)
        if raw is None or not str(raw).strip():
            return self._fallback_image_dirs
        try:
            return parse_image_dirs(raw, base_dir=PROJECT_ROOT)
        except ValueError as error:
            LOGGER.error(
                "Invalid IMAGE_DIR configuration for scan, falling back, error=[%s]",
                error,
            )
            return self._fallback_image_dirs

    @property
    def image_directory(self) -> Path:
        """返回主扫描根目录，保留单目录时期的属性名。"""
        return self.image_dirs[0]

    @property
    def max_attempts(self) -> int:
        """按当前生效配置返回扫描登记任务的最大尝试次数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "JOB_MAX_ATTEMPTS", self._fallback_max_attempts
            ),
            1,
            3,
            self._fallback_max_attempts,
        )

    def _collect(self) -> list[Path]:
        """递归收集全部照片目录下可分析的图片，跳过各根自己的回收站与截图。"""
        images: list[Path] = []
        for root in self.image_dirs:
            if not root.is_dir():
                LOGGER.warning("Configured image directory unavailable, path=[%s]", root)
                continue
            # 每个根各有自己的 .trash，必须逐根跳过，不能只跳过主目录的回收站。
            trash_directory = root / self._TRASH_DIRECTORY_NAME
            for candidate in sorted(root.rglob("*")):
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in self._SUFFIXES:
                    continue
                if trash_directory in candidate.parents:
                    continue
                # 与批量分析脚本保持一致：截图没有拍摄信息，不进入候选池
                if "screenshot" in str(candidate).lower():
                    continue
                images.append(candidate)
        return images

    def scan(self, created_by: int) -> dict[str, Any]:
        """登记本次发现的新照片并创建分析任务。

        Args:
            created_by: 触发扫描的管理员编号。

        Returns:
            含发现总数、本次登记数、已在库数与剩余待登记数的统计。
        """
        images = self._collect()
        now = _timestamp()
        payload = json.dumps({"is_new_upload": False}, ensure_ascii=False, sort_keys=True)
        registered = 0
        pending_total = 0
        with write_transaction(self.database_path) as connection:
            # 已软删除的记录同样占用 path 唯一约束，因此不限定 is_deleted
            indexed = {
                str(row["path"])
                for row in connection.execute("SELECT path FROM photo_scores")
            }
            candidates = [item for item in images if str(item) not in indexed]
            pending_total = len(candidates)
            for candidate in candidates[: self.batch_limit]:
                cursor = connection.execute(
                    "INSERT INTO photo_scores (path,original_filename,analysis_status,analysis_error,"
                    "is_deleted,created_at,updated_at,version) VALUES (?,?,'pending',NULL,0,?,?,1)",
                    (str(candidate), candidate.name, now, now),
                )
                photo_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO admin_jobs (job_type,status,payload_json,priority,progress,created_by,"
                    "photo_id,photo_version,attempts,max_attempts,cancel_requested,created_at,updated_at) "
                    "VALUES ('analyze_photo','pending',?,100,0,?,?,1,0,?,0,?,?)",
                    (payload, int(created_by), photo_id, self.max_attempts, now, now),
                )
                registered += 1
        return {
            "discovered": len(images),
            "registered": registered,
            "already_indexed": len(images) - pending_total,
            "remaining": max(0, pending_total - registered),
            "batch_limit": self.batch_limit,
        }
