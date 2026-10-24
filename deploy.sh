#!/usr/bin/env bash
# 在现有仓库中更新并部署 RAGv3。
#
# 部署流程（顺序即失败边界）：
#   1. 预检：Docker/Compose、.env 存在且权限收紧、端口绑定提示
#   2. git pull --ff-only
#   3. 构建镜像
#   4. 到期备份（失败即中止，旧容器继续跑）
#   5. 启动服务（docker compose 细节见 start_stack）
#   6. 健康门禁：轮询 /ready（存在时）或 /health，失败则打印容器状态/日志并**非 0 退出**
#
# F-06：旧版最后一步是"启动 + ps"，容器起不来/降级也算"部署成功"。
# 现在没有一次 200 就判失败。
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 与应用端口保持一致：docker-compose.yml 默认把容器 8000 发布到宿主机 8000。
published_port="${RAG_PUBLISHED_PORT:-8000}"
health_base_url="${RAG_HEALTH_BASE_URL:-http://127.0.0.1:${published_port}}"
health_retries="${RAG_HEALTH_RETRIES:-24}"
health_interval_seconds="${RAG_HEALTH_INTERVAL_SECONDS:-5}"
compose_service="${RAG_COMPOSE_SERVICE:-rag-app}"
health_probe_timeout_seconds="${RAG_HEALTH_PROBE_TIMEOUT_SECONDS:-5}"

log() {
    echo "==> $*"
}

warn() {
    echo "警告：$*" >&2
}

die() {
    echo "错误：$*" >&2
    return 1
}

# ── 预检 ───────────────────────────────────────────────────────────────────

require_docker() {
    if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
        die "请先安装 Docker Engine 和 Docker Compose v2。"
        return 1
    fi
}

ensure_env_file() {
    if [[ -f .env ]]; then
        return 0
    fi
    cp .env.example .env
    chmod 600 .env 2>/dev/null || true
    die "已创建 .env，请填写 API Key 和 REDIS_PASSWORD 后重新运行。"
    return 1
}

# .env 里有模型 API Key 和 Redis 密码，默认 644 会让同机其他用户直接读到。
harden_env_permissions() {
    if [[ ! -f .env ]]; then
        return 0
    fi
    local mode
    mode="$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null || true)"
    if [[ -z "$mode" ]]; then
        warn "无法读取 .env 的权限位，请确认只有部署用户可以读取（含 API Key 与 REDIS_PASSWORD）。"
        return 0
    fi
    if [[ "$mode" == "600" ]]; then
        return 0
    fi
    if chmod 600 .env 2>/dev/null; then
        log "已把 .env 权限收紧为 600（原 ${mode}）"
    else
        warn ".env 权限为 ${mode} 且无法收紧为 600；该文件含 API Key 与 REDIS_PASSWORD。"
    fi
}

# 只提示，不改 compose：端口绑定属于部署拓扑（是否在前置 TLS 反代之后），
# 由管理员决定；本脚本只保证"不会静默地把服务暴露到全部网卡"。
check_port_binding() {
    local compose_file="${1:-docker-compose.yml}"
    if [[ ! -f "$compose_file" ]]; then
        warn "未找到 ${compose_file}，跳过端口绑定检查。"
        return 0
    fi
    local mapping
    mapping="$(grep -E "^[[:space:]]*-[[:space:]]*[\"']?[0-9.:]+:${published_port}[\"']?[[:space:]]*\$" \
        "$compose_file" | head -n1 || true)"
    if [[ -z "$mapping" ]]; then
        warn "在 ${compose_file} 中未找到发布到 ${published_port} 的端口映射，健康检查可能打不到服务。"
        return 0
    fi
    if [[ "$mapping" =~ 127\.0\.0\.1|\[::1\]|::1: ]]; then
        log "端口仅绑定回环地址：${mapping#*-}"
    else
        warn "端口映射 ${mapping#*-} 绑定在全部网卡。公网部署应在前置 TLS 反向代理后运行，" \
            "或把 docker-compose.yml 的映射改为 \"127.0.0.1:${published_port}:8000\"。"
    fi
}

