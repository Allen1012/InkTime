FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    libexiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY . .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 创建必要的目录
RUN mkdir -p output logs

# 设置环境变量
ENV LMSTUDIO_URL=http://host.docker.internal:1234/v1/chat/completions
ENV LMSTUDIO_MODEL=qwen3-vl-32b-instruct
ENV LMSTUDIO_API_KEY=

# 启动脚本
CMD ["python", "src/analysis/analyze_photos.py"]
