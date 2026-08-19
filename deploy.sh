#!/usr/bin/env bash
# 在现有仓库中更新并部署 RAGv3。
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "错误：请先安装 Docker Engine 和 Docker Compose v2。" >&2
    exit 1
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "已创建 .env，请填写 API Key 和 REDIS_PASSWORD 后重新运行。" >&2
    exit 1
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "==> 拉取最新代码"
    git pull --ff-only
fi

echo "==> 构建应用镜像"
docker compose build rag-app

echo "==> 检查并创建到期备份"
docker compose run --rm --no-deps rag-app \
  python scripts/backup.py create-if-due --trigger deploy

echo "==> 启动服务"
docker compose up -d
docker compose ps
