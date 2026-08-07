#!/usr/bin/env bash
set -euo pipefail

# InkTime 照片分析脚本
# 加载 .env 配置并用 venv 的 python 执行增量分析

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
cd "$PROJECT_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: python not found in venv: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "ERROR: .env not found in $PROJECT_DIR" >&2
  exit 1
fi

# 加载环境变量
set -a
# shellcheck disable=SC1091
source .env
set +a

mkdir -p logs data

# 统一使用模块入口，确保数据库迁移与公共模块可被稳定导入。
# 支持传参覆盖，如 BATCH_LIMIT=20 ./scripts/run_analysis.sh
exec "$PYTHON_BIN" -m src.analysis.analyze_photos_docker "$@"
