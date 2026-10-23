"""Phase 0: readiness (``/ready``) and Prometheus metrics (``/metrics``) contracts.

The two endpoints answer different questions:

* ``/health`` keeps its existing contract (process + dependency snapshot used by the
  container healthcheck);
* ``/ready`` is the strict dependency gate a load balancer may call without
  credentials: 200 when SQLite/Qdrant (and Redis, when configured) answer, 503 with
  per-component status otherwise;
* ``/metrics`` is admin-only, read-only and exposes aggregate counters/latency
  percentiles only — never question text, document names, credentials or route ids.
"""

import re
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import redis
from fastapi.testclient import TestClient

import rag.vector_store as vector_store
from config import settings
from rag.api import app
from rag.routers import system as system_router
from rag.tracker import ExecutionTrace, ExecutionTracker

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


@pytest.fixture(autouse=True)
def _reset_metric_state():
    system_router.reset_http_metrics()
    vector_store.reset_search_stats()
    yield
    system_router.reset_http_metrics()
    vector_store.reset_search_stats()


@pytest.fixture
def client():
    return TestClient(app)


def _sqlite_ready(monkeypatch, tmp_path):
    db = tmp_path / "users.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(settings, "users_db_path", str(db))


def _qdrant_ready(monkeypatch):
    qdrant = MagicMock()
    qdrant.get_collections.return_value = MagicMock(collections=[])
    monkeypatch.setattr(vector_store, "_get_client", lambda: qdrant)
    monkeypatch.setattr(settings, "qdrant_url", "")


def _no_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")


