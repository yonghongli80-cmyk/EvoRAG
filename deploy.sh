#!/bin/bash
# RAGv3 一键部署脚本
# 用法：在 Linux 服务器上执行
# curl -fsSL https://raw.githubusercontent.com/<你的用户名>/RAGv3/main/deploy.sh | bash
# 或者上传到服务器后 bash deploy.sh

set -e

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║   RAGv3 一键部署                 ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ── 配置 ─────────────────────────────────────────────
APP_DIR="/opt/RAGv3"
PORT=8000

# ── 1. 安装 Docker ────────────────────────────────────
echo "[1/5] 检查 Docker..."
if ! command -v docker &> /dev/null; then
    echo "  安装 Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "  Docker 安装完成"
else
    echo "  Docker 已安装"
fi

# ── 2. 安装 Docker Compose ────────────────────────────
echo "[2/5] 检查 Docker Compose..."
if ! docker compose version &> /dev/null; then
    echo "  安装 Docker Compose 插件..."
    apt-get update && apt-get install -y docker-compose-plugin
    echo "  Docker Compose 安装完成"
else
    echo "  Docker Compose 已安装"
fi

# ── 3. 克隆项目 ────────────────────────────────────────
echo "[3/5] 获取项目代码..."
if [ -d "$APP_DIR" ]; then
    echo "  项目目录已存在，更新代码..."
    cd "$APP_DIR"
    git pull
else
    # 如果没有 git 仓库，需要先上传代码
    echo "  请先将项目代码上传到 $APP_DIR"
    echo "  方式1: git clone <你的仓库地址> $APP_DIR"
    echo "  方式2: scp -r ./RAGv3 root@服务器IP:$APP_DIR"
    exit 1
fi

# ── 4. 配置环境变量 ────────────────────────────────────
echo "[4/5] 配置环境变量..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "  ⚠️  请编辑 $APP_DIR/.env 填入你的 API Key"
    echo "  填好后重新运行此脚本"
    echo ""
    echo "  必填项："
    echo "    RAG_DEEPSEEK_API_KEY=你的key"
    echo "    RAG_BAILIAN_API_KEY=你的key"
    echo ""
    exit 1
else
    echo "  .env 已存在"
fi

# ── 5. 构建并启动 ─────────────────────────────────────
echo "[5/5] 构建并启动服务..."
cd "$APP_DIR"
docker compose build
docker compose up -d

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║   部署完成！                      ║"
echo "  ║                                  ║"
echo "  ║   访问: http://$(hostname -I | awk '{print $1}'):${PORT}  ║"
echo "  ║                                  ║"
echo "  ║   查看日志: docker compose logs -f  ║"
echo "  ║   重启服务: docker compose restart  ║"
echo "  ║   停止服务: docker compose down     ║"
echo "  ╚══════════════════════════════════╝"
echo ""
