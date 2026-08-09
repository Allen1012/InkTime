"""InkTime 独立照片分析工作进程入口。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from src.analysis.photo_analyzer import analyze_single_photo, generate_narration
from src.migrations import migrate_database
from src.server.admin_jobs import AdminJobRepository, AnalysisWorker, JobRuntimeConfig

ROOT_DIR = Path(__file__).resolve().parents[2]


def _absolute_path(value: str) -> Path:
    """按项目根目录解析环境中的数据库路径。"""
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT_DIR / path).resolve()


def main() -> None:
    """先校验共享任务配置，再迁移明确数据库并启动后台工作进程。"""
    load_dotenv(ROOT_DIR / ".env", override=False)
    runtime = JobRuntimeConfig.from_mapping(os.environ)
    database_path = _absolute_path(os.environ.get("DB_PATH", "./data/photos.db"))
    migrate_database(database_path)
    repository = AdminJobRepository(database_path, runtime.max_attempts)
    worker = AnalysisWorker(
        repository,
        analyze_single_photo,
        generate_narration,
        lease_seconds=runtime.lease_seconds,
        renew_seconds=runtime.renew_seconds,
        poll_seconds=runtime.poll_seconds,
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
