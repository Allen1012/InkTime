#!/usr/bin/env bash
# 定时扫描照片目录：登记新照片并排队分析，分析本身由独立工作进程执行。
# 用法： scripts/scan_library.sh
# crontab 示例（每天 03:30 扫描，05:00 由 daily_render.sh 选片渲染）：
#   30 3 * * * /path/to/InkTime/scripts/scan_library.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/scan.log"
LOCK_DIR="$PROJECT_DIR/tmp/inktime_scan.lockdir"

mkdir -p "$LOG_DIR" "$PROJECT_DIR/tmp"
cd "$PROJECT_DIR"

# 目录锁保证同一时间只有一次扫描，避免重复登记与事务争用
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date '+%F %T')] another scan is running, skip." >> "$LOG_FILE"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[$(date '+%F %T')] ERROR: python not found in venv: $PYTHON_BIN" >> "$LOG_FILE"
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "[$(date '+%F %T')] ERROR: .env not found in $PROJECT_DIR" >> "$LOG_FILE"
  exit 1
fi

echo "[$(date '+%F %T')] scan start" >> "$LOG_FILE"
if "$PYTHON_BIN" -m src.analysis.run_scan >> "$LOG_FILE" 2>&1; then
  echo "[$(date '+%F %T')] scan done" >> "$LOG_FILE"
else
  status=$?
  echo "[$(date '+%F %T')] scan failed with exit code $status" >> "$LOG_FILE"
  exit "$status"
fi