# ── 健康门禁 ───────────────────────────────────────────────────────────────

# 打印 URL 的 HTTP 状态码；连不上/无探针时输出 000（永不失败）。
http_status() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl --silent --output /dev/null --write-out '%{http_code}' \
            --max-time "$health_probe_timeout_seconds" "$url" 2>/dev/null || true
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$url" "$health_probe_timeout_seconds" <<'PY' 2>/dev/null || true
import sys
import urllib.error
import urllib.request

url, timeout = sys.argv[1], float(sys.argv[2])
try:
    print(urllib.request.urlopen(url, timeout=timeout).status)
except urllib.error.HTTPError as error:
    print(error.code)
except Exception:
    print("000")
PY
        return 0
    fi
    printf '000'
}

# /ready 存在时用它（能区分"依赖未就绪"），否则退回 /health（S7 落地前）。
# 判定"存在"= 不是 404 且能连上；503 表示存在但未就绪，继续等它。
detect_health_path() {
    if [[ -n "${RAG_HEALTH_PATH:-}" ]]; then
        printf '%s' "$RAG_HEALTH_PATH"
        return 0
    fi
    local ready_status
    ready_status="$(http_status "${health_base_url}/ready")"
    case "$ready_status" in
        404|000|"")
            printf '%s' "/health"
            ;;
        *)
            printf '%s' "/ready"
            ;;
    esac
}

# 轮询到 200 为止；任何非 200（含 503 降级）都算未通过。
verify_health() {
    local path="$1"
    local url="${health_base_url}${path}"
    local attempt status
    for ((attempt = 1; attempt <= health_retries; attempt++)); do
        status="$(http_status "$url")"
        if [[ "$status" == "200" ]]; then
            log "健康检查通过：${url}（第 ${attempt} 次探测）"
            return 0
        fi
        echo "    等待 ${url} 就绪（HTTP ${status}，第 ${attempt}/${health_retries} 次）" >&2
        if ((attempt < health_retries)); then
            sleep "$health_interval_seconds"
        fi
    done
    return 1
}

# 部署失败时把现场留给运维：容器状态 + 最近日志 + 回滚入口。
report_deploy_failure() {
    local health_path="$1"
    echo "错误：部署后健康检查失败（${health_base_url}${health_path}，${health_retries} 次 × ${health_interval_seconds}s）。" >&2
    docker compose ps >&2 || true
    docker compose logs --tail=100 "$compose_service" >&2 || true
    echo "回滚建议：git checkout <上一个已发布的提交>，然后重新执行 bash deploy.sh（本脚本可重复执行）。" >&2
}

# ── 部署步骤 ───────────────────────────────────────────────────────────────

maybe_pull() {
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        log "拉取最新代码"
        git pull --ff-only
    fi
}

build_image() {
    log "构建应用镜像"
    docker compose build "$compose_service"
}

backup_before_up() {
    log "检查并创建到期备份"
    docker compose run --rm --no-deps "$compose_service" \
        python scripts/backup.py create-if-due --trigger deploy
}

start_stack() {
    log "启动服务"
    docker compose up -d || return $?
    docker compose ps || return $?
}

main() {
    cd "$project_dir"

    require_docker || return $?
    ensure_env_file || return $?
    harden_env_permissions || return $?
    maybe_pull || return $?
    check_port_binding || return $?
    build_image || return $?
    backup_before_up || return $?
    start_stack || return $?

    local health_path
    health_path="$(detect_health_path)"
    log "等待服务健康（${health_base_url}${health_path}）"
    if ! verify_health "$health_path"; then
        report_deploy_failure "$health_path"
        return 1
    fi
    log "部署完成"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    if ! main "$@"; then
        exit 1
    fi
fi
