"""System, health, readiness, and metrics routes."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse, Response

import rag.vector_store as vector_store
from config import settings
from rag.api_security import require_admin, require_user
from rag.context import get_gap_analyzer
from rag.llm_pool import get_llm_pool_router

logger = logging.getLogger(__name__)

router = APIRouter()

_CHARTS_DIR = Path(__file__).resolve().parents[2] / "data" / "charts"


@router.get("/data/charts/{filename}", include_in_schema=False)
def download_chart(filename: str, user: dict = Depends(require_user)):
    """Serve a generated chart only to an authenticated session."""
    if Path(filename).name != filename or not filename.endswith(".png"):
        return JSONResponse(status_code=404, content={"detail": "资源不存在"})
    path = _CHARTS_DIR / filename
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "资源不存在"})
    return FileResponse(path, media_type="image/png")

# ── Phase 0 观测面 ──────────────────────────────────────────────────
# `/health` 保持既有契约（容器 healthcheck 使用）；`/ready` 是依赖就绪门禁：
# 任一必需依赖不可用时 503，并逐组件给出状态。`/metrics` 只读、管理员可见，
# 只输出聚合计数与耗时百分位，不含问题文本、文档名、请求路径或凭据。
_PROBE_TIMEOUT_SECONDS = 2.0
_REDIS_SOCKET_TIMEOUT_SECONDS = 1.0
_HTTP_SAMPLE_LIMIT = 512
_TRACE_SAMPLE_LIMIT = 500
_TRACE_QUERY_TIMEOUT_SECONDS = 2.0
#: 目前执行轨迹里会落盘的分段耗时（见 `RAGPipeline._attach_jev_stage`）。
#: 轨迹键 -> Prometheus `stage` 标签值（标签用短名，键保持落盘格式）。
_STAGE_LABELS = (
    ("preparation_ms", "preparation"),
    ("jev_preflight_ms", "jev_preflight"),
    ("jev_passages_ms", "jev_passages"),
)
_STAGE_KEYS = tuple(key for key, _ in _STAGE_LABELS)
#: 执行轨迹里已知的 route 取值；未知值归入 "other"，避免标签基数无限增长。
_TRACE_ROUTES = ("rag", "structured", "tool", "clarification", "legacy", "fixed", "arena", "blocked", "error")
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
#: 百分位标签与其分位点（Prometheus summary 的 `quantile` 维度）。
_QUANTILES = (("0.5", 0.5), ("0.95", 0.95), ("0.99", 0.99))

_metric_lock = threading.Lock()
_http_requests: dict[tuple[str, str, str], int] = {}
_http_durations: dict[tuple[str, str], deque[float]] = {}


def record_http_request(
    method: str, route: str, status: int, duration_seconds: float
) -> None:
    """Record one served request; ``route`` must be a route template, never a raw path."""
    method = method.upper()
    route = route or "unmatched"
    status_key = str(status)
    with _metric_lock:
        key = (method, route, status_key)
        _http_requests[key] = _http_requests.get(key, 0) + 1
        samples = _http_durations.setdefault(
            (method, route), deque(maxlen=_HTTP_SAMPLE_LIMIT)
        )
        samples.append(max(0.0, duration_seconds))


def reset_http_metrics() -> None:
    """Drop the process-local HTTP counters/samples (tests and manual windows)."""
    with _metric_lock:
        _http_requests.clear()
        _http_durations.clear()


def _snapshot_http_metrics() -> tuple[dict, dict]:
    with _metric_lock:
        return (
            dict(_http_requests),
            {key: list(samples) for key, samples in _http_durations.items()},
        )


def _percentile(samples: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile over unsorted samples; empty input yields 0.0."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _emit_help(lines: list[str], name: str, help_text: str, metric_type: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")


def _emit_summary(
    lines: list[str],
    name: str,
    samples: Sequence[float],
    labels: dict[str, str] | None = None,
) -> None:
    """Emit a Prometheus summary (quantiles + count + sum) for one label set."""
    labels = labels or {}
    base = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in labels.items()
    )
    for label, quantile in _QUANTILES:
        if not samples:
            break
        rendered = f'quantile="{label}"'
        selector = f"{{{base},{rendered}}}" if base else f"{{{rendered}}}"
        lines.append(f"{name}{selector} {_percentile(samples, quantile):.6f}")
    suffix = f"{{{base}}}" if base else ""
    lines.append(f"{name}_count{suffix} {len(samples)}")
    lines.append(f"{name}_sum{suffix} {sum(samples):.6f}")


# ── 依赖探针：/ready 与 /metrics 共用 ───────────────────────────────


def _probe_sqlite() -> dict[str, str]:
    """Prove SQLite is usable with a single ``SELECT 1`` (no schema/data writes).

    文件不存在时直接判失败，避免就绪探针顺手创建一个空库。
    """
    try:
        if not Path(settings.analysis_db_path).is_file():
            raise FileNotFoundError(settings.analysis_db_path)
        conn = sqlite3.connect(settings.analysis_db_path, timeout=_PROBE_TIMEOUT_SECONDS)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # 探针失败必须转成状态，绝不把 500 抛给负载均衡
        logger.warning("readiness: sqlite probe failed (%s)", type(exc).__name__)
        return {"status": "error", "detail": "sqlite_unavailable"}
    return {"status": "ok"}


def _probe_qdrant() -> dict[str, str]:
    """Check the shared client can enumerate collections (local lock or server RPC)."""
    try:
        client = vector_store._get_client()
        client.get_collections()
    except Exception as exc:
        logger.warning("readiness: qdrant probe failed (%s)", type(exc).__name__)
        return {"status": "error", "detail": "qdrant_unavailable"}
    return {"status": "ok", "mode": vector_store.client_mode()}


def _probe_redis() -> dict[str, str]:
    """Ping Redis only when a URL is configured; skipped otherwise."""
    url = (settings.redis_url or "").strip()
    if not url:
        return {"status": "skipped", "detail": "not_configured"}
    client = None
    try:
        import redis

        client = redis.from_url(
            url,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        client.ping()
    except Exception as exc:
        logger.warning("readiness: redis probe failed (%s)", type(exc).__name__)
        # Redis is an optional cache on single-node deployments; it only blocks
        # readiness when the operator explicitly marks it required (shared
        # cache/lock deployments).
        status = "error" if settings.redis_required else "degraded"
        return {"status": status, "detail": "redis_unavailable"}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("readiness: redis probe close failed", exc_info=True)
    return {"status": "ok"}


def _probe_dependencies() -> dict[str, dict[str, str]]:
    return {
        "sqlite": _probe_sqlite(),
        "qdrant": _probe_qdrant(),
        "redis": _probe_redis(),
    }


# ── 执行轨迹聚合：/metrics 的查询分段 ───────────────────────────────


def _read_query_traces(
    limit: int = _TRACE_SAMPLE_LIMIT,
) -> list[tuple[str, float | None, dict]]:
    """Read the recent execution-log window read-only; missing DB yields no samples."""
    path = (settings.memory_db_path or "").strip()
    if not path:
        return []
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
    except (OSError, ValueError):
        logger.warning("metrics: execution log path is not usable as a file URI")
        return []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=_TRACE_QUERY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:
        logger.debug("metrics: execution log unavailable (%s)", type(exc).__name__)
        return []
    try:
        rows = conn.execute(
            "SELECT route, total_ms, details FROM execution_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("metrics: execution log schema unavailable (%s)", type(exc).__name__)
        return []
    finally:
        conn.close()

    traces: list[tuple[str, float | None, dict]] = []
    for route, total_ms, details in rows:
        stages: dict[str, float] = {}
        if details:
            try:
                payload = json.loads(details)
            except (TypeError, ValueError):
                payload = {}
            raw = payload.get("stage_timings") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                stages = {
                    key: float(value)
                    for key, value in raw.items()
                    if key in _STAGE_KEYS and isinstance(value, int | float)
                }
        total = float(total_ms) if total_ms is not None else None
        route_label = route if route in _TRACE_ROUTES else "other"
        traces.append((route_label, total, stages))
    return traces


def _query_trace_metrics() -> dict:
    traces = _read_query_traces()
    totals: dict[str, list[float]] = {}
    stages: dict[str, dict[str, list[float]]] = {key: {} for key in _STAGE_KEYS}
    for route, total, frame_stages in traces:
        if total is not None and total > 0.0:
            totals.setdefault(route, []).append(total / 1000.0)
        for key, value in frame_stages.items():
            if value >= 0.0:
                stages[key].setdefault(route, []).append(value / 1000.0)
    return {"traces": len(traces), "totals": totals, "stages": stages}


def _llm_pool_models() -> list[dict]:
    """Snapshot provider health/inflight without any credential or endpoint detail."""
    try:
        models = get_llm_pool_router().list_models()
    except Exception as exc:
        logger.warning("metrics: LLM pool snapshot unavailable (%s)", type(exc).__name__)
        return []
    return [model for model in models if isinstance(model, dict) and model.get("id")]


def _render_metrics() -> str:
    lines: list[str] = []

    _emit_help(lines, "rag_up", "1 when the API process serves this endpoint.", "gauge")
    lines.append("rag_up 1")

    http_requests, http_durations = _snapshot_http_metrics()
    _emit_help(
        lines,
        "rag_http_requests_total",
        "HTTP requests served, by method, route template and status.",
        "counter",
    )
    for (method, route, status), count in sorted(http_requests.items()):
        lines.append(
            "rag_http_requests_total"
            f'{{method="{_escape_label(method)}",route="{_escape_label(route)}",'
            f'status="{_escape_label(status)}"}} {count}'
        )

    _emit_help(
        lines,
        "rag_http_request_duration_seconds",
        "HTTP request duration in seconds, by method and route template.",
        "summary",
    )
    for (method, route), samples in sorted(http_durations.items()):
        _emit_summary(
            lines,
            "rag_http_request_duration_seconds",
            samples,
            {"method": method, "route": route},
        )

    search_stats = vector_store.search_stats_snapshot()
    _emit_help(
        lines,
        "rag_vector_searches_total",
        "Vector searches by result kind (hit/empty).",
        "counter",
    )
    for result in ("hit", "empty"):
        lines.append(
            f'rag_vector_searches_total{{result="{result}"}} '
            f"{search_stats['counts'].get(result, 0)}"
        )
    _emit_help(
        lines,
        "rag_vector_search_duration_seconds",
        "Vector search duration in seconds (retrieval stage).",
        "summary",
    )
    _emit_summary(lines, "rag_vector_search_duration_seconds", search_stats["samples"])

    trace_metrics = _query_trace_metrics()
    _emit_help(
        lines,
        "rag_query_traces",
        "Execution traces read from the recent execution-log window.",
        "gauge",
    )
    lines.append(f"rag_query_traces {trace_metrics['traces']}")
    _emit_help(
        lines,
        "rag_query_duration_seconds",
        "End-to-end query duration in seconds from execution traces, by route.",
        "summary",
    )
    for route, samples in sorted(trace_metrics["totals"].items()):
        _emit_summary(lines, "rag_query_duration_seconds", samples, {"route": route})
    _emit_help(
        lines,
        "rag_query_stage_samples",
        "Execution traces carrying a recorded duration for the stage, by route.",
        "gauge",
    )
    for key, label in _STAGE_LABELS:
        for route, samples in sorted(trace_metrics["stages"][key].items()):
            lines.append(
                f'rag_query_stage_samples{{stage="{label}",route="{route}"}} {len(samples)}'
            )
    _emit_help(
        lines,
        "rag_query_stage_duration_seconds",
        "Recorded query stage duration in seconds, by stage and route.",
        "summary",
    )
    for key, label in _STAGE_LABELS:
        for route, samples in sorted(trace_metrics["stages"][key].items()):
            _emit_summary(
                lines,
                "rag_query_stage_duration_seconds",
                samples,
                {"stage": label, "route": route},
            )

    _emit_help(
        lines,
        "rag_dependency_healthy",
        "1 when the dependency answered the readiness probe, else 0 (skipped omitted).",
        "gauge",
    )
    for component, state in _probe_dependencies().items():
        if state["status"] == "skipped":
            continue
        lines.append(
            f'rag_dependency_healthy{{component="{component}"}} '
            f"{1 if state['status'] == 'ok' else 0}"
        )

    models = _llm_pool_models()
    _emit_help(
        lines,
        "rag_llm_provider_healthy",
        "1 when the configured LLM provider is not circuit-broken.",
        "gauge",
    )
    for model in models:
        provider = _escape_label(str(model["id"]))
        healthy = 1 if model.get("healthy") else 0
        lines.append(f'rag_llm_provider_healthy{{provider="{provider}"}} {healthy}')
    _emit_help(
        lines,
        "rag_llm_inflight_requests",
        "In-flight requests in the LLM concurrency gate, by provider.",
        "gauge",
    )
    for model in models:
        provider = _escape_label(str(model["id"]))
        active = int(model.get("active_requests") or 0)
        lines.append(f'rag_llm_inflight_requests{{provider="{provider}"}} {active}')

    return "\n".join(lines) + "\n"


# ── 路由 ────────────────────────────────────────────────────────────


@router.get("/health", summary="健康检查", description="检查服务是否正常运行")
def health():
    components = {}
    try:
        qdrant_client = vector_store._get_client()
        qdrant_client.get_collections()
        components["qdrant"] = "ok"
    except Exception as e:
        logger.error("Qdrant 健康检查失败: %s", e)
        components["qdrant"] = "error"

    try:
        conn = sqlite3.connect(settings.analysis_db_path)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        components["sqlite"] = "ok"
    except Exception as e:
        logger.error("SQLite 健康检查失败: %s", e)
        components["sqlite"] = "error"

    status = "healthy" if all(v == "ok" for v in components.values()) else "degraded"
    payload = {"status": status, "components": components}
    if status == "degraded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get(
    "/ready",
    summary="依赖就绪检查",
    description=(
        "只读依赖探针：SQLite 可执行 SELECT 1、Qdrant 集合可列举、Redis 已配置时 ping 通。"
        "任一检查失败返回 503，并在 components 中给出组件级状态；不泄露异常文本。"
    ),
)
def ready():
    components = _probe_dependencies()
    payload = {
        "status": "not_ready"
        if any(state["status"] == "error" for state in components.values())
        else "ready",
        "components": components,
    }
    return JSONResponse(
        status_code=200 if payload["status"] == "ready" else 503,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/metrics",
    summary="运行指标",
    description=(
        "Prometheus 文本格式（text/plain; version=0.0.4）。只读、管理员可见；"
        "输出请求量/延迟、检索耗时、执行轨迹分段、依赖健康与 LLM 池并发，"
        "不含问题文本、文档名、请求路径或凭据。"
    ),
)
def metrics(user: dict = Depends(require_admin)) -> Response:
    return Response(
        content=_render_metrics(),
        media_type=_PROMETHEUS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def _get_gap_analyzer_instance():
    return get_gap_analyzer()


@router.get("/analytics/gaps/summary", summary="检索空白分析统计")
async def get_gap_summary(user: dict = Depends(require_admin)):
    analyzer = _get_gap_analyzer_instance()
    return await asyncio.to_thread(analyzer.get_summary)


@router.get("/analytics/gaps", summary="未解答问题列表")
async def get_gaps(limit: int = 50, user: dict = Depends(require_admin)):
    analyzer = _get_gap_analyzer_instance()
    return await asyncio.to_thread(analyzer.get_gaps, limit=limit)
