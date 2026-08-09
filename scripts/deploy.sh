#!/bin/bash
# 部署脚本：拉取最新代码 + 使用缓存重建
set -e

cd "$(dirname "$0")/.."

echo "=== 拉取最新代码 ==="
git pull

echo "=== 使用缓存重建 ==="
docker compose build rag-app

echo "=== 启动容器 ==="
docker compose up -d

echo "=== 部署完成 ==="
docker logs ragv3 2>&1 | tail -5
