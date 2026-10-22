# ── Stage 1: 构建前端 ──────────────────────────────────
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ── Stage 2: 运行后端 ─────────────────────────────────
FROM python:3.12-slim-bookworm

WORKDIR /app

# 使用国内 apt 镜像源（大幅加速构建）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true

# 系统依赖：build-essential (编译) + default-jre-headless (PDF 解析)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    default-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && if [ ! -d /usr/lib/jvm/default-java ]; then ln -s /usr/lib/jvm/java-17-openjdk-* /usr/lib/jvm/default-java; fi

# Java 环境变量（通用架构，避免硬编码 amd64）
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# 标记 Docker 环境（start.py 根据此变量决定监听地址）
ENV DOCKER_CONTAINER=1

# Python 依赖
COPY requirements.lock ./
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.lock

# 项目代码
COPY . .

# 前端静态文件（从 Stage 1 复制）
COPY --from=frontend-builder /app/static /app/static

# 仓库文件备份到不被 volume 覆盖的位置（仅限 git 追踪的文件，不含用户上传）
RUN mkdir -p /app/repo_upload && \
    if [ -f /app/data/upload/.repo_files ]; then \
      cp /app/data/upload/.repo_files /app/repo_upload/.repo_files && \
      while IFS= read -r fname; do \
        [ -f "/app/data/upload/$fname" ] && cp "/app/data/upload/$fname" "/app/repo_upload/"; \
      done < /app/data/upload/.repo_files; \
    fi

# 数据目录（运行时挂载卷）与非特权运行用户
RUN useradd -m -u 10001 rag && \
    mkdir -p /app/data/upload /app/qdrant_data /app/backups && \
    chown -R rag:rag /app

USER rag

# 暴露端口
EXPOSE 8000

# PID 1 的停止契约（F-04）：
# * JSON 数组 = exec form，PID 1 就是 python，没有 /bin/sh 夹在中间吞信号；
# * `-u` 让启动/停止日志不滞留在缓冲区里（容器日志是排障的唯一入口）；
# * start.py 作为 PID 1 自己接管 SIGTERM/SIGINT，转发给 uvicorn 子进程，并在
#   RAG_SHUTDOWN_TIMEOUT（默认 10s）内没退出时强杀——详见 start.py 与
#   tests/test_start_signal_forwarding.py。
# 没有改成 `CMD ["python", "-m", "uvicorn", ...]`（让 uvicorn 直接当 PID 1）：那样虽然
# 信号最省事，但会跳过 start.py 里的端口预检与"远程绑定 + 初始密码"安全护栏。
STOPSIGNAL SIGTERM
CMD ["python", "-u", "start.py"]
