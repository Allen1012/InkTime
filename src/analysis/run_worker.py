"""InkTime 独立照片分析工作进程入口。"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

from dotenv import load_dotenv

from src.analysis.photo_analyzer import analyze_single_photo, generate_narration
from src.configuration import ConfigurationService, parse_image_dirs
from src.migrations import assert_current_schema
from src.server.admin_jobs import AdminJobRepository, AnalysisWorker, JobRuntimeConfig
from src.server.model_providers import ModelProviderService
from src.server.repositories.model_provider_repository import ModelProviderRepository
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


def build_provider_task_snapshot(
    configuration: ConfigurationService,
    model_providers: ModelProviderService,
    job_type: str,
    scope: str,
    connection: object,
) -> tuple[int, str]:
    """按任务用途在认领事务内固化公开厂商参数，密钥留到执行时现读。

    Args:
        configuration: 统一配置服务。
        model_providers: 模型厂商服务。
        job_type: 后台任务类型。
        scope: 任务配置作用域。
        connection: 当前任务认领 SQLite 事务连接。

    Returns:
        配置版本和稳定任务快照 JSON。
    """
    routes = configuration.get_many(
        ("ANALYSIS_PROVIDER", "NARRATION_PROVIDER"), connection=connection
    )
    provider: dict[str, list[dict[str, object]]] = {}
    route_by_purpose: dict[str, str] = {}
    if job_type == "analyze_photo":
        route_by_purpose["analysis"] = str(routes["ANALYSIS_PROVIDER"])
        route_by_purpose["narration"] = str(
            routes["NARRATION_PROVIDER"] or routes["ANALYSIS_PROVIDER"]
        )
    elif job_type == "generate_narration":
        route_by_purpose["narration"] = str(
            routes["NARRATION_PROVIDER"] or routes["ANALYSIS_PROVIDER"]
        )
    fields = (
        "id", "name", "version", "base_url", "model_name",
        "timeout_seconds", "max_long_edge",
    )
    for purpose, route in route_by_purpose.items():
        chain = model_providers.resolve_chain(route, connection=connection)
        if chain:
            provider[purpose] = [
                {key: candidate[key] for key in fields} for candidate in chain
            ]
    return configuration.task_snapshot(scope, connection, provider=provider)


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
    # 照片目录可配置多个，这里只做结构校验并原样下传，由服务按当前配置动态解析。
    raw_image_dirs = str(paths_and_retention["IMAGE_DIR"])
    try:
        parse_image_dirs(raw_image_dirs, base_dir=ROOT_DIR)
    except ValueError as error:
        raise SystemExit(f"IMAGE_DIR 配置无效: {error}") from error
    output_directory = _absolute_path(str(paths_and_retention["BIN_OUTPUT_DIR"]))
    retention_days = int(paths_and_retention["TRASH_RETENTION_DAYS"])

    model_providers = ModelProviderService(
        ModelProviderRepository(database_path), configuration_service=configuration
    )

    repository = AdminJobRepository(
        database_path,
        runtime.max_attempts,
        configuration_service=configuration,
        job_snapshot_provider=partial(
            build_provider_task_snapshot, configuration, model_providers
        ),
    )
    photo_worker = AnalysisWorker(
        repository,
        analyze_single_photo,
        generate_narration,
        configuration_service=configuration,
        model_provider_service=model_providers,
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
        raw_image_dirs,
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