def _parse_samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Strictly parse the Prometheus text exposition format used by ``/metrics``."""
    samples = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        assert match, f"invalid Prometheus exposition line: {line!r}"
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        samples.append((match.group("name"), labels, float(match.group("value"))))
    return samples


def _value(samples, name: str, **labels):
    matches = [
        value
        for sample_name, sample_labels, value in samples
        if sample_name == name and all(sample_labels.get(k) == v for k, v in labels.items())
    ]
    assert matches, f"metric {name} with labels {labels} not found in {samples}"
    assert len(matches) == 1, f"metric {name} with labels {labels} is not unique: {matches}"
    return matches[0]


# ── /ready ──────────────────────────────────────────────────────────


def test_ready_returns_200_when_dependencies_are_reachable(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["sqlite"] == {"status": "ok"}
    assert body["components"]["qdrant"] == {"status": "ok", "mode": "local"}
    assert body["components"]["redis"] == {"status": "skipped", "detail": "not_configured"}


def test_ready_reports_remote_mode_when_qdrant_url_is_set(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["components"]["qdrant"] == {"status": "ok", "mode": "remote"}


def test_ready_returns_503_when_sqlite_is_unavailable(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "users_db_path", str(tmp_path / "missing-dir" / "users.db")
    )
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"]["sqlite"] == {
        "status": "error",
        "detail": "sqlite_unavailable",
    }


def test_ready_sqlite_probe_does_not_create_a_missing_database(client, monkeypatch, tmp_path):
    """只读探针不能在 GET 上顺手创建一个空库。"""
    target = tmp_path / "missing-dir" / "users.db"
    monkeypatch.setattr(settings, "users_db_path", str(target))
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    assert client.get("/ready").status_code == 503

    assert not target.exists()


def test_ready_returns_503_when_qdrant_is_unavailable_without_leaking_details(
    client, monkeypatch, tmp_path
):
    _sqlite_ready(monkeypatch, tmp_path)
    _no_redis(monkeypatch)
    monkeypatch.setattr(
        vector_store, "_get_client", MagicMock(side_effect=RuntimeError("secret path /data"))
    )

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["components"]["qdrant"] == {
        "status": "error",
        "detail": "qdrant_unavailable",
    }
    assert "secret path" not in response.text


def test_ready_checks_redis_only_when_configured(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "redis://redis:6379/0")
    fake = MagicMock()
    fake.ping.return_value = True
    calls = {}

    def _from_url(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(redis, "from_url", _from_url)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["components"]["redis"] == {"status": "ok"}
    assert calls["url"] == "redis://redis:6379/0"
    assert calls["kwargs"]["socket_connect_timeout"] <= 2.0
    assert calls["kwargs"]["socket_timeout"] <= 2.0
    fake.close.assert_called_once()


def test_ready_keeps_redis_optional_when_configured_but_down(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "redis://redis:6379/0")
    monkeypatch.setattr(settings, "redis_required", False)
    fake = MagicMock()
    fake.ping.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: fake)

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["redis"] == {"status": "degraded", "detail": "redis_unavailable"}
    assert "redis down" not in response.text


def test_ready_returns_503_when_redis_is_required_and_down(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "redis://redis:6379/0")
    monkeypatch.setattr(settings, "redis_required", True)
    fake = MagicMock()
    fake.ping.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: fake)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"]["redis"] == {"status": "error", "detail": "redis_unavailable"}
    assert "redis down" not in response.text


def test_health_keeps_its_contract_and_does_not_probe_redis(client, monkeypatch, tmp_path):
    """/health stays the container healthcheck contract; Redis is not a liveness input."""
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "components": {"qdrant": "ok", "sqlite": "ok"}}


def test_ready_is_public_for_load_balancer_probes(isolated_api, monkeypatch):
    _no_redis(monkeypatch)
    response = isolated_api.get("/ready")
    assert response.status_code in {200, 503}


# ── /metrics: authentication and format ─────────────────────────────


def test_metrics_requires_an_admin_principal(isolated_api, make_user, monkeypatch):
    _no_redis(monkeypatch)

    assert isolated_api.get("/metrics").status_code == 401

    _, user_headers = make_user("metrics-plain-user")
    assert isolated_api.get("/metrics", headers=user_headers).status_code == 403

    _, admin_headers = make_user("metrics-admin-user", is_admin=True)
    response = isolated_api.get("/metrics", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert _value(_parse_samples(response.text), "rag_up") == 1


def test_metrics_returns_strict_prometheus_text_with_http_latency(
    client, monkeypatch, tmp_path
):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    assert client.get("/health").status_code == 200
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith("# HELP rag_up ")
    samples = _parse_samples(response.text)
    assert _value(samples, "rag_http_requests_total", method="GET", route="/health", status="200") == 1
    assert _value(
        samples, "rag_http_request_duration_seconds_count", method="GET", route="/health"
    ) == 1
    for quantile in ("0.5", "0.95", "0.99"):
        assert _value(
            samples,
            "rag_http_request_duration_seconds",
            method="GET",
            route="/health",
            quantile=quantile,
        ) >= 0.0
    # 抓取自己不计入自己的指标，避免自噪声。
    assert not any(labels.get("route") == "/metrics" for _, labels, _ in samples)


def test_metrics_does_not_expose_paths_or_credentials(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    client.get("/definitely-not-a-route/secret-document-name.txt")
    text = client.get("/metrics").text

    assert "secret-document-name" not in text
    if settings.deepseek_api_key:
        assert settings.deepseek_api_key not in text
    assert 'route="unmatched"' in text


def test_metrics_reports_dependency_health_gauges(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)

    samples = _parse_samples(client.get("/metrics").text)

    assert _value(samples, "rag_dependency_healthy", component="sqlite") == 1
    assert _value(samples, "rag_dependency_healthy", component="qdrant") == 1
    assert not any(
        name == "rag_dependency_healthy" and labels.get("component") == "redis"
        for name, labels, _ in samples
    )


# ── /metrics: stage data ────────────────────────────────────────────


def test_metrics_reports_vector_search_stage(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _no_redis(monkeypatch)
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    point = MagicMock()
    point.payload = {"text": "content", "doc_name": "a.txt", "chunk_index": 0}
    mock_client.query_points.return_value = MagicMock(points=[point])

    with patch("rag.vector_store.QdrantClient", return_value=mock_client):
        results = vector_store.search_collection("rag_docs", [0.1] * settings.embed_dimension)

    assert len(results) == 1
    samples = _parse_samples(client.get("/metrics").text)
    assert _value(samples, "rag_vector_searches_total", result="hit") == 1
    assert _value(samples, "rag_vector_search_duration_seconds_count") == 1
    assert _value(samples, "rag_vector_search_duration_seconds", quantile="0.5") >= 0.0


def test_metrics_reports_query_stage_percentiles_from_execution_logs(
    client, monkeypatch, tmp_path
):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    db = tmp_path / "memory.db"
    tracker = ExecutionTracker(str(db))
    try:
        for total_ms, preparation_ms in ((100.0, 10.0), (200.0, 20.0), (300.0, 30.0), (400.0, 40.0)):
            tracker.save(
                ExecutionTrace(
                    question="q",
                    route="rag",
                    total_ms=total_ms,
                    stage_timings={"preparation_ms": preparation_ms},
                )
            )
    finally:
        tracker.close()
    monkeypatch.setattr(settings, "memory_db_path", str(db))

    samples = _parse_samples(client.get("/metrics").text)

    assert _value(samples, "rag_query_traces") == 4
    assert _value(
        samples, "rag_query_duration_seconds", route="rag", quantile="0.5"
    ) == pytest.approx(0.2)
    assert _value(
        samples, "rag_query_duration_seconds", route="rag", quantile="0.99"
    ) == pytest.approx(0.4)
    assert _value(samples, "rag_query_stage_samples", stage="preparation", route="rag") == 4
    assert _value(
        samples,
        "rag_query_stage_duration_seconds",
        stage="preparation",
        route="rag",
        quantile="0.5",
    ) == pytest.approx(0.02)


def test_metrics_does_not_mix_percentiles_across_routes(client, monkeypatch, tmp_path):
    """rag/fixed/error 的耗时不能混在一个分位数里比。"""
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    db = tmp_path / "memory.db"
    tracker = ExecutionTracker(str(db))
    try:
        for total_ms in (100.0, 200.0, 300.0, 400.0):
            tracker.save(ExecutionTrace(question="q", route="rag", total_ms=total_ms))
        for total_ms in (1000.0, 2000.0):
            tracker.save(ExecutionTrace(question="q", route="fixed", total_ms=total_ms))
        tracker.save(ExecutionTrace(question="q", route="mystery", total_ms=3000.0))
    finally:
        tracker.close()
    monkeypatch.setattr(settings, "memory_db_path", str(db))

    samples = _parse_samples(client.get("/metrics").text)

    assert _value(
        samples, "rag_query_duration_seconds", route="rag", quantile="0.5"
    ) == pytest.approx(0.2)
    assert _value(
        samples, "rag_query_duration_seconds", route="fixed", quantile="0.5"
    ) == pytest.approx(1.0)
    assert _value(
        samples, "rag_query_duration_seconds_count", route="rag"
    ) == 4
    assert _value(
        samples, "rag_query_duration_seconds_count", route="fixed"
    ) == 2
    # 未登记的 route 归入 other，标签基数有界。
    assert _value(
        samples, "rag_query_duration_seconds_count", route="other"
    ) == 1
    assert "mystery" not in client.get("/metrics").text


def test_metrics_stays_available_without_execution_logs(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    monkeypatch.setattr(settings, "memory_db_path", str(tmp_path / "absent" / "memory.db"))

    response = client.get("/metrics")

    assert response.status_code == 200
    assert _value(_parse_samples(response.text), "rag_query_traces") == 0


def test_metrics_reports_llm_pool_load_without_credentials(client, monkeypatch, tmp_path):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    fake_router = MagicMock()
    fake_router.list_models.return_value = [
        {"id": "deepseek", "name": "DeepSeek", "healthy": True, "active_requests": 2}
    ]
    monkeypatch.setattr(system_router, "get_llm_pool_router", lambda: fake_router)

    text = client.get("/metrics").text
    samples = _parse_samples(text)

    assert _value(samples, "rag_llm_provider_healthy", provider="deepseek") == 1
    assert _value(samples, "rag_llm_inflight_requests", provider="deepseek") == 2
    assert "DeepSeek" not in text


def test_ready_and_metrics_routes_are_get_only():
    for route in app.routes:
        if getattr(route, "path", "") in {"/ready", "/metrics"}:
            assert route.methods == {"GET"}


def test_chart_download_is_not_anonymous_static_content(client):
    response = client.get("/data/charts/example.png")
    # Auth-disabled test client is a development principal; the point is that a
    # missing/foreign chart is never served by an anonymous StaticFiles mount.
    assert response.status_code in {401, 404}

def test_metrics_reports_direct_rerank_and_generation_stages_from_sqlite(
    client, monkeypatch, tmp_path
):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    db = tmp_path / "direct-stages.db"
    tracker = ExecutionTracker(str(db))
    try:
        tracker.save(
            ExecutionTrace(
                question="private question",
                route="rag",
                stage_timings={"rerank_ms": 12.0, "generation_ms": 45.0},
            )
        )
    finally:
        tracker.close()
    monkeypatch.setattr(settings, "memory_db_path", str(db))

    response = client.get("/metrics")
    samples = _parse_samples(response.text)
    assert _value(samples, "rag_query_stage_samples", stage="rerank", route="rag") == 1
    assert _value(samples, "rag_query_stage_samples", stage="generation", route="rag") == 1
    assert _value(
        samples, "rag_query_stage_duration_seconds", stage="rerank", route="rag", quantile="0.5"
    ) == pytest.approx(0.012)
    assert _value(
        samples, "rag_query_stage_duration_seconds", stage="generation", route="rag", quantile="0.5"
    ) == pytest.approx(0.045)
    assert "private question" not in response.text
    assert "request_id" not in response.text
    assert "kb_id" not in response.text

def test_metrics_ignores_malformed_and_invalid_historical_stage_values(
    client, monkeypatch, tmp_path
):
    _sqlite_ready(monkeypatch, tmp_path)
    _qdrant_ready(monkeypatch)
    _no_redis(monkeypatch)
    db = tmp_path / "historical-stages.db"
    tracker = ExecutionTracker(str(db))
    tracker.close()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO execution_logs (timestamp, question, route, answer, total_ms, details) VALUES (?, ?, ?, ?, ?, ?)",
        ("t", "q", "rag", "", 1.0, '{"stage_timings":{"preparation_ms":2,"jev_preflight_ms":true,"rerank_ms":-1,"generation_ms":1e999,"unknown_ms":8}}'),
    )
    conn.execute(
        "INSERT INTO execution_logs (timestamp, question, route, answer, total_ms, details) VALUES (?, ?, ?, ?, ?, ?)",
        ("t", "q", "rag", "", 1.0, "not json"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "memory_db_path", str(db))

    samples = _parse_samples(client.get("/metrics").text)

    assert _value(samples, "rag_query_stage_samples", stage="preparation", route="rag") == 1
    assert not any(
        name == "rag_query_stage_samples" and labels.get("stage") in {"rerank", "generation"}
        for name, labels, _value_ in samples
    )
