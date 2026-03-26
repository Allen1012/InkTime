#!/bin/bash

# InkTime 照片分析脚本
# 加载 .env 配置并执行分析

# 进入项目根目录
cd "$(dirname "$(dirname "$0")")"

# 加载环境变量
set -a
source .env
set +a

# 执行分析脚本
python src/analysis/analyze_photos_docker.py
