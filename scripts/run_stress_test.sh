#!/bin/bash
# RAG 系统压测运行脚本
# 用法：bash scripts/run_stress_test.sh [host] [users] [duration]
#
# 示例：
#   bash scripts/run_stress_test.sh                           # 默认：localhost:8000, 50用户, 5分钟
#   bash scripts/run_stress_test.sh http://39.105.89.99:8000  # 指定服务器
#   bash scripts/run_stress_test.sh http://localhost:8000 100 10m  # 100用户, 10分钟

set -e

mkdir -p benchmarks

HOST=${1:-"http://localhost:8000"}
USERS=${2:-50}
DURATION=${3:-5m}
SPAWN_RATE=10

echo "============================================================"
echo " RAG 系统极致压测"
echo "============================================================"
echo " 目标:     $HOST"
echo " 并发用户: $USERS"
echo " 持续时间: $DURATION"
echo " 启动速率: $SPAWN_RATE/秒"
echo "============================================================"
echo ""

# 检查 locust 是否安装
if ! command -v locust &> /dev/null; then
    echo "正在安装 locust..."
    pip install locust
fi

# 运行压测
locust -f scripts/locustfile.py \
    --host "$HOST" \
    --headless \
    --users "$USERS" \
    --spawn-rate "$SPAWN_RATE" \
    --run-time "$DURATION" \
    --csv=benchmarks/stress_test \
    --html=benchmarks/stress_test.html \
    2>&1 | tee benchmarks/stress_test.log

echo ""
echo "============================================================"
echo " 压测完成！报告已保存到 benchmarks/"
echo "============================================================"
