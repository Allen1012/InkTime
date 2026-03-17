# InkTime 照片分析模块 NAS Docker 部署指南

## 项目概述

本指南介绍如何在 NAS 的 Docker 环境中部署 InkTime 照片分析模块，实现照片的自动分析、评分和文案生成。

## 准备工作

### 1. 硬件要求
- NAS 设备（支持 Docker）
- 足够的存储空间（用于照片和数据库）
- 稳定的网络连接
- 推荐至少 4GB 内存（用于运行 VLM 模型）

### 2. 软件要求
- Docker 和 Docker Compose
- 本地或云端 VLM 模型服务（如 LM Studio、OpenAI API 等）

## 部署步骤

### 步骤 1：准备项目文件

1. **克隆项目代码**
   ```bash
   git clone https://github.com/yourusername/InkTime.git
   cd InkTime
   ```

2. **创建必要的目录**
   ```bash
   mkdir -p output logs
   ```

### 步骤 2：配置 Docker 环境

1. **修改 docker-compose.yml 文件**
   - 替换照片目录路径：
   ```yaml
   # 挂载照片目录，需要根据实际情况修改路径
   - /path/to/your/photos:/photos:ro
   ```
   改为：
   ```yaml
   # 挂载照片目录，需要根据实际情况修改路径
   - /volume1/photos:/photos:ro
   ```

2. **配置环境变量**
   - 根据你的 VLM 服务情况，修改环境变量：
   ```yaml
   environment:
     - LMSTUDIO_URL=http://host.docker.internal:1234/v1/chat/completions
     - LMSTUDIO_MODEL=qwen3-vl-32b-instruct
     - LMSTUDIO_API_KEY=
   ```

### 步骤 3：构建和启动容器

1. **构建镜像**
   ```bash
   docker-compose build
   ```

2. **启动服务**
   ```bash
   docker-compose up -d
   ```

3. **查看日志**
   ```bash
   docker-compose logs -f
   ```

### 步骤 4：配置 VLM 服务

#### 选项 A：使用本地 LM Studio

1. 在 NAS 或同一网络的电脑上安装并启动 LM Studio
2. 下载支持视觉的模型（如 qwen3-vl-32b-instruct）
3. 启动本地服务器，确保端口为 1234

#### 选项 B：使用云端 API

1. 修改 docker-compose.yml 中的环境变量：
   ```yaml
   environment:
     - API_URL=https://api.openai.com/v1/chat/completions
     - MODEL_NAME=gpt-4o
     - API_KEY=your-openai-api-key
   ```

### 步骤 5：运行照片分析

1. **手动运行分析**
   ```bash
   docker-compose exec inktime-analyzer python analyze_photos_docker.py
   ```

2. **设置定时任务**
   - 在 NAS 的任务计划中添加定时任务：
   ```bash
   docker-compose -f /path/to/InkTime/docker-compose.yml exec inktime-analyzer python analyze_photos_docker.py
   ```

## 配置说明

### 环境变量配置

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| IMAGE_DIR | /photos | 照片目录路径 |
| DB_PATH | ./photos.db | 数据库文件路径 |
| API_URL | http://host.docker.internal:1234/v1/chat/completions | VLM API 地址 |
| MODEL_NAME | qwen3-vl-32b-instruct | 模型名称 |
| API_KEY | | API 密钥 |
| BATCH_LIMIT | | 每次处理的照片数量限制 |
| TIMEOUT | 600 | 请求超时时间（秒） |
| VLM_MAX_LONG_EDGE | 2560 | 图片长边最大尺寸 |
| HOME_LAT | 22.543096 | 家的纬度 |
| HOME_LON | 114.057865 | 家的经度 |
| HOME_RADIUS_KM | 60.0 | 家的半径（公里） |

### 目录结构

```
InkTime/
├── Dockerfile              # Docker 镜像定义
├── docker-compose.yml      # Docker 服务配置
├── analyze_photos_docker.py # Docker 适配的分析脚本
├── data/
│   └── world_cities_zh.csv # 中文城市数据库
├── output/                # 输出目录
├── logs/                  # 日志目录
└── photos.db             # SQLite 数据库
```

## 常见问题

### 1. 照片分析速度慢

**解决方案：**
- 调整 BATCH_LIMIT 限制每次处理的照片数量
- 降低 VLM_MAX_LONG_EDGE 减小图片尺寸
- 使用更强大的 VLM 模型或硬件

### 2. 无法连接 VLM 服务

**解决方案：**
- 检查网络连接
- 确认 VLM 服务是否正常运行
- 验证 API_URL 和 API_KEY 是否正确

### 3. 内存不足

**解决方案：**
- 增加 NAS 的内存
- 减小 BATCH_LIMIT
- 使用更轻量级的模型

### 4. 照片分析失败

**解决方案：**
- 检查照片格式是否支持
- 查看容器日志了解具体错误
- 确保照片文件权限正确

## 性能优化

1. **使用 SSD 存储**：数据库和临时文件使用 SSD 可以显著提高性能
2. **合理设置批处理大小**：根据 NAS 性能调整 BATCH_LIMIT
3. **优化 VLM 模型**：选择适合硬件的模型大小
4. **定期清理数据库**：移除不需要的记录

## 监控和维护

1. **查看分析进度**：
   ```bash
   docker-compose logs -f
   ```

2. **检查数据库状态**：
   ```bash
   docker-compose exec inktime-analyzer sqlite3 photos.db "SELECT COUNT(*) FROM photo_scores;"
   ```

3. **备份数据库**：
   ```bash
   cp photos.db photos.db.bak
   ```

4. **更新容器**：
   ```bash
   docker-compose pull
   docker-compose up -d
   ```

## 总结

通过本指南，你可以在 NAS 的 Docker 环境中成功部署 InkTime 照片分析模块，实现照片的自动分析、评分和文案生成。系统会定期扫描照片目录，使用 VLM 模型分析每张照片，并将结果存储在 SQLite 数据库中，为 InkTime 电子相框提供高质量的照片内容。

如果遇到问题，请查看容器日志或参考项目文档获取更多帮助。
