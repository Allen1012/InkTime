"""阶段六照片回收站、永久删除、维护任务与渲染产物发布。"""

from __future__ import annotations

import fcntl
import importlib
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from src.configuration import (
    PROJECT_ROOT,
    TRASH_DIRECTORY_NAME,
    bounded_float,
    bounded_int,
    current_setting,
    like_prefix,
    parse_image_dirs,
)
from src.database import database_connection, write_transaction

from .errors import ConflictError, ParameterError, ResourceNotFoundError, ServerError

LOGGER = logging.getLogger(__name__)
MAINTENANCE_JOB_TYPES = {"render_display", "cleanup_expired_trash"}
MAINTENANCE_JOB_STATUSES = {"pending", "running", "succeeded", "failed", "canceled"}


def _utc_timestamp(value: datetime | None = None) -> str:
    """返回可按字典序比较的秒级协调世界时字符串。"""
    return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _positive_integer(value: Any, name: str) -> int:
    """把外部值严格转换为正整数。"""
    if isinstance(value, bool):
        raise ParameterError(f"{name} 必须为正整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise ParameterError(f"{name} 必须为正整数") from error
    if normalized < 1:
        raise ParameterError(f"{name} 必须为正整数")
    return normalized


class _ManagedPathMissingError(RuntimeError):
    """表示受管路径的父目录或叶子在安全访问期间不存在。"""


class MaintenanceJobRepository:
    """持久化独立维护任务、事件和显示产物状态。"""

    def __init__(
        self,
        database_path: Path,
        max_attempts: int = 3,
        snapshot_provider: Callable[[str, Any], tuple[int, str]] | None = None,
        configuration_service: Any | None = None,
    ) -> None:
        """保存数据库路径、任务上限回退值及可选的事务内配置快照提供器。

        Args:
            database_path: 维护任务使用的 SQLite 文件。
            max_attempts: 未注入配置服务时的最多尝试次数，限制为一至三次。
            snapshot_provider: 接收作用域和当前连接，返回版本与稳定 JSON 文本。
            configuration_service: 可选统一配置服务；注入后 `max_attempts` 按当前
                生效的 `JOB_MAX_ATTEMPTS` 动态读取。
        """
        self.database_path = Path(database_path).expanduser().resolve()
        self._fallback_max_attempts = max(1, min(int(max_attempts), 3))
        self.configuration_service = configuration_service
        self.snapshot_provider = snapshot_provider

    @property
    def max_attempts(self) -> int:
        """按当前生效配置返回新维护任务的最大尝试次数。"""
        return bounded_int(
            current_setting(
                self.configuration_service, "JOB_MAX_ATTEMPTS", self._fallback_max_attempts
            ),
            1,
            3,
            self._fallback_max_attempts,
        )

    @staticmethod
    def _event(
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
        """在任务事务内写入不含异常正文的稳定事件。"""
        connection.execute(
            "INSERT INTO admin_maintenance_job_events "
            "(job_id,event_type,old_status,new_status,admin_user_id,worker_id,reason_code,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                job_id,
                event_type,
                old_status,
                new_status,
                admin_user_id,
                worker_id,
                reason_code[:100] if reason_code else None,
                created_at or _utc_timestamp(),
            ),
        )

    @staticmethod
    def _payload(job: Mapping[str, Any]) -> dict[str, Any]:
        """解析系统生成的维护任务载荷，损坏内容按空字典处理。"""
        try:
            payload = json.loads(str(job["payload_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _render_generation(cls, job: Mapping[str, Any]) -> int | None:
        """读取非负渲染目标代次，缺失或非法时返回空值。"""
        value = cls._payload(job).get("render_generation")
        if isinstance(value, bool):
            return None
        try:
            generation = int(value)
        except (TypeError, ValueError):
            return None
        return generation if generation >= 0 else None

    @classmethod
    def _payload_with_render_generation(
        cls, job: Mapping[str, Any], generation: int
    ) -> str:
        """保留诊断字段并返回写入指定渲染代次的稳定 JSON。"""
        payload = cls._payload(job)
        payload["render_generation"] = int(generation)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def enqueue_in_transaction(
        connection: Any,
        job_type: str,
        payload: Mapping[str, Any],
        created_by_user_id: int | None,
        created_by_username: str,
        *,
        priority: int = 100,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        """在调用方事务内去重创建任务，渲染任务会提升为最新目标代次。"""
        if job_type not in MAINTENANCE_JOB_TYPES:
            raise ParameterError("不支持的维护任务类型")
        now = _utc_timestamp()
        existing = connection.execute(
            "SELECT * FROM admin_maintenance_jobs WHERE job_type=? "
            "AND status IN ('pending','running') ORDER BY id DESC LIMIT 1",
            (job_type,),
        ).fetchone()
        if existing is not None and job_type == "render_display":
            if existing["cancel_requested"]:
                connection.execute(
                    "UPDATE admin_maintenance_jobs SET status='canceled',progress=0,"
                    "lease_owner=NULL,lease_expires_at=NULL,error_code='render_superseded',"
                    "error_summary='已由更新的渲染请求替代',updated_at=?,finished_at=? "
                    "WHERE id=? AND status IN ('pending','running')",
                    (now, now, existing["id"]),
                )
                MaintenanceJobRepository._event(
                    connection,
                    int(existing["id"]),
                    "superseded",
                    str(existing["status"]),
                    "canceled",
                    reason_code="new_render_after_cancel",
                    created_at=now,
                )
                existing = None
            else:
                connection.execute(
                    "UPDATE admin_maintenance_jobs SET payload_json=?,priority=MAX(priority,?),"
                    "updated_at=? WHERE id=? AND status IN ('pending','running')",
                    (
                        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                        int(priority),
                        now,
                        existing["id"],
                    ),
                )
                result = dict(
                    connection.execute(
                        "SELECT * FROM admin_maintenance_jobs WHERE id=?", (existing["id"],)
                    ).fetchone()
                )
                result["duplicate"] = True
                return result
        elif existing is not None:
            result = dict(existing)
            result["duplicate"] = True
            return result

        cursor = connection.execute(
            "INSERT INTO admin_maintenance_jobs "
            "(job_type,status,payload_json,priority,progress,created_by_user_id,created_by_username,"
            "attempts,max_attempts,cancel_requested,created_at,updated_at) "
            "VALUES (?,'pending',?,?,0,?,?,0,?,0,?,?)",
            (
                job_type,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                int(priority),
                created_by_user_id,
                created_by_username[:128],
                max(1, min(int(max_attempts), 3)),
                now,
                now,
            ),
        )
        job_id = int(cursor.lastrowid)
        MaintenanceJobRepository._event(
            connection,
            job_id,
            "created",
            None,
            "pending",
            admin_user_id=created_by_user_id,
            reason_code="admin_enqueued",
            created_at=now,
        )
        result = dict(
            connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job_id,)
            ).fetchone()
        )
        result["duplicate"] = False
        return result

    def enqueue(
        self,
        job_type: str,
        payload: Mapping[str, Any],
        created_by_user_id: int | None,
        created_by_username: str,
        *,
        priority: int = 100,
    ) -> dict[str, Any]:
        """在独立短事务中创建或返回同类型活跃维护任务。"""
        with write_transaction(self.database_path) as connection:
            return self.enqueue_in_transaction(
                connection,
                job_type,
                payload,
                created_by_user_id,
                created_by_username,
                priority=priority,
                max_attempts=self.max_attempts,
            )

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """按编号倒序返回维护任务及解析后的结果。"""
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM admin_maintenance_jobs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        """原子认领最高优先级维护任务，首次认领同时固化配置快照。"""
        now_value = datetime.now(timezone.utc)
        now = _utc_timestamp(now_value)
        expires = _utc_timestamp(now_value + timedelta(seconds=max(1, lease_seconds)))
        with write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE status='pending' "
                "AND cancel_requested=0 AND attempts<max_attempts "
                "ORDER BY priority DESC,created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            if row["job_type"] == "render_display":
                state = connection.execute(
                    "SELECT desired_generation FROM display_artifact_state WHERE id=1"
                ).fetchone()
                if state is None:
                    raise RuntimeError("display_artifact_state_missing")
                desired_generation = int(state["desired_generation"])
                if self._render_generation(row) != desired_generation:
                    connection.execute(
                        "UPDATE admin_maintenance_jobs SET payload_json=?,updated_at=? "
                        "WHERE id=? AND status='pending' AND cancel_requested=0",
                        (
                            self._payload_with_render_generation(row, desired_generation),
                            now,
                            row["id"],
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM admin_maintenance_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
            first_claim = row["started_at"] is None
            scope = {
                "render_display": "render",
                "cleanup_expired_trash": "worker",
            }.get(str(row["job_type"]))
            if first_claim and self.snapshot_provider is not None and scope is not None:
                config_version, snapshot_json = self.snapshot_provider(scope, connection)
                cursor = connection.execute(
                    "UPDATE admin_maintenance_jobs SET status='running',progress=1,attempts=attempts+1,"
                    "lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=?,"
                    "config_version=?,config_snapshot_json=? WHERE id=? AND status='pending' "
                    "AND cancel_requested=0",
                    (
                        worker_id, expires, now, now, config_version, snapshot_json,
                        row["id"],
                    ),
                )
            else:
                cursor = connection.execute(
                    "UPDATE admin_maintenance_jobs SET status='running',progress=1,attempts=attempts+1,"
                    "lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=? "
                    "WHERE id=? AND status='pending' AND cancel_requested=0",
                    (worker_id, expires, now, now, row["id"]),
                )
            if cursor.rowcount != 1:
                return None
            self._event(
                connection,
                int(row["id"]),
                "claimed",
                "pending",
                "running",
                worker_id=worker_id,
                reason_code="worker_claim",
                created_at=now,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM admin_maintenance_jobs WHERE id=?", (row["id"],)
                ).fetchone()
            )

    def renew_lease(self, job_id: int, worker_id: str, lease_seconds: int) -> bool:
        """仅为当前持有者的未过期、未取消维护任务续租。"""
        now_value = datetime.now(timezone.utc)
        now = _utc_timestamp(now_value)
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE admin_maintenance_jobs SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? AND cancel_requested=0 "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at>?",
                (
                    _utc_timestamp(now_value + timedelta(seconds=max(1, lease_seconds))),
                    now,
                    job_id,
                    worker_id,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def recover_expired_leases(self) -> int:
        """恢复过期维护任务，取消请求优先闭合，其余任务按尝试次数恢复。"""
        now = _utc_timestamp()
        recovered = 0
        with write_transaction(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE status='running' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=? ORDER BY id",
                (now,),
            ).fetchall()
            for row in rows:
                canceled = bool(row["cancel_requested"])
                final = int(row["attempts"]) >= int(row["max_attempts"])
                if canceled:
                    status = "canceled"
                    error_code = "job_canceled"
                    error_summary = "任务已取消"
                    event_type = "canceled"
                    reason_code = "job_canceled"
                else:
                    status = "failed" if final else "pending"
                    error_code = "max_attempts_exceeded" if final else None
                    error_summary = "维护任务已达到最大尝试次数" if final else None
                    event_type = "lease_recovered"
                    reason_code = "max_attempts_exceeded" if final else "lease_expired"
                connection.execute(
                    "UPDATE admin_maintenance_jobs SET status=?,progress=0,lease_owner=NULL,"
                    "lease_expires_at=NULL,error_code=?,error_summary=?,updated_at=?,finished_at=? WHERE id=?",
                    (
                        status,
                        error_code,
                        error_summary,
                        now,
                        now if status in {"failed", "canceled"} else None,
                        row["id"],
                    ),
                )
                self._event(
                    connection,
                    int(row["id"]),
                    event_type,
                    "running",
                    status,
                    reason_code=reason_code,
                    created_at=now,
                )
                recovered += 1
        return recovered

    def is_interrupted(self, job_id: int, worker_id: str) -> bool:
        """判断维护任务是否被取消、失去所有权或租约已过期。"""
        now = _utc_timestamp()
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT status,lease_owner,cancel_requested,lease_expires_at "
                "FROM admin_maintenance_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return (
            row is None
            or row["status"] != "running"
            or row["lease_owner"] != worker_id
            or bool(row["cancel_requested"])
            or row["lease_expires_at"] is None
            or str(row["lease_expires_at"]) <= now
        )

    @staticmethod
    def _owns_unexpired_lease(current: Any, worker_id: str, now: str) -> bool:
        """校验任务仍由当前工作进程持有且租约未过期。"""
        return bool(
            current is not None
            and current["status"] == "running"
            and current["lease_owner"] == worker_id
            and not current["cancel_requested"]
            and current["lease_expires_at"] is not None
            and str(current["lease_expires_at"]) > now
        )

    def _complete_in_transaction(
        self,
        connection: Any,
        job: Mapping[str, Any],
        worker_id: str,
        result: Mapping[str, Any],
        now: str,
        *,
        manifest: Mapping[str, Any] | None = None,
        render_generation: int | None = None,
        next_cleanup_payload: Mapping[str, Any] | None = None,
    ) -> None:
        """在已验证租约和渲染代次的写事务内完成任务及关联状态。"""
        connection.execute(
            "UPDATE admin_maintenance_jobs SET status='succeeded',progress=100,result_json=?,"
            "lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,error_summary=NULL,"
            "updated_at=?,finished_at=? WHERE id=?",
            (
                json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                now,
                now,
                job["id"],
            ),
        )
        if manifest is not None:
            if render_generation is None:
                raise RuntimeError("render_generation_missing")
            cursor = connection.execute(
                "UPDATE display_artifact_state SET blocked=0,generation=?,"
                "manifest_json=?,updated_at=?,maintenance_job_id=? "
                "WHERE id=1 AND desired_generation=?",
                (
                    render_generation,
                    json.dumps(dict(manifest), ensure_ascii=False, sort_keys=True),
                    now,
                    job["id"],
                    render_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("render_generation_changed_inside_transaction")
        self._event(
            connection,
            int(job["id"]),
            "succeeded",
            "running",
            "succeeded",
            worker_id=worker_id,
            reason_code="completed",
            created_at=now,
        )
        if next_cleanup_payload is not None:
            self.enqueue_in_transaction(
                connection,
                "cleanup_expired_trash",
                next_cleanup_payload,
                job.get("created_by_user_id"),
                str(job.get("created_by_username") or "system_cleanup"),
                priority=int(job.get("priority") or 50),
                max_attempts=self.max_attempts,
            )

    def complete(
        self,
        job: Mapping[str, Any],
        worker_id: str,
        result: Mapping[str, Any],
        *,
        manifest: Mapping[str, Any] | None = None,
        next_cleanup_payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """仅由未过期租约持有者终结维护任务及关联数据库状态。"""
        now = _utc_timestamp()
        with write_transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if not self._owns_unexpired_lease(current, worker_id, now):
                return False
            self._complete_in_transaction(
                connection,
                job,
                worker_id,
                result,
                now,
                manifest=manifest,
                next_cleanup_payload=next_cleanup_payload,
            )
            return True

    def publish_and_complete(
        self,
        job: Mapping[str, Any],
        worker_id: str,
        result: Mapping[str, Any],
        manifest: Mapping[str, Any],
        publisher: Callable[[], None],
    ) -> str:
        """通过租约与期望代次双栅栏发布文件，过期业务代次退回等待。"""
        now = _utc_timestamp()
        with write_transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if not self._owns_unexpired_lease(current, worker_id, now):
                return "ownership_lost"
            state = connection.execute(
                "SELECT desired_generation FROM display_artifact_state WHERE id=1"
            ).fetchone()
            if state is None:
                raise RuntimeError("display_artifact_state_missing")
            desired_generation = int(state["desired_generation"])
            render_generation = self._render_generation(job)
            if render_generation != desired_generation:
                cursor = connection.execute(
                    "UPDATE admin_maintenance_jobs SET status='pending',progress=0,"
                    "attempts=MAX(0,attempts-1),payload_json=?,lease_owner=NULL,"
                    "lease_expires_at=NULL,error_code=NULL,error_summary=NULL,updated_at=? "
                    "WHERE id=? AND status='running' AND lease_owner=?",
                    (
                        self._payload_with_render_generation(current, desired_generation),
                        now,
                        job["id"],
                        worker_id,
                    ),
                )
                if cursor.rowcount != 1:
                    return "ownership_lost"
                connection.execute(
                    "UPDATE display_artifact_state SET blocked=1,updated_at=?,"
                    "maintenance_job_id=? WHERE id=1",
                    (now, job["id"]),
                )
                self._event(
                    connection,
                    int(job["id"]),
                    "superseded",
                    "running",
                    "pending",
                    worker_id=worker_id,
                    reason_code="render_generation_superseded",
                    created_at=now,
                )
                return "superseded"
            publisher()
            self._complete_in_transaction(
                connection,
                job,
                worker_id,
                result,
                now,
                manifest=manifest,
                render_generation=render_generation,
            )
            return "published"

    def fail(self, job: Mapping[str, Any], worker_id: str, error_code: str) -> str:
        """记录稳定错误码并自动重试，渲染失败不会解除产物屏蔽。"""
        now = _utc_timestamp()
        stable_code = str(error_code)[:100]
        with write_transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if current is None or current["status"] != "running" or current["lease_owner"] != worker_id:
                return str(job.get("status") or "failed")
            if current["cancel_requested"]:
                status = "canceled"
                stable_code = "job_canceled"
                error_summary = "任务已取消"
                event_type = "canceled"
            else:
                status = "failed" if int(current["attempts"]) >= int(current["max_attempts"]) else "pending"
                error_summary = "维护任务处理失败" if status == "failed" else "等待自动重试"
                event_type = "failed" if status == "failed" else "automatic_retry"
            connection.execute(
                "UPDATE admin_maintenance_jobs SET status=?,progress=0,error_code=?,error_summary=?,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=?,finished_at=? WHERE id=?",
                (
                    status,
                    stable_code,
                    error_summary,
                    now,
                    now if status in {"failed", "canceled"} else None,
                    job["id"],
                ),
            )
            self._event(
                connection,
                int(job["id"]),
                event_type,
                "running",
                status,
                worker_id=worker_id,
                reason_code=stable_code,
                created_at=now,
            )
            return status

    def cancel(self, job_id: int, admin_user_id: int) -> str:
        """取消等待任务或请求运行任务协作取消。"""
        now = _utc_timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("维护任务不存在")
            if row["status"] == "pending":
                connection.execute(
                    "UPDATE admin_maintenance_jobs SET status='canceled',error_code='job_canceled',"
                    "error_summary='任务已取消',updated_at=?,finished_at=? WHERE id=?",
                    (now, now, job_id),
                )
                self._event(
                    connection,
                    job_id,
                    "canceled",
                    "pending",
                    "canceled",
                    admin_user_id=admin_user_id,
                    reason_code="admin_canceled",
                    created_at=now,
                )
                return "canceled"
            if row["status"] == "running":
                connection.execute(
                    "UPDATE admin_maintenance_jobs SET cancel_requested=1,updated_at=? WHERE id=?",
                    (now, job_id),
                )
                self._event(
                    connection,
                    job_id,
                    "cancel_requested",
                    "running",
                    "running",
                    admin_user_id=admin_user_id,
                    reason_code="admin_requested",
                    created_at=now,
                )
                return "cancel_requested"
            raise ConflictError("维护任务当前状态不能取消")

    def retry(self, job_id: int, admin_user_id: int) -> dict[str, Any]:
        """把未耗尽尝试次数的失败或取消维护任务重新排队。"""
        now = _utc_timestamp()
        with write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError("维护任务不存在")
            if row["status"] not in {"failed", "canceled"} or int(row["attempts"]) >= int(row["max_attempts"]):
                raise ConflictError("维护任务不能重试")
            duplicate = connection.execute(
                "SELECT id FROM admin_maintenance_jobs WHERE job_type=? "
                "AND status IN ('pending','running') AND id<>?",
                (row["job_type"], job_id),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError("同类型维护任务已在运行")
            connection.execute(
                "UPDATE admin_maintenance_jobs SET status='pending',progress=0,cancel_requested=0,"
                "error_code=NULL,error_summary=NULL,lease_owner=NULL,lease_expires_at=NULL,"
                "updated_at=?,finished_at=NULL WHERE id=?",
                (now, job_id),
            )
            if row["job_type"] == "render_display":
                connection.execute(
                    "UPDATE display_artifact_state SET blocked=1,updated_at=?,maintenance_job_id=? WHERE id=1",
                    (now, job_id),
                )
            self._event(
                connection,
                job_id,
                "retried",
                str(row["status"]),
                "pending",
                admin_user_id=admin_user_id,
                reason_code="admin_retry",
                created_at=now,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM admin_maintenance_jobs WHERE id=?", (job_id,)
                ).fetchone()
            )


class PhotoLifecycleService:
    """实现安全软删除、恢复、永久删除和过期回收站清理。"""

    def __init__(
        self,
        database_path: Path,
        image_directory: Path,
        maintenance_jobs: MaintenanceJobRepository,
        invalidate_date_cache: Callable[[], None],
        retention_days: int = 30,
        failure_injector: Callable[[str], None] | None = None,
        configuration_service: Any | None = None,
    ) -> None:
        """保存生命周期边界并允许临时验证注入明确失败点。

        Args:
            database_path: 照片记录所在的 SQLite 文件。
            image_directory: 受管照片根目录，可用分号分隔配置多个，第一个为主目录。
            maintenance_jobs: 维护任务仓储。
            invalidate_date_cache: 失效日期缓存的回调。
            retention_days: 未注入配置服务时的回收站保留天数。
            failure_injector: 可选验证失败注入器。
            configuration_service: 可选统一配置服务；注入后 `retention_days` 按当前
                生效的 `TRASH_RETENTION_DAYS` 动态读取，照片根目录按当前生效的
                `IMAGE_DIR` 动态解析。
        """
        self.database_path = Path(database_path).expanduser().resolve()
        self._fallback_image_dirs = parse_image_dirs(
            image_directory, base_dir=PROJECT_ROOT
        )
        self.maintenance_jobs = maintenance_jobs
        self.invalidate_date_cache = invalidate_date_cache
        self._fallback_retention_days = int(retention_days)
        self.configuration_service = configuration_service
        self.failure_injector = failure_injector or (lambda _point: None)
        self.operation_owner = uuid.uuid4().hex
        self.operation_lease_seconds = 120

    @property
    def image_dirs(self) -> tuple[Path, ...]:
        """按当前生效配置返回全部照片根目录，第一个是主目录。

        配置写入路径已强校验目录存在、可读且互不嵌套；此处对损坏配置只记录错误
        并回退到构造时的根目录，避免运行期因一次异常写入导致所有照片不可访问。
        """
        raw = current_setting(self.configuration_service, "IMAGE_DIR", None)
        if raw is None or not str(raw).strip():
            return self._fallback_image_dirs
        try:
            return parse_image_dirs(raw, base_dir=PROJECT_ROOT)
        except ValueError as error:
            LOGGER.error(
                "Invalid IMAGE_DIR configuration, falling back to startup value, error=[%s]",
                error,
            )
            return self._fallback_image_dirs

    @property
    def image_directory(self) -> Path:
        """返回主照片目录，即上传与锁文件的写入位置。"""
        return self.image_dirs[0]

    @property
    def trash_directory(self) -> Path:
        """返回主照片目录自己的回收站目录。"""
        return self.trash_root(self.image_directory)

    @property
    def retention_days(self) -> int:
        """按当前生效配置返回回收站默认保留天数。"""
        return bounded_int(
            current_setting(
                self.configuration_service,
                "TRASH_RETENTION_DAYS",
                self._fallback_retention_days,
            ),
            1,
            3650,
            self._fallback_retention_days,
        )

    def image_directory_status(self) -> list[dict[str, Any]]:
        """汇总每个已配置照片目录的可用性与照片数量，供后台配置页展示。

        只做只读探测：目录是否存在、是否可读、是否可写，以及该目录下未删除与已在
        回收站的照片记录数。主目录额外标记，因为上传与锁文件只写主目录。

        Returns:
            与配置顺序一致的目录状态字典列表。
        """
        directories = self.image_dirs
        with database_connection(self.database_path, read_only=True) as connection:
            items: list[dict[str, Any]] = []
            for index, directory in enumerate(directories):
                prefix = like_prefix(directory)
                active = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM photo_scores "
                        "WHERE is_deleted=0 AND path LIKE ? ESCAPE '\\'",
                        (prefix,),
                    ).fetchone()[0]
                )
                trashed = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM photo_scores "
                        "WHERE is_deleted=1 AND path LIKE ? ESCAPE '\\'",
                        (prefix,),
                    ).fetchone()[0]
                )
                exists = directory.is_dir()
                items.append(
                    {
                        "path": str(directory),
                        "primary": index == 0,
                        "exists": exists,
                        "readable": exists and os.access(directory, os.R_OK | os.X_OK),
                        "writable": exists and os.access(directory, os.W_OK | os.X_OK),
                        "active_photos": active,
                        "trashed_photos": trashed,
                    }
                )
        return items

    def _assert_no_symlink_components(self, path: Path, root: Path) -> None:
        """拒绝所属照片根目录以下任何已存在的符号链接路径组件。"""
        current = root
        for component in path.relative_to(root).parts:
            current = current / component
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ParameterError("照片路径无法安全检查") from error
            if stat.S_ISLNK(mode):
                raise ParameterError("照片路径不能包含符号链接")

    def _owning_root(self, path: Path) -> Path:
        """返回包含给定绝对路径的照片根目录。

        配置解析阶段已拒绝互相嵌套的根目录，因此归属唯一：不存在一个路径同时
        属于两个根、进而绕过回收站边界检查的情况。

        Args:
            path: 已规范化的绝对路径。

        Returns:
            包含该路径的照片根目录。

        Raises:
            ParameterError: 路径不在任何已配置的照片目录内。
        """
        for root in self.image_dirs:
            if path.is_relative_to(root):
                return root
        raise ParameterError("照片路径超出允许范围")

    def trash_root(self, root: Path) -> Path:
        """返回指定照片根目录自己的回收站目录。

        每个根各自建一个 `.trash`，而不是集中到一处：软删除用硬链接加删除实现
        原子移动，硬链接不能跨文件系统，若照片在网络存储、回收站在本地磁盘会直接
        抛 `Invalid cross-device link`。按根隔离同时保证回收站文件不跨根泄露。
        """
        return root / TRASH_DIRECTORY_NAME

    def _managed_path(self, raw_path: str, *, trash: bool | None = None) -> Path:
        """按词法解析受管路径并拒绝所属根目录以下的符号链接。"""
        raw_text = str(raw_path or "").strip()
        if not raw_text:
            raise ParameterError("照片路径不能为空")
        path = Path(raw_text).expanduser()
        if not path.is_absolute():
            path = self.image_directory / path
        normalized = Path(os.path.abspath(path))
        root = self._owning_root(normalized)
        inside_trash = normalized.is_relative_to(self.trash_root(root))
        if trash is True and not inside_trash:
            raise ParameterError("回收站路径无效")
        if trash is False and inside_trash:
            raise ParameterError("活动照片路径不能位于回收站")
        self._assert_no_symlink_components(normalized, root)
        return normalized

    @contextmanager
    def _managed_parent(
        self, path: Path, *, create_parents: bool = False
    ) -> Iterator[tuple[int, str]]:
        """从所属照片根目录文件描述符逐级打开无符号链接的目标父目录。"""
        normalized = self._managed_path(str(path))
        root = self._owning_root(normalized)
        relative = normalized.relative_to(root)
        if not relative.parts:
            raise ParameterError("照片路径不能是图片根目录")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptors: list[int] = []
        try:
            current_descriptor = os.open(root, directory_flags)
            descriptors.append(current_descriptor)
            for component in relative.parts[:-1]:
                if create_parents:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                    except FileExistsError:
                        pass
                current_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=current_descriptor,
                )
                descriptors.append(current_descriptor)
        except FileNotFoundError as error:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise _ManagedPathMissingError("managed_path_parent_missing") from error
        except OSError as error:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise ServerError("照片路径无法安全访问") from error
        try:
            yield current_descriptor, relative.name
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _managed_file_identity(self, path: Path) -> tuple[int, int] | None:
        """不跟随符号链接读取受管普通文件身份；文件不存在时返回空。"""
        try:
            with self._managed_parent(path) as (parent_descriptor, name):
                try:
                    status = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
        except _ManagedPathMissingError:
            return None
        except OSError as error:
            raise ServerError("照片文件无法安全检查") from error
        if not stat.S_ISREG(status.st_mode):
            raise ServerError("照片目标不是普通文件")
        return status.st_dev, status.st_ino

    def _move_without_overwrite(self, source: Path, destination: Path) -> None:
        """用锚定父目录的硬链接加删除移动普通文件且不覆盖目标。"""
        try:
            with self._managed_parent(source) as (source_parent, source_name):
                with self._managed_parent(
                    destination, create_parents=True
                ) as (destination_parent, destination_name):
                    try:
                        source_status = os.stat(
                            source_name,
                            dir_fd=source_parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError as error:
                        raise ServerError("照片文件不存在") from error
                    if not stat.S_ISREG(source_status.st_mode):
                        raise ServerError("照片来源不是普通文件")
                    source_identity = source_status.st_dev, source_status.st_ino
                    try:
                        os.link(
                            source_name,
                            destination_name,
                            src_dir_fd=source_parent,
                            dst_dir_fd=destination_parent,
                            follow_symlinks=False,
                        )
                    except FileExistsError as error:
                        raise ConflictError("目标位置已有文件，操作已取消") from error
                    except OSError as error:
                        raise ServerError("文件无法安全移动") from error

                    def rollback_destination() -> None:
                        """仅当目标仍是本次创建的文件身份时删除硬链接。"""
                        try:
                            current = os.stat(
                                destination_name,
                                dir_fd=destination_parent,
                                follow_symlinks=False,
                            )
                            if (current.st_dev, current.st_ino) == source_identity:
                                os.unlink(destination_name, dir_fd=destination_parent)
                        except (FileNotFoundError, OSError):
                            return

                    try:
                        destination_status = os.stat(
                            destination_name,
                            dir_fd=destination_parent,
                            follow_symlinks=False,
                        )
                        current_source_status = os.stat(
                            source_name,
                            dir_fd=source_parent,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(destination_status.st_mode)
                            or not stat.S_ISREG(current_source_status.st_mode)
                            or (destination_status.st_dev, destination_status.st_ino)
                            != source_identity
                            or (current_source_status.st_dev, current_source_status.st_ino)
                            != source_identity
                        ):
                            raise ServerError("照片文件身份在移动期间发生变化")
                        os.unlink(source_name, dir_fd=source_parent)
                    except Exception:
                        rollback_destination()
                        raise
        except _ManagedPathMissingError as error:
            raise ServerError("文件无法安全移动") from error

    def _unlink_managed_file(
        self,
        path: Path,
        *,
        missing_ok: bool = False,
        expected_identity: tuple[int, int] | None = None,
    ) -> bool:
        """不跟随符号链接删除受管普通文件，并可校验预期文件身份。"""
        try:
            with self._managed_parent(path) as (parent_descriptor, name):
                try:
                    status = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if missing_ok:
                        return False
                    raise ServerError("照片文件不存在")
                identity = status.st_dev, status.st_ino
                if not stat.S_ISREG(status.st_mode):
                    raise ServerError("照片目标不是普通文件")
                if expected_identity is not None and identity != expected_identity:
                    raise ServerError("照片文件身份在删除前发生变化")
                os.unlink(name, dir_fd=parent_descriptor)
                return True
        except _ManagedPathMissingError:
            if missing_ok:
                return False
            raise ServerError("照片文件不存在")
        except OSError as error:
            raise ServerError("照片文件无法安全删除") from error

    @contextmanager
    def _photo_file_lock(self, photo_id: int) -> Iterator[None]:
        """用按照片编号稳定命名的跨进程文件锁串行化所有文件副作用。"""
        lock_path = self._managed_path(
            str(self.trash_directory / ".locks" / f"{int(photo_id)}.lock"),
            trash=True,
        )
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            with self._managed_parent(
                lock_path, create_parents=True
            ) as (parent_descriptor, name):
                descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        except (OSError, _ManagedPathMissingError) as error:
            raise ServerError("照片文件锁无法创建") from error
        with os.fdopen(descriptor, "a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _assert_owned_operation(
        self,
        operation_id: str,
        action: str,
        photo_id: int,
        owner: str,
        *,
        require_unexpired: bool,
    ) -> None:
        """在取得照片文件锁后确认生命周期操作仍属于指定所有者。"""
        with database_connection(self.database_path, read_only=True) as connection:
            operation = connection.execute(
                "SELECT action,photo_id,lease_owner,lease_expires_at "
                "FROM photo_lifecycle_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if (
            operation is None
            or str(operation["action"]) != action
            or int(operation["photo_id"]) != photo_id
            or str(operation["lease_owner"]) != owner
            or (
                require_unexpired
                and str(operation["lease_expires_at"]) <= _utc_timestamp()
            )
        ):
            raise RuntimeError("photo_lifecycle_operation_ownership_lost")

    def _move_owned_operation(
        self,
        operation_id: str,
        action: str,
        photo_id: int,
        source: Path,
        destination: Path,
    ) -> None:
        """持有共享照片锁并复核未过期所有权后执行不覆盖移动。"""
        with self._photo_file_lock(photo_id):
            self._assert_owned_operation(
                operation_id,
                action,
                photo_id,
                self.operation_owner,
                require_unexpired=True,
            )
            self._move_without_overwrite(source, destination)

    def _create_operation(
        self,
        action: str,
        photo_id: int,
        expected_version: int,
        source: Path,
        destination: Path,
        admin_user_id: int,
        admin_username: str,
    ) -> str:
        """在文件移动前以独立短事务创建唯一操作意图，并阻止恢复跨越删除请求。"""
        operation_id = uuid.uuid4().hex
        now_value = datetime.now(timezone.utc)
        now = _utc_timestamp(now_value)
        lease_expires_at = _utc_timestamp(
            now_value + timedelta(seconds=max(60, self.operation_lease_seconds))
        )
        with write_transaction(self.database_path) as connection:
            existing = connection.execute(
                "SELECT operation_id FROM photo_lifecycle_operations WHERE photo_id=? LIMIT 1",
                (photo_id,),
            ).fetchone()
            if existing is not None:
                raise ConflictError("照片存在尚未完成的生命周期操作，请稍后重试")
            if action == "restore":
                current = connection.execute(
                    "SELECT path,is_deleted,version,original_path,trash_path "
                    "FROM photo_scores WHERE id=?",
                    (photo_id,),
                ).fetchone()
                if (
                    current is None
                    or not bool(current["is_deleted"])
                    or int(current["version"]) != expected_version
                    or not self._same_managed_path(
                        current["path"], destination, trash=False
                    )
                    or not self._same_managed_path(
                        current["original_path"], destination, trash=False
                    )
                    or not self._same_managed_path(
                        current["trash_path"], source, trash=True
                    )
                ):
                    raise ConflictError("照片版本已变化，请刷新后重试")
                active_purge = connection.execute(
                    "SELECT operation_id FROM photo_purge_operations "
                    "WHERE photo_id=? LIMIT 1",
                    (photo_id,),
                ).fetchone()
                if active_purge is not None:
                    raise ConflictError("照片永久删除请求正在处理中，暂时不能恢复")
            connection.execute(
                "INSERT INTO photo_lifecycle_operations "
                "(operation_id,action,photo_id,expected_version,source_path,destination_path,"
                "admin_user_id,admin_username,lease_owner,lease_expires_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    action,
                    photo_id,
                    expected_version,
                    str(source),
                    str(destination),
                    admin_user_id,
                    admin_username[:128],
                    self.operation_owner,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
        return operation_id

    def _renew_operation(self, operation_id: str) -> bool:
        """仅在当前所有者租约尚未过期时续租操作意图。"""
        now_value = datetime.now(timezone.utc)
        now = _utc_timestamp(now_value)
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE photo_lifecycle_operations SET lease_expires_at=?,updated_at=? "
                "WHERE operation_id=? AND lease_owner=? AND lease_expires_at>?",
                (
                    _utc_timestamp(
                        now_value
                        + timedelta(seconds=max(60, self.operation_lease_seconds))
                    ),
                    now,
                    operation_id,
                    self.operation_owner,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def _same_managed_path(
        self,
        raw_path: Any,
        expected_path: Any,
        *,
        trash: bool,
    ) -> bool:
        """把数据库原始路径和台账绝对路径规范化到同一受管区域后比较。"""
        raw_text = str(raw_path or "").strip()
        expected_text = str(expected_path or "").strip()
        if not raw_text or not expected_text:
            return False
        try:
            return self._managed_path(raw_text, trash=trash) == self._managed_path(
                expected_text, trash=trash
            )
        except (OSError, RuntimeError, ParameterError):
            return False

    def _operation_state(
        self, connection: Any, operation: Mapping[str, Any]
    ) -> str | None:
        """按规范化受管路径及照片严格前态和后态判定文件目标位置。"""
        row = connection.execute(
            "SELECT id,path,is_deleted,deleted_at,original_path,trash_path,"
            "deleted_by_user_id,deleted_by_username,version FROM photo_scores WHERE id=?",
            (operation["photo_id"],),
        ).fetchone()
        if row is None:
            return None
        action = str(operation["action"])
        expected_version = int(operation["expected_version"])
        source = str(operation["source_path"])
        destination = str(operation["destination_path"])
        if action == "soft_delete":
            before = (
                not bool(row["is_deleted"])
                and int(row["version"]) == expected_version
                and self._same_managed_path(row["path"], source, trash=False)
                and row["deleted_at"] is None
                and row["original_path"] is None
                and row["trash_path"] is None
                and row["deleted_by_user_id"] is None
                and row["deleted_by_username"] is None
            )
            after = (
                bool(row["is_deleted"])
                and int(row["version"]) == expected_version + 1
                and self._same_managed_path(row["path"], source, trash=False)
                and row["deleted_at"] is not None
                and self._same_managed_path(
                    row["original_path"], source, trash=False
                )
                and self._same_managed_path(
                    row["trash_path"], destination, trash=True
                )
                and row["deleted_by_user_id"] == operation["admin_user_id"]
                and str(row["deleted_by_username"] or "")
                == str(operation["admin_username"])
            )
        elif action == "restore":
            before = (
                bool(row["is_deleted"])
                and int(row["version"]) == expected_version
                and self._same_managed_path(
                    row["path"], destination, trash=False
                )
                and row["deleted_at"] is not None
                and self._same_managed_path(
                    row["original_path"], destination, trash=False
                )
                and self._same_managed_path(row["trash_path"], source, trash=True)
            )
            after = (
                not bool(row["is_deleted"])
                and int(row["version"]) == expected_version + 1
                and self._same_managed_path(
                    row["path"], destination, trash=False
                )
                and row["deleted_at"] is None
                and row["original_path"] is None
                and row["trash_path"] is None
                and row["deleted_by_user_id"] is None
                and row["deleted_by_username"] is None
            )
        else:
            return None
        if before:
            return "before"
        if after:
            return "after"
        return None

    def _align_operation_paths(
        self, source: Path, destination: Path, *, prefer_destination: bool
    ) -> None:
        """按无符号链接文件身份对齐路径；双路径只删除同一文件的冗余链接。"""
        source_identity = self._managed_file_identity(source)
        destination_identity = self._managed_file_identity(destination)
        if source_identity is not None and destination_identity is not None:
            if source_identity != destination_identity:
                raise RuntimeError("photo_lifecycle_paths_conflict")
            redundant = source if prefer_destination else destination
            self._unlink_managed_file(
                redundant,
                expected_identity=source_identity,
            )
            return
        if source_identity is None and destination_identity is None:
            raise RuntimeError("photo_lifecycle_paths_missing")
        preferred_identity = (
            destination_identity if prefer_destination else source_identity
        )
        if preferred_identity is not None:
            return
        current = source if source_identity is not None else destination
        preferred = destination if prefer_destination else source
        self._move_without_overwrite(current, preferred)

    def _delete_owned_operation(self, operation_id: str, owner: str) -> bool:
        """仅删除仍由指定所有者持有的操作台账，避免覆盖并发恢复认领。"""
        with write_transaction(self.database_path) as connection:
            cursor = connection.execute(
                "DELETE FROM photo_lifecycle_operations WHERE operation_id=? AND lease_owner=?",
                (operation_id, owner),
            )
        return cursor.rowcount == 1

    def _compensate_operation(
        self,
        operation_id: str,
        action: str,
        photo_id: int,
        source: Path,
        destination: Path,
    ) -> None:
        """持有共享照片锁恢复文件前态，仍拥有台账时才删除操作。"""
        with self._photo_file_lock(photo_id):
            self._assert_owned_operation(
                operation_id,
                action,
                photo_id,
                self.operation_owner,
                require_unexpired=True,
            )
            self._align_operation_paths(
                source, destination, prefer_destination=False
            )
            if not self._delete_owned_operation(
                operation_id, self.operation_owner
            ):
                raise RuntimeError("photo_lifecycle_operation_ownership_lost")
        LOGGER.info(
            "Photo lifecycle operation compensated, operation_id=[%s], photo_id=[%s], action=[%s]",
            operation_id,
            photo_id,
            action,
        )

    def _claim_expired_operation(self, recovery_owner: str) -> dict[str, Any] | None:
        """在短写事务内原子认领一条最早过期操作，活动租约不被触碰。"""
        now_value = datetime.now(timezone.utc)
        now = _utc_timestamp(now_value)
        lease_expires_at = _utc_timestamp(
            now_value + timedelta(seconds=max(60, self.operation_lease_seconds))
        )
        with write_transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM photo_lifecycle_operations WHERE lease_expires_at<=? "
                "ORDER BY created_at,operation_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE photo_lifecycle_operations SET lease_owner=?,lease_expires_at=?,updated_at=? "
                "WHERE operation_id=? AND lease_expires_at<=?",
                (recovery_owner, lease_expires_at, now, row["operation_id"], now),
            )
            if cursor.rowcount != 1:
                return None
            operation = dict(row)
            operation["lease_owner"] = recovery_owner
            operation["lease_expires_at"] = lease_expires_at
            operation["updated_at"] = now
            return operation

    def recover_incomplete_operations(self) -> int:
        """逐条认领并对账至多一百个过期文件操作。

        每条操作在文件对齐前重新验证恢复所有权和有效租约，并以重新读取的当前台账行
        判定数据库严格前态或后态。未知状态、租约失效、路径缺失或冲突都会保留台账。

        Returns:
            本轮成功对齐并删除操作台账的数量。
        """
        recovery_owner = f"{self.operation_owner}-recovery-{uuid.uuid4().hex}"
        recovered = 0
        for _ in range(100):
            claimed = self._claim_expired_operation(recovery_owner)
            if claimed is None:
                break
            operation_id = str(claimed["operation_id"])
            photo_id = int(claimed["photo_id"])
            action = str(claimed["action"])
            try:
                with self._photo_file_lock(photo_id):
                    validation_now = _utc_timestamp()
                    with database_connection(
                        self.database_path, read_only=True
                    ) as connection:
                        current = connection.execute(
                            "SELECT * FROM photo_lifecycle_operations "
                            "WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        if (
                            current is None
                            or str(current["lease_owner"]) != recovery_owner
                            or str(current["lease_expires_at"])
                            <= validation_now
                        ):
                            raise RuntimeError(
                                "photo_lifecycle_operation_ownership_lost"
                            )
                        operation = dict(current)
                        state = self._operation_state(connection, operation)
                    if state is None:
                        raise RuntimeError(
                            "photo_lifecycle_database_state_ambiguous"
                        )
                    action = str(operation["action"])
                    if action == "soft_delete":
                        source = self._managed_path(
                            str(operation["source_path"]), trash=False
                        )
                        destination = self._managed_path(
                            str(operation["destination_path"]), trash=True
                        )
                    elif action == "restore":
                        source = self._managed_path(
                            str(operation["source_path"]), trash=True
                        )
                        destination = self._managed_path(
                            str(operation["destination_path"]), trash=False
                        )
                    else:
                        raise RuntimeError("photo_lifecycle_action_invalid")
                    self._align_operation_paths(
                        source,
                        destination,
                        prefer_destination=state == "after",
                    )
                    with write_transaction(self.database_path) as connection:
                        current = connection.execute(
                            "SELECT * FROM photo_lifecycle_operations "
                            "WHERE operation_id=? AND lease_owner=?",
                            (operation_id, recovery_owner),
                        ).fetchone()
                        if current is None:
                            raise RuntimeError(
                                "photo_lifecycle_operation_ownership_lost"
                            )
                        if self._operation_state(connection, current) != state:
                            raise RuntimeError(
                                "photo_lifecycle_database_state_changed"
                            )
                        cursor = connection.execute(
                            "DELETE FROM photo_lifecycle_operations "
                            "WHERE operation_id=? AND lease_owner=?",
                            (operation_id, recovery_owner),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError(
                                "photo_lifecycle_operation_delete_lost"
                            )
            except Exception as error:
                LOGGER.error(
                    "Photo lifecycle operation recovery failed, operation_id=[%s], "
                    "photo_id=[%s], action=[%s]",
                    operation_id,
                    photo_id,
                    action,
                    exc_info=error,
                )
                continue
            LOGGER.info(
                "Photo lifecycle operation recovered, operation_id=[%s], photo_id=[%s], action=[%s]",
                operation_id,
                photo_id,
                action,
            )
            recovered += 1
        return recovered

    @staticmethod
    def _lifecycle_audit(
        connection: Any,
        action: str,
        photo_id: int,
        path_snapshot: str | None,
        admin_user_id: int | None,
        admin_username: str,
        detail: Mapping[str, Any],
        created_at: str,
    ) -> None:
        """写入不引用 photo_scores 的不可逆生命周期审计快照。"""
        connection.execute(
            "INSERT INTO photo_lifecycle_audit "
            "(action,photo_id,path_snapshot,admin_user_id,admin_username,detail_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                action,
                photo_id,
                path_snapshot,
                admin_user_id,
                admin_username[:128],
                json.dumps(dict(detail), ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )

    def _block_and_enqueue_render(
        self,
        connection: Any,
        admin_user_id: int | None,
        admin_username: str,
        reason: str,
        photo_id: int,
        now: str,
    ) -> int:
        """推进期望代次并让唯一活跃渲染任务承接最新目标。"""
        cursor = connection.execute(
            "UPDATE display_artifact_state SET blocked=1,"
            "desired_generation=desired_generation+1,updated_at=? WHERE id=1",
            (now,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("display_artifact_state_missing")
        state = connection.execute(
            "SELECT desired_generation FROM display_artifact_state WHERE id=1"
        ).fetchone()
        desired_generation = int(state["desired_generation"])
        job = self.maintenance_jobs.enqueue_in_transaction(
            connection,
            "render_display",
            {
                "reason": reason,
                "photo_id": photo_id,
                "render_generation": desired_generation,
            },
            admin_user_id,
            admin_username,
            priority=200,
            max_attempts=self.maintenance_jobs.max_attempts,
        )
        connection.execute(
            "UPDATE display_artifact_state SET maintenance_job_id=? WHERE id=1",
            (job["id"],),
        )
        return int(job["id"])

    @staticmethod
    def _cancel_photo_jobs(connection: Any, photo_id: int, admin_user_id: int, now: str) -> None:
        """在软删除事务内直接终结全部活跃照片任务并记录稳定事件。"""
        rows = connection.execute(
            "SELECT id,status FROM admin_jobs WHERE photo_id=? AND status IN ('pending','running')",
            (photo_id,),
        ).fetchall()
        for row in rows:
            cursor = connection.execute(
                "UPDATE admin_jobs SET status='canceled',progress=0,cancel_requested=1,"
                "lease_owner=NULL,lease_expires_at=NULL,error_code='photo_deleted',"
                "error_summary='照片已移入回收站',updated_at=?,finished_at=? "
                "WHERE id=? AND status=?",
                (now, now, row["id"], row["status"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("photo_job_cancel_lost_inside_transaction")
            connection.execute(
                "INSERT INTO admin_job_events "
                "(job_id,event_type,old_status,new_status,admin_user_id,reason_code,created_at) "
                "VALUES (?,'canceled',?,'canceled',?,'photo_deleted',?)",
                (row["id"], row["status"], admin_user_id, now),
            )

    @staticmethod
    def _trash_item(row: Any) -> dict[str, Any]:
        """为回收站条目补一个可读展示名。

        与后台列表同一套口径：优先原始上传名，扫描入库的照片回退到磁盘名。
        `original_path` 原样保留，需要确认文件真实位置时看它。
        """
        item = dict(row)
        original_name = str(item.get("original_filename") or "").strip()
        stored_name = Path(str(item.get("original_path") or item.get("path") or "")).name
        item["display_name"] = original_name or stored_name or "未命名照片"
        return item

    def list_trash(self, page: int, limit: int) -> dict[str, Any]:
        """按删除时间和稳定照片编号倒序返回回收站分页。"""
        normalized_page = _positive_integer(page, "page")
        normalized_limit = _positive_integer(limit, "limit")
        if normalized_limit > 100:
            raise ParameterError("limit 不能超过 100")
        offset = (normalized_page - 1) * normalized_limit
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT id,path,original_filename,original_path,trash_path,deleted_at,"
                "deleted_by_user_id,deleted_by_username,version FROM photo_scores "
                "WHERE is_deleted=1 ORDER BY deleted_at DESC,id DESC LIMIT ? OFFSET ?",
                (normalized_limit, offset),
            ).fetchall()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM photo_scores WHERE is_deleted=1"
                ).fetchone()[0]
            )
        return {
            "items": [self._trash_item(row) for row in rows],
            "total": total,
            "page": normalized_page,
            "limit": normalized_limit,
            "total_pages": max(1, (total + normalized_limit - 1) // normalized_limit),
        }

    def get_trash_photo(self, photo_id: Any) -> dict[str, Any]:
        """读取回收站照片确认页需要的删除快照。"""
        normalized_id = _positive_integer(photo_id, "photo_id")
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT id,path,original_path,trash_path,deleted_at,deleted_by_user_id,"
                "deleted_by_username,version FROM photo_scores WHERE id=? AND is_deleted=1",
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("回收站照片不存在")
        return dict(row)

    def soft_delete(
        self,
        photo_id: Any,
        expected_version: Any,
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """以持久化操作意图把照片移入回收站，并在普通异常时反向补偿。"""
        normalized_id = _positive_integer(photo_id, "photo_id")
        normalized_version = _positive_integer(expected_version, "expected_version")
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT id,path,is_deleted,version FROM photo_scores WHERE id=?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("照片不存在")
        if bool(row["is_deleted"]):
            raise ConflictError("照片已在回收站")
        if int(row["version"]) != normalized_version:
            raise ConflictError("照片版本已变化，请刷新后重试")
        source = self._managed_path(str(row["path"]), trash=False)
        if not source.is_file():
            raise ResourceNotFoundError("照片文件不存在")
        # 回收站必须与照片同根：硬链接不能跨文件系统，集中回收站会让非主目录
        # 的照片软删除直接失败。
        destination = self._managed_path(
            str(
                self.trash_root(self._owning_root(source))
                / str(normalized_id)
                / f"{uuid.uuid4().hex}-{source.name}"
            ),
            trash=True,
        )
        operation_id = self._create_operation(
            "soft_delete",
            normalized_id,
            normalized_version,
            source,
            destination,
            admin_user_id,
            admin_username,
        )
        try:
            self._move_owned_operation(
                operation_id,
                "soft_delete",
                normalized_id,
                source,
                destination,
            )
            self.failure_injector("soft_delete_after_move")
            if not self._renew_operation(operation_id):
                raise RuntimeError("photo_lifecycle_operation_ownership_lost")
            now = _utc_timestamp()
            with write_transaction(self.database_path) as connection:
                operation = connection.execute(
                    "SELECT operation_id FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=? AND action='soft_delete' AND photo_id=?",
                    (operation_id, self.operation_owner, normalized_id),
                ).fetchone()
                if operation is None:
                    raise RuntimeError("photo_lifecycle_operation_ownership_lost")
                current = connection.execute(
                    "SELECT id,path,is_deleted,version FROM photo_scores WHERE id=?",
                    (normalized_id,),
                ).fetchone()
                if current is None:
                    raise ResourceNotFoundError("照片不存在")
                if bool(current["is_deleted"]):
                    raise ConflictError("照片已在回收站")
                if int(current["version"]) != normalized_version or str(current["path"]) != str(row["path"]):
                    raise ConflictError("照片版本已变化，请刷新后重试")
                cursor = connection.execute(
                    "UPDATE photo_scores SET is_deleted=1,deleted_at=?,original_path=?,trash_path=?,"
                    "deleted_by_user_id=?,deleted_by_username=?,"
                    "analysis_status=CASE WHEN analysis_status IN ('pending','running') "
                    "THEN 'failed' ELSE analysis_status END,"
                    "analysis_error=CASE WHEN analysis_status IN ('pending','running') "
                    "THEN 'job_canceled' ELSE analysis_error END,"
                    "updated_at=?,version=version+1 WHERE id=? AND version=? AND is_deleted=0",
                    (
                        now,
                        str(source),
                        str(destination),
                        admin_user_id,
                        admin_username[:128],
                        now,
                        normalized_id,
                        normalized_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("照片版本已变化，请刷新后重试")
                self._cancel_photo_jobs(connection, normalized_id, admin_user_id, now)
                self._lifecycle_audit(
                    connection,
                    "soft_deleted",
                    normalized_id,
                    str(source),
                    admin_user_id,
                    admin_username,
                    {"trash_path": str(destination), "expected_version": normalized_version},
                    now,
                )
                job_id = self._block_and_enqueue_render(
                    connection,
                    admin_user_id,
                    admin_username,
                    "photo_soft_deleted",
                    normalized_id,
                    now,
                )
                self.failure_injector("soft_delete_before_commit")
                cursor = connection.execute(
                    "DELETE FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (operation_id, self.operation_owner),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("photo_lifecycle_operation_delete_lost")
        except Exception:
            with database_connection(self.database_path, read_only=True) as connection:
                pending = connection.execute(
                    "SELECT 1 FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (operation_id, self.operation_owner),
                ).fetchone()
            if pending is not None:
                try:
                    self._compensate_operation(
                        operation_id,
                        "soft_delete",
                        normalized_id,
                        source,
                        destination,
                    )
                except Exception as compensation_error:
                    LOGGER.error(
                        "Soft delete compensation failed, operation_id=[%s], "
                        "photo_id=[%s], action=[%s]",
                        operation_id,
                        normalized_id,
                        "soft_delete",
                        exc_info=compensation_error,
                    )
                    raise ServerError("照片移入回收站失败且文件补偿失败") from compensation_error
            raise
        self.invalidate_date_cache()
        return {
            "id": normalized_id,
            "version": normalized_version + 1,
            "deleted_at": now,
            "maintenance_job_id": job_id,
        }

    def batch_soft_delete(
        self,
        items: Any,
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """逐项复用安全软删除流程，把一批活动照片移入回收站。

        整批输入会在移动文件前完成数量、正整数和重复编号校验；单项不存在、版本冲突或
        文件错误只记录当前项失败，其他项目继续执行。每个成功项仍独立完成文件补偿边界、
        生命周期审计、任务取消和显示产物屏蔽。

        Args:
            items: 包含照片编号和预期版本的列表，数量限制为 1 至 100。
            admin_user_id: 当前管理员编号。
            admin_username: 当前管理员用户名快照。

        Returns:
            批次编号、逐项成功与失败结果及汇总数量。
        """
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ParameterError("批量项目数量必须在 1 到 100 之间")
        normalized_items: list[tuple[int, int]] = []
        seen_ids: set[int] = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise ParameterError("每个批量项目必须包含 id 和 version")
            photo_id = _positive_integer(item.get("id"), "id")
            version = _positive_integer(item.get("version"), "version")
            if photo_id in seen_ids:
                raise ParameterError("同一批次不能包含重复照片编号")
            seen_ids.add(photo_id)
            normalized_items.append((photo_id, version))

        batch_id = uuid.uuid4().hex
        succeeded: list[dict[str, int]] = []
        failed: list[dict[str, Any]] = []
        for photo_id, version in normalized_items:
            try:
                result = self.soft_delete(
                    photo_id,
                    version,
                    admin_user_id,
                    admin_username,
                )
            except ResourceNotFoundError as error:
                failed.append(
                    {
                        "id": photo_id,
                        "code": "not_found",
                        "message": error.public_message,
                    }
                )
            except ConflictError as error:
                failed.append(
                    {
                        "id": photo_id,
                        "code": "conflict",
                        "message": error.public_message,
                    }
                )
            except ServerError as error:
                LOGGER.error(
                    "Batch soft delete item failed, batch_id=[%s], photo_id=[%s]",
                    batch_id,
                    photo_id,
                    exc_info=error,
                )
                failed.append(
                    {
                        "id": photo_id,
                        "code": "server_error",
                        "message": error.public_message,
                    }
                )
            except Exception as error:
                LOGGER.error(
                    "Unexpected batch soft delete item failure, batch_id=[%s], photo_id=[%s]",
                    batch_id,
                    photo_id,
                    exc_info=error,
                )
                failed.append(
                    {
                        "id": photo_id,
                        "code": "server_error",
                        "message": "服务器内部错误",
                    }
                )
            else:
                succeeded.append(
                    {"id": photo_id, "version": int(result["version"])}
                )
        return {
            "batch_id": batch_id,
            "succeeded": succeeded,
            "failed": failed,
            "success_count": len(succeeded),
            "failure_count": len(failed),
        }

    def restore(
        self,
        photo_id: Any,
        expected_version: Any,
        admin_user_id: int,
        admin_username: str,
    ) -> dict[str, Any]:
        """以持久化操作意图恢复照片，并在普通异常时移回原回收站路径。"""
        normalized_id = _positive_integer(photo_id, "photo_id")
        normalized_version = _positive_integer(expected_version, "expected_version")
        row = self.get_trash_photo(normalized_id)
        if int(row["version"]) != normalized_version:
            raise ConflictError("照片版本已变化，请刷新后重试")
        source = self._managed_path(str(row["trash_path"] or ""), trash=True)
        destination = self._managed_path(str(row["original_path"] or ""), trash=False)
        if not source.is_file():
            raise ResourceNotFoundError("回收站文件不存在")
        if destination.exists():
            raise ConflictError("原位置已有文件，恢复不会覆盖目标")
        operation_id = self._create_operation(
            "restore",
            normalized_id,
            normalized_version,
            source,
            destination,
            admin_user_id,
            admin_username,
        )
        try:
            self._move_owned_operation(
                operation_id,
                "restore",
                normalized_id,
                source,
                destination,
            )
            self.failure_injector("restore_after_move")
            if not self._renew_operation(operation_id):
                raise RuntimeError("photo_lifecycle_operation_ownership_lost")
            now = _utc_timestamp()
            with write_transaction(self.database_path) as connection:
                operation = connection.execute(
                    "SELECT operation_id FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=? AND action='restore' AND photo_id=?",
                    (operation_id, self.operation_owner, normalized_id),
                ).fetchone()
                if operation is None:
                    raise RuntimeError("photo_lifecycle_operation_ownership_lost")
                current = connection.execute(
                    "SELECT is_deleted,version,trash_path FROM photo_scores WHERE id=?",
                    (normalized_id,),
                ).fetchone()
                if current is None:
                    raise ResourceNotFoundError("照片不存在")
                if not bool(current["is_deleted"]):
                    raise ConflictError("照片已经恢复")
                if (
                    int(current["version"]) != normalized_version
                    or not self._same_managed_path(
                        current["trash_path"], source, trash=True
                    )
                ):
                    raise ConflictError("照片版本已变化，请刷新后重试")
                cursor = connection.execute(
                    "UPDATE photo_scores SET path=?,is_deleted=0,deleted_at=NULL,original_path=NULL,"
                    "trash_path=NULL,deleted_by_user_id=NULL,deleted_by_username=NULL,updated_at=?,"
                    "version=version+1 WHERE id=? AND version=? AND is_deleted=1",
                    (str(destination), now, normalized_id, normalized_version),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("照片版本已变化，请刷新后重试")
                self._lifecycle_audit(
                    connection,
                    "restored",
                    normalized_id,
                    str(destination),
                    admin_user_id,
                    admin_username,
                    {"expected_version": normalized_version},
                    now,
                )
                job_id = self._block_and_enqueue_render(
                    connection,
                    admin_user_id,
                    admin_username,
                    "photo_restored",
                    normalized_id,
                    now,
                )
                self.failure_injector("restore_before_commit")
                cursor = connection.execute(
                    "DELETE FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (operation_id, self.operation_owner),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("photo_lifecycle_operation_delete_lost")
        except Exception:
            with database_connection(self.database_path, read_only=True) as connection:
                pending = connection.execute(
                    "SELECT 1 FROM photo_lifecycle_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (operation_id, self.operation_owner),
                ).fetchone()
            if pending is not None:
                try:
                    self._compensate_operation(
                        operation_id,
                        "restore",
                        normalized_id,
                        source,
                        destination,
                    )
                except Exception as compensation_error:
                    LOGGER.error(
                        "Restore compensation failed, operation_id=[%s], "
                        "photo_id=[%s], action=[%s]",
                        operation_id,
                        normalized_id,
                        "restore",
                        exc_info=compensation_error,
                    )
                    raise ServerError("照片恢复失败且文件补偿失败") from compensation_error
            raise
        self.invalidate_date_cache()
        return {
            "id": normalized_id,
            "version": normalized_version + 1,
            "maintenance_job_id": job_id,
        }

    def _record_purge_failure(
        self,
        operation_id: str,
        purge_owner: str,
        photo_id: int,
        expected_version: int,
        path_snapshot: str,
        admin_user_id: int | None,
        admin_username: str,
        error_code: str,
    ) -> bool:
        """仅由当前永久删除所有者原子写入失败审计并关闭在途操作。"""
        with write_transaction(self.database_path) as connection:
            operation = connection.execute(
                "SELECT operation_id FROM photo_purge_operations "
                "WHERE operation_id=? AND lease_owner=?",
                (operation_id, purge_owner),
            ).fetchone()
            if operation is None:
                return False
            current = connection.execute(
                "SELECT id,path FROM photo_scores WHERE id=?",
                (photo_id,),
            ).fetchone()
            if current is not None:
                self._lifecycle_audit(
                    connection,
                    "purge_failed",
                    photo_id,
                    str(current["path"] or path_snapshot),
                    admin_user_id,
                    admin_username,
                    {
                        "operation_id": operation_id,
                        "expected_version": expected_version,
                        "error_code": error_code,
                    },
                    _utc_timestamp(),
                )
            cursor = connection.execute(
                "DELETE FROM photo_purge_operations "
                "WHERE operation_id=? AND lease_owner=?",
                (operation_id, purge_owner),
            )
            return cursor.rowcount == 1

    def purge(
        self,
        photo_id: Any,
        expected_version: Any,
        admin_user_id: int | None,
        admin_username: str,
        confirmation: str | None = None,
        *,
        internal: bool = False,
    ) -> dict[str, Any]:
        """持有照片文件锁和唯一在途台账执行可接管的永久删除。"""
        normalized_id = _positive_integer(photo_id, "photo_id")
        normalized_version = _positive_integer(expected_version, "expected_version")
        if not internal and confirmation != f"永久删除 {normalized_id}":
            raise ParameterError(f"请输入“永久删除 {normalized_id}”确认")
        purge_owner = f"{self.operation_owner}-purge-{uuid.uuid4().hex}"
        with self._photo_file_lock(normalized_id):
            now_value = datetime.now(timezone.utc)
            now = _utc_timestamp(now_value)
            lease_expires_at = _utc_timestamp(
                now_value + timedelta(seconds=max(60, self.operation_lease_seconds))
            )
            with write_transaction(self.database_path) as connection:
                current = connection.execute(
                    "SELECT id,path,trash_path,is_deleted,version "
                    "FROM photo_scores WHERE id=?",
                    (normalized_id,),
                ).fetchone()
                if current is None:
                    completed = connection.execute(
                        "SELECT 1 FROM photo_lifecycle_audit "
                        "WHERE photo_id=? AND action='purge_completed' LIMIT 1",
                        (normalized_id,),
                    ).fetchone()
                    if completed is not None:
                        return {"id": normalized_id, "status": "already_completed"}
                    raise ResourceNotFoundError("照片不存在")
                if not bool(current["is_deleted"]):
                    raise ConflictError("只有回收站照片可以永久删除")
                if int(current["version"]) != normalized_version:
                    raise ConflictError("照片版本已变化，请刷新后重试")
                active_operation = connection.execute(
                    "SELECT operation_id FROM photo_lifecycle_operations "
                    "WHERE photo_id=? LIMIT 1",
                    (normalized_id,),
                ).fetchone()
                if active_operation is not None:
                    raise ConflictError(
                        "照片存在尚未完成的生命周期操作，暂时不能永久删除"
                    )
                row = dict(current)
                trash_path = self._managed_path(
                    str(row["trash_path"] or ""), trash=True
                )
                existing_purge = connection.execute(
                    "SELECT * FROM photo_purge_operations WHERE photo_id=?",
                    (normalized_id,),
                ).fetchone()
                if existing_purge is None:
                    purge_operation_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO photo_purge_operations "
                        "(operation_id,photo_id,expected_version,trash_path,"
                        "admin_user_id,admin_username,internal,lease_owner,"
                        "lease_expires_at,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            purge_operation_id,
                            normalized_id,
                            normalized_version,
                            str(trash_path),
                            admin_user_id,
                            admin_username[:128],
                            int(internal),
                            purge_owner,
                            lease_expires_at,
                            now,
                            now,
                        ),
                    )
                    self._lifecycle_audit(
                        connection,
                        "purge_requested",
                        normalized_id,
                        str(row["path"]),
                        admin_user_id,
                        admin_username,
                        {
                            "operation_id": purge_operation_id,
                            "expected_version": normalized_version,
                            "internal": internal,
                        },
                        now,
                    )
                else:
                    if (
                        int(existing_purge["expected_version"])
                        != normalized_version
                        or not self._same_managed_path(
                            existing_purge["trash_path"], trash_path, trash=True
                        )
                    ):
                        raise ConflictError("永久删除操作版本或路径已变化")
                    if str(existing_purge["lease_expires_at"]) > now:
                        raise ConflictError("照片永久删除请求正在处理中")
                    purge_operation_id = str(existing_purge["operation_id"])
                    cursor = connection.execute(
                        "UPDATE photo_purge_operations SET admin_user_id=?,"
                        "admin_username=?,internal=?,lease_owner=?,lease_expires_at=?,"
                        "updated_at=? WHERE operation_id=? AND lease_expires_at<=?",
                        (
                            admin_user_id,
                            admin_username[:128],
                            int(internal),
                            purge_owner,
                            lease_expires_at,
                            now,
                            purge_operation_id,
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictError("照片永久删除请求正在处理中")

            self.failure_injector("purge_before_unlink")
            try:
                file_was_missing = not self._unlink_managed_file(
                    trash_path,
                    missing_ok=True,
                )
            except ServerError as error:
                error_code = "purge_file_removal_failed"
                try:
                    self._record_purge_failure(
                        purge_operation_id,
                        purge_owner,
                        normalized_id,
                        normalized_version,
                        str(row["path"]),
                        admin_user_id,
                        admin_username,
                        error_code,
                    )
                except Exception as audit_error:
                    LOGGER.error(
                        "Permanent delete failure audit failed, photo_id=[%s]",
                        normalized_id,
                        exc_info=audit_error,
                    )
                if isinstance(error, OSError):
                    LOGGER.error(
                        "Permanent delete file removal failed, photo_id=[%s]",
                        normalized_id,
                        exc_info=error,
                    )
                    raise ServerError("永久删除文件失败") from error
                raise
            self.failure_injector("purge_after_unlink")
            completed_at = _utc_timestamp()
            with write_transaction(self.database_path) as connection:
                purge_operation = connection.execute(
                    "SELECT operation_id FROM photo_purge_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (purge_operation_id, purge_owner),
                ).fetchone()
                if purge_operation is None:
                    raise ConflictError("永久删除操作所有权已失效")
                active_operation = connection.execute(
                    "SELECT operation_id FROM photo_lifecycle_operations "
                    "WHERE photo_id=? LIMIT 1",
                    (normalized_id,),
                ).fetchone()
                if active_operation is not None:
                    raise ConflictError("照片恢复或删除操作已经开始，永久删除已停止")
                current = connection.execute(
                    "SELECT id,path,is_deleted,version FROM photo_scores WHERE id=?",
                    (normalized_id,),
                ).fetchone()
                if current is None:
                    connection.execute(
                        "DELETE FROM photo_purge_operations "
                        "WHERE operation_id=? AND lease_owner=?",
                        (purge_operation_id, purge_owner),
                    )
                    return {"id": normalized_id, "status": "already_completed"}
                if not bool(current["is_deleted"]):
                    raise ConflictError("照片已恢复，永久删除已停止")
                if int(current["version"]) != normalized_version:
                    raise ConflictError("照片版本已变化，数据库收尾未执行")
                self._lifecycle_audit(
                    connection,
                    "purge_completed",
                    normalized_id,
                    str(current["path"]),
                    admin_user_id,
                    admin_username,
                    {
                        "operation_id": purge_operation_id,
                        "file_missing": file_was_missing,
                        "expected_version": normalized_version,
                    },
                    completed_at,
                )
                connection.execute(
                    "DELETE FROM admin_job_events WHERE job_id IN "
                    "(SELECT id FROM admin_jobs WHERE photo_id=?)",
                    (normalized_id,),
                )
                connection.execute(
                    "DELETE FROM admin_jobs WHERE photo_id=?", (normalized_id,)
                )
                connection.execute(
                    "DELETE FROM photo_audit_log WHERE photo_id=?", (normalized_id,)
                )
                display_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='display_stats'"
                ).fetchone()
                if display_table is not None:
                    connection.execute(
                        "DELETE FROM display_stats WHERE photo_id=?", (normalized_id,)
                    )
                cursor = connection.execute(
                    "DELETE FROM photo_scores WHERE id=? AND version=? AND is_deleted=1",
                    (normalized_id, normalized_version),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("永久删除数据库收尾失败")
                cursor = connection.execute(
                    "DELETE FROM photo_purge_operations "
                    "WHERE operation_id=? AND lease_owner=?",
                    (purge_operation_id, purge_owner),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("photo_purge_operation_delete_lost")
                self.failure_injector("purge_before_commit")
        self.invalidate_date_cache()
        return {"id": normalized_id, "status": "purged"}

    def cleanup_preview(
        self,
        *,
        cutoff: str | None = None,
        limit: int = 100,
        after_id: int = 0,
    ) -> dict[str, Any]:
        """只读预览达到保留期限且编号大于游标的稳定批次。"""
        normalized_limit = max(1, min(int(limit), 1000))
        normalized_after_id = max(0, int(after_id))
        cutoff_value = cutoff or _utc_timestamp(
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        )
        try:
            datetime.fromisoformat(cutoff_value)
        except ValueError as error:
            raise ParameterError("cutoff 必须是 ISO 8601 时间") from error
        with database_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT id,deleted_at,original_path,deleted_by_username,version FROM photo_scores "
                "WHERE is_deleted=1 AND deleted_at<=? AND id>? ORDER BY id LIMIT ?",
                (cutoff_value, normalized_after_id, normalized_limit),
            ).fetchall()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM photo_scores WHERE is_deleted=1 AND deleted_at<=? AND id>?",
                    (cutoff_value, normalized_after_id),
                ).fetchone()[0]
            )
        return {
            "cutoff": cutoff_value,
            "after_id": normalized_after_id,
            "items": [dict(row) for row in rows],
            "total": total,
        }

    def enqueue_cleanup(
        self,
        admin_user_id: int,
        admin_username: str,
        *,
        cutoff: str | None = None,
        batch_size: int = 100,
    ) -> dict[str, Any]:
        """排队分批过期清理任务，截止条件固定为 deleted_at 小于等于协调世界时。"""
        preview = self.cleanup_preview(cutoff=cutoff, limit=1)
        normalized_batch = max(1, min(int(batch_size), 500))
        return self.maintenance_jobs.enqueue(
            "cleanup_expired_trash",
            {"cutoff": preview["cutoff"], "batch_size": normalized_batch, "after_id": 0},
            admin_user_id,
            admin_username,
            priority=50,
        )

    def cleanup_batch(
        self,
        cutoff: str,
        batch_size: int,
        job_id: int,
        after_id: int = 0,
        interrupted: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """按稳定编号逐项清理，并在每项开始前检查项目边界协作中断。

        检查通过即越过当前项安全边界，该项可能完成；之后观察到取消或失租时只保证
        不再开始后续项。安全游标仍只跨过已成功或跳过的项目，不引入覆盖整批的事务。

        Args:
            cutoff: 固定的回收站过期截止时间。
            batch_size: 本批最多读取的项目数。
            job_id: 当前维护任务编号，用于审计和日志。
            after_id: 上一批已安全处理的最后照片编号。
            interrupted: 可选中断回调，返回真时不再开始当前边界对应的项目。

        Returns:
            逐项结果、计数、安全游标、剩余数量和显式中断标记。
        """
        normalized_after_id = max(0, int(after_id))
        preview = self.cleanup_preview(
            cutoff=cutoff,
            limit=batch_size,
            after_id=normalized_after_id,
        )
        results: list[dict[str, Any]] = []
        next_after_id = normalized_after_id
        was_interrupted = False
        for item in preview["items"]:
            if interrupted is not None and interrupted():
                was_interrupted = True
                break
            photo_id = int(item["id"])
            result: dict[str, Any]
            try:
                outcome = self.purge(
                    photo_id,
                    int(item["version"]),
                    None,
                    "system_cleanup",
                    internal=True,
                )
                state = "skipped" if outcome["status"] == "already_completed" else "succeeded"
                result = {"photo_id": photo_id, "status": state}
            except ResourceNotFoundError as error:
                result = {
                    "photo_id": photo_id,
                    "status": "skipped",
                    "error_code": error.error_code,
                }
            except ConflictError as error:
                LOGGER.warning(
                    "Trash cleanup item conflicted, job_id=[%s], photo_id=[%s], error_code=[%s]",
                    job_id,
                    photo_id,
                    error.error_code,
                )
                result = {
                    "photo_id": photo_id,
                    "status": "failed",
                    "error_code": error.error_code,
                }
            except Exception as error:
                error_code = type(error).__name__[:100]
                LOGGER.error(
                    "Trash cleanup item failed, job_id=[%s], photo_id=[%s], error_code=[%s]",
                    job_id,
                    photo_id,
                    error_code,
                    exc_info=error,
                )
                result = {
                    "photo_id": photo_id,
                    "status": "failed",
                    "error_code": error_code,
                }
            try:
                with write_transaction(self.database_path) as connection:
                    self._lifecycle_audit(
                        connection,
                        "cleanup_item_result",
                        photo_id,
                        str(item.get("original_path") or "") or None,
                        None,
                        "system_cleanup",
                        {
                            "job_id": job_id,
                            "status": result["status"],
                            "error_code": result.get("error_code"),
                            "cutoff": cutoff,
                        },
                        _utc_timestamp(),
                    )
            except Exception as audit_error:
                LOGGER.error(
                    "Trash cleanup audit failed, job_id=[%s], photo_id=[%s]",
                    job_id,
                    photo_id,
                    exc_info=audit_error,
                )
                result = {
                    "photo_id": photo_id,
                    "status": "failed",
                    "error_code": "cleanup_audit_failed",
                }
            results.append(result)
            if result["status"] == "failed":
                break
            next_after_id = photo_id
        counts = {
            "succeeded": sum(item["status"] == "succeeded" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
        }
        remaining = self.cleanup_preview(
            cutoff=cutoff,
            limit=1,
            after_id=next_after_id,
        )["total"]
        return {
            "cutoff": cutoff,
            "after_id": normalized_after_id,
            "next_after_id": next_after_id,
            "items": results,
            "counts": counts,
            "remaining": remaining,
            "interrupted": was_interrupted,
        }


class DisplayArtifactGuard:
    """在删除触发的新产物安全发布前统一拒绝旧产物访问。"""

    def __init__(self, database_path: Path) -> None:
        """保存只读查询显示产物状态的数据库路径。"""
        self.database_path = Path(database_path).expanduser().resolve()

    def blocked(self) -> bool:
        """在显式屏蔽或已发布代次落后期望代次时拒绝产物访问。"""
        with database_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT blocked,generation,desired_generation "
                "FROM display_artifact_state WHERE id=1"
            ).fetchone()
        return (
            row is None
            or bool(row["blocked"])
            or int(row["generation"]) < int(row["desired_generation"])
        )


_QUEUE_LABELS = {"photo": "照片", "maintenance": "维护"}
_JOB_TYPE_LABELS = {
    "analyze_photo": "照片分析",
    "generate_narration": "重写旁白",
    "backfill_content_hash": "摘要回填",
    "render_display": "展示产物渲染",
    "cleanup_expired_trash": "回收站过期清理",
}
_RESULT_COUNT_LABELS = {
    "purged": "永久删除",
    "skipped": "跳过",
    "failed": "失败",
    "accepted": "接收",
    "duplicate": "重复",
    "created": "新建",
    "scanned": "扫描",
}


def _summarize_job_result(raw: Any) -> str:
    """把任务结果 JSON 翻译成中文摘要，无法识别时返回空串由界面回退展示原文。

    Args:
        raw: 数据库中的 result_json 原始字符串。

    Returns:
        可直接展示的中文描述；空串表示没有可识别字段。
    """
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""

    parts: list[str] = []
    if "artifact_count" in payload:
        try:
            parts.append(f"生成 {int(payload['artifact_count'])} 个展示产物")
        except (TypeError, ValueError):
            pass

    counts = payload.get("counts")
    if isinstance(counts, dict):
        details = []
        for key, value in counts.items():
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if not number:
                continue
            details.append(f"{_RESULT_COUNT_LABELS.get(key, key)} {number} 张")
        if details:
            parts.append("，".join(details))

    if payload.get("remaining"):
        parts.append("仍有剩余，将继续分批处理")
    return "；".join(parts)


class MaintenanceJobService:
    """合并展示照片任务与维护任务并提供管理操作。"""

    _PURGE_INTERVAL = 3600  # 最多每小时触发一次自动清理

    def __init__(
        self,
        database_path: Path,
        maintenance_repository: MaintenanceJobRepository,
        photo_job_service: Any,
    ) -> None:
        """保存两套独立任务来源，避免修改阶段五任务约束。"""
        self.database_path = Path(database_path).expanduser().resolve()
        self.maintenance_repository = maintenance_repository
        self.photo_job_service = photo_job_service
        self._last_purge_at: float = 0.0
        self._purge_lock = threading.Lock()

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """合并两套任务并按创建时间与编号倒序返回。

        每小时最多自动触发一次终态任务清理，保留最近 50 条。服务实例在
        `create_app` 时构造一次、被 Waitress 多线程共享，因此节流状态用
        非阻塞锁保护：抢不到锁的线程直接跳过清理，不排队等待。
        """
        now = time.time()
        if (
            now - self._last_purge_at > self._PURGE_INTERVAL
            and self._purge_lock.acquire(blocking=False)
        ):
            try:
                self._last_purge_at = now
                self.purge_completed(keep=50)
            except Exception:  # noqa: BLE001 - 清理是尽力而为，不能影响列表查询
                LOGGER.warning("Periodic job purge failed", exc_info=True)
            finally:
                self._purge_lock.release()

        with database_connection(self.database_path, read_only=True) as connection:
            photo_rows = connection.execute(
                "SELECT j.*,p.path AS photo_path FROM admin_jobs j "
                "LEFT JOIN photo_scores p ON p.id=j.photo_id ORDER BY j.id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        jobs = []
        for row in photo_rows:
            item = dict(row)
            item["queue"] = "photo"
            item["result_json"] = None
            jobs.append(item)
        for item in self.maintenance_repository.list_jobs(limit):
            item["queue"] = "maintenance"
            item["photo_id"] = None
            item["photo_path"] = None
            jobs.append(item)
        return [
            self._decorate(item)
            for item in sorted(
                jobs,
                key=lambda item: (str(item.get("created_at") or ""), int(item["id"])),
                reverse=True,
            )[: max(1, min(int(limit), 200))]
        ]

    @staticmethod
    def _decorate(item: dict[str, Any]) -> dict[str, Any]:
        """为任务补充界面直接可用的中文标签与结果摘要。"""
        queue = str(item.get("queue") or "")
        job_type = str(item.get("job_type") or "")
        item["queue_label"] = _QUEUE_LABELS.get(queue, queue)
        item["type_label"] = _JOB_TYPE_LABELS.get(job_type, job_type)
        item["result_summary"] = _summarize_job_result(item.get("result_json"))
        return item

    def cancel(self, queue: str, job_id: int, admin_user_id: int) -> str:
        """按明确队列取消照片任务或维护任务。"""
        if queue == "maintenance":
            return self.maintenance_repository.cancel(job_id, admin_user_id)
        if queue == "photo":
            return self.photo_job_service.cancel(job_id, admin_user_id)
        raise ParameterError("未知任务队列")

    def retry(self, queue: str, job_id: int, admin_user_id: int) -> dict[str, Any]:
        """按明确队列重试照片任务或维护任务。"""
        if queue == "maintenance":
            return self.maintenance_repository.retry(job_id, admin_user_id)
        if queue == "photo":
            return self.photo_job_service.retry(job_id, admin_user_id)
        raise ParameterError("未知任务队列")

    def purge_completed(self, keep: int = 50) -> dict[str, int]:
        """删除超出保留数量的终态任务记录及其事件日志。

        保留最近 keep 条终态任务（succeeded/failed/canceled），活跃任务
        （pending/running）始终保留不受影响。

        两张任务表各用一个独立事务，属于「尽力清理」语义：两表之间没有交叉
        外键，先成功后失败只会少清一张表，下次调用会补上，不产生不一致。
        事件表必须先删——外键没有 ON DELETE CASCADE，反序会触发约束违反。

        Args:
            keep: 每张任务表各保留的终态任务条数，最小 10。

        Returns:
            两张表各删除的任务数。
        """
        keep = max(10, int(keep))
        terminal = ("succeeded", "failed", "canceled")
        placeholders = ",".join("?" for _ in terminal)

        photo_purged = 0
        with write_transaction(self.database_path) as connection:
            cutoff_row = connection.execute(
                f"SELECT id FROM admin_jobs WHERE status IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1 OFFSET ?",
                (*terminal, keep),
            ).fetchone()
            if cutoff_row:
                cutoff_id = int(cutoff_row["id"])
                connection.execute(
                    f"DELETE FROM admin_job_events WHERE job_id IN "
                    f"(SELECT id FROM admin_jobs WHERE status IN ({placeholders}) AND id <= ?)",
                    (*terminal, cutoff_id),
                )
                cursor = connection.execute(
                    f"DELETE FROM admin_jobs WHERE status IN ({placeholders}) AND id <= ?",
                    (*terminal, cutoff_id),
                )
                photo_purged = cursor.rowcount

        maintenance_purged = 0
        with write_transaction(self.database_path) as connection:
            cutoff_row = connection.execute(
                f"SELECT id FROM admin_maintenance_jobs WHERE status IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1 OFFSET ?",
                (*terminal, keep),
            ).fetchone()
            if cutoff_row:
                cutoff_id = int(cutoff_row["id"])
                connection.execute(
                    f"DELETE FROM admin_maintenance_job_events WHERE job_id IN "
                    f"(SELECT id FROM admin_maintenance_jobs WHERE status IN ({placeholders}) AND id <= ?)",
                    (*terminal, cutoff_id),
                )
                cursor = connection.execute(
                    f"DELETE FROM admin_maintenance_jobs WHERE status IN ({placeholders}) AND id <= ?",
                    (*terminal, cutoff_id),
                )
                maintenance_purged = cursor.rowcount

        return {"photo_purged": photo_purged, "maintenance_purged": maintenance_purged}


class MaintenanceWorker:
    """显式分派渲染和过期清理维护任务，未知类型稳定失败。"""

    def __init__(
        self,
        repository: MaintenanceJobRepository,
        lifecycle: PhotoLifecycleService,
        database_path: Path,
        output_directory: Path,
        *,
        worker_id: str,
        lease_seconds: int = 120,
        renew_seconds: int = 30,
        failure_injector: Callable[[str], None] | None = None,
        configuration_service: Any | None = None,
    ) -> None:
        """保存维护任务依赖、发布边界及可选统一配置服务。

        Args:
            repository: 维护任务仓储。
            lifecycle: 照片生命周期服务。
            database_path: 渲染读取的明确数据库路径。
            output_directory: 同文件系统正式输出目录。
            worker_id: 当前工作进程标识。
            lease_seconds: 未注入配置服务时的任务租约秒数。
            renew_seconds: 未注入配置服务时的续租间隔秒数。
            failure_injector: 可选验证失败注入器。
            configuration_service: 可选统一配置服务；注入后租约与续租间隔按当前
                生效配置动态读取，为空时保持旧渲染行为。
        """
        self.repository = repository
        self.lifecycle = lifecycle
        self.database_path = Path(database_path).expanduser().resolve()
        self.output_directory = Path(output_directory).expanduser().resolve()
        self.worker_id = worker_id
        self._fallback_lease_seconds = int(lease_seconds)
        self._fallback_renew_seconds = int(renew_seconds)
        self.failure_injector = failure_injector or (lambda _point: None)
        self.configuration_service = configuration_service

    @property
    def lease_seconds(self) -> int:
        """按当前生效配置返回维护任务租约秒数。"""
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
        """按当前生效配置返回续租间隔，并强制小于当前租约时长。"""
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
                "Maintenance renew interval not shorter than lease, clamped, "
                "worker_id=[%s], renew_seconds=[%s], lease_seconds=[%s]",
                self.worker_id, renew, lease,
            )
            return max(1, lease - 1)
        return renew

    def run_once(self) -> bool:
        """先恢复过期文件操作和维护租约，再处理至多一个维护任务。"""
        recovered_operations = self.lifecycle.recover_incomplete_operations()
        if recovered_operations:
            LOGGER.info(
                "Photo lifecycle operations recovered by worker, recovered_count=[%s]",
                recovered_operations,
            )
        self.repository.recover_expired_leases()
        job = self.repository.claim_next(self.worker_id, self.lease_seconds)
        if job is None:
            return False
        self._execute(job)
        return True

    @staticmethod
    def _payload(job: Mapping[str, Any]) -> dict[str, Any]:
        """安全解析系统生成的维护任务载荷。"""
        try:
            payload = json.loads(str(job["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _render(
        self,
        job: Mapping[str, Any],
        settings: Mapping[str, Any] | None,
        interrupted: Callable[[], bool],
    ) -> tuple[dict[str, Any], dict[str, Any], Path, list[str]]:
        """用同一任务配置生成并验证两套临时产物，不触碰正式文件。"""
        self.output_directory.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".inktime-render-", dir=str(self.output_directory))
        ).resolve()
        try:
            small = importlib.import_module("src.render.render_daily_photo")
            large = importlib.import_module("src.render.render_daily_photo_133c")
            small_result = small.main(
                output_directory=temporary,
                database_path=self.database_path,
                settings=settings,
            )
            if interrupted():
                raise RuntimeError("maintenance_job_ownership_lost")
            large_result = large.main(
                output_directory=temporary,
                database_path=self.database_path,
                settings=settings,
            )
            if interrupted():
                raise RuntimeError("maintenance_job_ownership_lost")
            artifacts = list(small_result["artifacts"]) + list(large_result["artifacts"])
            if not artifacts:
                raise RuntimeError("render_produced_no_artifacts")
            for name in artifacts:
                source = (temporary / name).resolve()
                if not source.is_relative_to(temporary) or not source.is_file():
                    raise RuntimeError("render_artifact_missing")
            self.failure_injector("render_before_publish")
            manifest = {
                "small": small_result["manifest"],
                "large": large_result["manifest"],
                "artifacts": artifacts,
                "published_at": _utc_timestamp(),
            }
            return {"artifact_count": len(artifacts)}, manifest, temporary, artifacts
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _publish_render(self, temporary: Path, artifacts: list[str]) -> None:
        """在仓储持有发布栅栏时替换正式产物并清理过期受管文件。"""
        published_names = set(artifacts)
        for name in artifacts:
            source = (temporary / name).resolve()
            if not source.is_relative_to(temporary) or not source.is_file():
                raise RuntimeError("render_artifact_missing")
            os.replace(source, self.output_directory / name)

        import re

        managed_pattern = re.compile(
            r"^(?:photo_\d+\.(?:bin|h)|preview_\d+\.png|latest\.(?:bin|h)|preview\.png|"
            r"photo_13in3_6c_\d+_(?:L|R|FULL)\.bin|preview_13in3_6c_\d+\.png|"
            r"latest_13in3_6c_(?:L|R|FULL)\.bin|preview_13in3_6c\.png)$"
        )
        for existing in self.output_directory.iterdir():
            if (
                existing.is_file()
                and managed_pattern.fullmatch(existing.name)
                and existing.name not in published_names
            ):
                existing.unlink()

    def _resolve_settings(self, job: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """按维护任务类型解析固化配置；未注入服务时保持旧执行方式。"""
        if self.configuration_service is None:
            return None
        scope = {
            "render_display": "render",
            "cleanup_expired_trash": "worker",
        }.get(str(job["job_type"]))
        if scope is None:
            return None
        return self.configuration_service.resolve_task_snapshot(job, scope)

    def _execute(self, job: Mapping[str, Any]) -> None:
        """解析快照并维持租约，渲染正式发布必须通过事务所有权栅栏。"""
        job_id = int(job["id"])
        try:
            settings = self._resolve_settings(job)
        except ValueError as error:
            status = self.repository.fail(
                job, self.worker_id, "invalid_config_snapshot"
            )
            LOGGER.error(
                "Invalid maintenance job config snapshot, job_id=[%s], status=[%s]",
                job_id,
                status,
                exc_info=error,
            )
            return
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            """按固定间隔续租，并把续租失败传播给执行线程。"""
            while not heartbeat_stop.wait(self.renew_seconds):
                try:
                    renewed = self.repository.renew_lease(
                        job_id, self.worker_id, self.lease_seconds
                    )
                except Exception as error:
                    lease_lost.set()
                    LOGGER.error(
                        "Maintenance job lease renewal failed, job_id=[%s]",
                        job_id,
                        exc_info=error,
                    )
                    return
                if not renewed:
                    lease_lost.set()
                    LOGGER.warning(
                        "Maintenance job lease lost, job_id=[%s]",
                        job_id,
                    )
                    return

        def interrupted() -> bool:
            """同时检查本地续租失败事件与仓储中的取消、所有权和租约状态。"""
            if lease_lost.is_set():
                return True
            try:
                return self.repository.is_interrupted(job_id, self.worker_id)
            except Exception as error:
                lease_lost.set()
                LOGGER.error(
                    "Maintenance job interruption check failed, job_id=[%s]",
                    job_id,
                    exc_info=error,
                )
                return True

        thread = threading.Thread(
            target=heartbeat,
            name=f"inktime-maintenance-{job_id}-heartbeat",
            daemon=True,
        )
        thread.start()
        temporary: Path | None = None
        try:
            if job["job_type"] == "render_display":
                result, manifest, temporary, artifacts = self._render(
                    job,
                    settings,
                    interrupted,
                )
                if lease_lost.is_set():
                    raise RuntimeError("maintenance_job_ownership_lost")
                publish_status = self.repository.publish_and_complete(
                    job,
                    self.worker_id,
                    result,
                    manifest,
                    lambda: self._publish_render(temporary, artifacts),
                )
                if publish_status == "superseded":
                    LOGGER.info(
                        "Maintenance render superseded, job_id=[%s]",
                        job_id,
                    )
                elif publish_status != "published":
                    raise RuntimeError("maintenance_job_ownership_lost")
            elif job["job_type"] == "cleanup_expired_trash":
                payload = self._payload(job)
                cutoff = str(payload.get("cutoff") or "")
                batch_size = max(1, min(int(payload.get("batch_size", 100)), 500))
                after_id = max(0, int(payload.get("after_id", 0)))
                result = self.lifecycle.cleanup_batch(
                    cutoff,
                    batch_size,
                    job_id,
                    after_id,
                    interrupted=interrupted,
                )
                if result["interrupted"]:
                    self.repository.fail(
                        job,
                        self.worker_id,
                        "cleanup_interrupted",
                    )
                    return
                if result["counts"]["failed"]:
                    self.repository.fail(
                        job,
                        self.worker_id,
                        "cleanup_item_failed",
                    )
                    return
                next_payload = None
                if result["remaining"]:
                    next_payload = {
                        "cutoff": cutoff,
                        "batch_size": batch_size,
                        "after_id": result["next_after_id"],
                    }
                if not self.repository.complete(
                    job,
                    self.worker_id,
                    result,
                    next_cleanup_payload=next_payload,
                ):
                    raise RuntimeError("maintenance_job_ownership_lost")
            else:
                self.repository.fail(job, self.worker_id, "unsupported_maintenance_job_type")
        except Exception as error:
            error_code = type(error).__name__[:100]
            status = self.repository.fail(job, self.worker_id, error_code)
            LOGGER.error(
                "Maintenance job failed, job_id=[%s], status=[%s], error_code=[%s]",
                job_id,
                status,
                error_code,
                exc_info=error,
            )
        finally:
            heartbeat_stop.set()
            thread.join(timeout=max(1.0, self.renew_seconds + 1.0))
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)


class CombinedWorker:
    """在单一工作进程中显式轮询维护任务和阶段五照片任务。"""

    def __init__(
        self,
        maintenance_worker: MaintenanceWorker,
        photo_worker: Any,
        poll_seconds: float,
        configuration_service: Any | None = None,
    ) -> None:
        """保存两套工作器并让高优先级维护任务先获得处理机会。

        Args:
            maintenance_worker: 维护任务工作器。
            photo_worker: 照片任务工作器。
            poll_seconds: 未注入配置服务时的空闲等待秒数。
            configuration_service: 可选统一配置服务；注入后每轮循环按当前生效的
                `JOB_POLL_SECONDS` 等待，改完无需重启工作进程。
        """
        self.maintenance_worker = maintenance_worker
        self.photo_worker = photo_worker
        self._fallback_poll_seconds = float(poll_seconds)
        self.configuration_service = configuration_service
        self._stop = threading.Event()

    @property
    def poll_seconds(self) -> float:
        """按当前生效配置返回两队列均空时的等待秒数。"""
        return bounded_float(
            current_setting(
                self.configuration_service, "JOB_POLL_SECONDS", self._fallback_poll_seconds
            ),
            0.1,
            3600.0,
            self._fallback_poll_seconds,
        )

    def request_stop(self, *_args: Any) -> None:
        """请求两套工作器在安全边界停止。"""
        self._stop.set()
        self.photo_worker.request_stop()

    def run_forever(self) -> None:
        """持续显式分派两套队列，空闲时按配置等待。"""
        import signal

        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        while not self._stop.is_set():
            handled = self.maintenance_worker.run_once()
            if not handled:
                handled = self.photo_worker.run_once()
            if not handled:
                self._stop.wait(self.poll_seconds)
