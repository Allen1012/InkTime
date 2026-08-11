"""InkTime 独立照片分析工作进程入口。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from src.analysis.photo_analyzer import analyze_single_photo, generate_narration
from src.configuration import ConfigurationService
from src.migrations import assert_current_schema
from src.server.admin_jobs import AdminJobRepository, AnalysisWorker, JobRuntimeConfig
from src.server.photo_lifecycle import (
    CombinedWorker,
    MaintenanceJobRepository,
    MaintenanceWorker,
    PhotoLifecycleService,
)

ROOT_DIR = Path(__file__).resolve().parents[2]


def _absolute_path(value: str) -> Path:
    """按项目根目录解析统一配置中的文件系统路径。"""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT_DIR / path).resolve()


def main() -> None:
    """校验数据库结构后构造统一配置服务并启动组合后台工作进程。"""
    load_dotenv(ROOT_DIR / ".env", override=False)
    database_path = _absolute_path(os.environ.get("DB_PATH", "./data/photos.db"))
    assert_current_schema(database_path)

    configuration = ConfigurationService(database_path, environment=os.environ)
    runtime = JobRuntimeConfig.from_mapping(
        configuration.get_many(
            (
                "JOB_MAX_ATTEMPTS",
                "JOB_LEASE_SECONDS",
                "JOB_RENEW_SECONDS",
                "JOB_POLL_SECONDS",
            )
        )
    )
    paths_and_retention = configuration.get_many(
        ("IMAGE_DIR", "BIN_OUTPUT_DIR", "TRASH_RETENTION_DAYS")
    )
    image_directory = _absolute_path(str(paths_and_retention["IMAGE_DIR"]))
    output_directory = _absolute_path(str(paths_and_retention["BIN_OUTPUT_DIR"]))
    retention_days = int(paths_and_retention["TRASH_RETENTION_DAYS"])

    repository = AdminJobRepository(
        database_path,
        runtime.max_attempts,
        snapshot_provider=configuration.task_snapshot,
        configuration_service=configuration,
    )
    photo_worker = AnalysisWorker(
        repository,
        analyze_single_photo,
        generate_narration,
        configuration_service=configuration,
        lease_seconds=runtime.lease_seconds,
        renew_seconds=runtime.renew_seconds,
        poll_seconds=runtime.poll_seconds,
    )
    maintenance_repository = MaintenanceJobRepository(
        database_path,
        runtime.max_attempts,
        snapshot_provider=configuration.task_snapshot,
        configuration_service=configuration,
    )
    lifecycle = PhotoLifecycleService(
        database_path,
        image_directory,
        maintenance_repository,
        lambda: None,
        retention_days,
        configuration_service=configuration,
    )
    maintenance_worker = MaintenanceWorker(
        maintenance_repository,
        lifecycle,
        database_path,
        output_directory,
        worker_id=photo_worker.worker_id,
        configuration_service=configuration,
        lease_seconds=runtime.lease_seconds,
        renew_seconds=runtime.renew_seconds,
    )
    CombinedWorker(
        maintenance_worker,
        photo_worker,
        runtime.poll_seconds,
        configuration_service=configuration,
    ).run_forever()


if __name__ == "__main__":
    main()
