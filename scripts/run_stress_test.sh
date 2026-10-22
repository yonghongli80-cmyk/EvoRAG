#!/bin/bash
# RAG 系统压测运行脚本
# 用法：bash scripts/run_stress_test.sh [host] [users] [duration]
#
# 示例：
#   bash scripts/run_stress_test.sh                                  # 默认：localhost:8000, 50 用户, 5 分钟
#   ALLOW_REMOTE_STRESS=1 bash scripts/run_stress_test.sh http://10.0.0.5:8000   # 远端需显式确认
#   bash scripts/run_stress_test.sh http://localhost:8000 100 10m    # 100 用户, 10 分钟
#
# F-10（2026-10-22）两条硬约束：
#   1. **版本固定**：locust 版本由 scripts/requirements-stress.lock 固定（当前 2.46.7，即实测基线
#      所用版本）。脚本不再 `pip install locust` 自动装最新版——那既不可复现，也是把一条可执行
#      供应链接到 CI/运维手里。版本不符直接退出 3，并给出安装命令。
#   2. **远端确认**：目标不是回环地址时，必须显式 `ALLOW_REMOTE_STRESS=1`，否则退出 2。
#      压测会把目标服务打满，误把生产地址当测试地址的代价是真实故障。
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

mkdir -p benchmarks

HOST="${1:-http://localhost:8000}"
USERS="${2:-50}"
DURATION="${3:-5m}"
SPAWN_RATE="${SPAWN_RATE:-10}"
ALLOW_REMOTE="${ALLOW_REMOTE_STRESS:-0}"

# 版本来源：scripts/requirements-stress.lock（唯一的固定处）。可用 LOCUST_PIN 显式覆盖。
locust_pin="${LOCUST_PIN:-}"
if [[ -z "$locust_pin" && -f scripts/requirements-stress.lock ]]; then
    locust_pin="$(grep -E '^locust==' scripts/requirements-stress.lock | head -n1 || true)"
fi
locust_pin="${locust_pin:-locust==2.46.7}"
required_version="${locust_pin#locust==}"

is_loopback_target() {
    case "$HOST" in
        http://127.0.0.1* | https://127.0.0.1* | http://localhost* | https://localhost*)
            return 0
            ;;
        "http://[::1]"* | "https://[::1]"*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

check_target() {
    if is_loopback_target; then
        return 0
    fi
    if [[ "$ALLOW_REMOTE" == "1" ]]; then
        echo "==> 远端目标已确认（ALLOW_REMOTE_STRESS=1）：$HOST"
        return 0
    fi
    {
        echo "错误：$HOST 不是回环地址，压测会把目标服务打满。"
        echo "确认它是隔离/专用的压测环境后，显式确认："
        echo "  ALLOW_REMOTE_STRESS=1 bash scripts/run_stress_test.sh $HOST $USERS $DURATION"
    } >&2
    return 2
}

check_locust() {
    if ! command -v locust >/dev/null 2>&1; then
        {
            echo "错误：未找到 locust。脚本不再自动安装（版本必须可复现）。"
            echo "安装：pip install -r scripts/requirements-stress.lock"
        } >&2
        return 3
    fi
    local installed
    installed="$(locust --version 2>/dev/null | sed -n 's/.* \([0-9][0-9.]*\).*/\1/p' | head -n1)"
    if [[ -z "$installed" ]]; then
        {
            echo "错误：无法读取 locust 版本，无法保证压测结果可比。"
            echo "命令输出：$(locust --version 2>&1 | head -n1)"
        } >&2
        return 3
    fi
    if [[ "$installed" != "$required_version" ]]; then
        {
            echo "错误：需要 ${locust_pin}（实测基线所用版本），当前 ${installed}。"
            echo "安装：pip install -r scripts/requirements-stress.lock"
            echo "（确实要换版本时，显式设置 LOCUST_PIN 并在报告里写明差异。）"
        } >&2
        return 3
    fi
    return 0
}

# 探测目标是否为一份可用的服务；只告警，不阻塞（压测本身可能就是来看降级行为的）。
probe_health() {
    if ! command -v curl >/dev/null 2>&1; then
        return 0
    fi
    curl --fail --silent --max-time 5 "${HOST%/}/health" >/dev/null 2>&1
}

check_target
check_locust

if ! probe_health; then
    echo "警告：${HOST%/}/health 未通过探测；先确认目标环境正常，否则 P95/P99 无意义。" >&2
fi

echo "============================================================"
echo " RAG 系统压测"
echo "============================================================"
echo " 目标:     $HOST"
echo " 并发用户: $USERS"
echo " 持续时间: $DURATION"
echo " 启动速率: $SPAWN_RATE/秒"
echo " locust:   ${locust_pin}"
echo "============================================================"
echo ""

# 可复现档案：命令、版本、时间（报告与基线的对应关系不靠记忆）。
{
    echo "# generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# locust: ${locust_pin}"
    echo "bash scripts/run_stress_test.sh ${HOST} ${USERS} ${DURATION}"
} > benchmarks/stress_test.command.txt

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
echo " 压测完成！报告已保存到 benchmarks/（command.txt 记录本次命令）"
echo "============================================================"
