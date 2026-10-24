"""F-06/F-10: ``deploy.sh`` must verify health and refuse to report success blind.

The old script ended with ``docker compose up -d; docker compose ps``, so a container that
started but stayed ``degraded`` (Qdrant/SQLite unavailable, bad API key, restart loop) still
looked like a successful deploy.  These tests exercise the real bash functions against a
local HTTP server: no Docker daemon, no image build, no network access required.

They also pin the F-10 hardening of the same script: ``.env`` permissions and a warning
when the published port is bound to every interface.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "deploy.sh"


def _bash_executable() -> str | None:
    """Return a usable bash, or None.

    Windows ships a WSL ``bash.exe`` that cannot run this script; prefer Git Bash.
    """
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None
    found = shutil.which("bash")
    return found


BASH = _bash_executable()
requires_bash = pytest.mark.skipif(BASH is None, reason="bash is required to run deploy.sh")


@contextmanager
def _http_server(routes: dict[str, int]):
    """Serve ``routes`` (path -> status) on a random loopback port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            status = routes.get(self.path, 404)
            body = b'{"status": "test"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(ROOT)),
        "LANG": "C.UTF-8",
    }
    environment.update(env or {})
    return subprocess.run(
        [BASH, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
        check=False,
    )


def _call(function: str, *args: str, env: dict[str, str] | None = None,
          cwd: Path = ROOT) -> subprocess.CompletedProcess:
    quoted = " ".join(f"'{arg}'" for arg in args)
    script = f"set -euo pipefail\nsource '{DEPLOY_SH.as_posix()}'\n{function} {quoted}"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(ROOT)),
        "LANG": "C.UTF-8",
    }
    environment.update(env or {})
    return subprocess.run(
        [BASH, "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
        check=False,
    )


def _run_main_with_stubs(tmp_path: Path, failure: str = "", *, update_compose: bool = False):
    """Run the real main flow with only git/docker replaced by temp-directory stubs."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        'services:\n  rag-app:\n    ports:\n      - "127.0.0.1:8000:8000"\n',
        encoding="utf-8",
    )
    event_log = tmp_path / "commands.log"
    docker_stub = stub_dir / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "docker:%s\\n" "$*" >> "$DEPLOY_STUB_LOG"\n'
        'if [[ "$1 $2" == "compose version" && "$DEPLOY_FAILURE" == require ]]; then exit 9; fi\n'
        'if [[ "$1 $2" == "compose build" && "$DEPLOY_FAILURE" == build ]]; then exit 9; fi\n'
        'if [[ "$1 $2" == "compose run" && "$DEPLOY_FAILURE" == backup ]]; then exit 9; fi\n'
        'if [[ "$1 $2" == "compose up" && "$DEPLOY_FAILURE" == up ]]; then exit 9; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)

    script = (
        "set -u\n"
        f"chmod +x '{docker_stub.as_posix()}'\n"
        f"source '{DEPLOY_SH.as_posix()}'\n"
        f"project_dir='{tmp_path.as_posix()}'\n"
        "git() {\n"
        '  printf "git:%s\\n" "$*" >> "$DEPLOY_STUB_LOG"\n'
        '  if [[ "$1" == rev-parse ]]; then return 0; fi\n'
        '  if [[ "$1" == pull ]]; then\n'
        '    if [[ "$DEPLOY_UPDATE_COMPOSE" == 1 ]]; then printf \'services:\\n  rag-app:\\n    ports:\\n      - "8000:8000"\\n\' > docker-compose.yml; fi\n'
        '    [[ "$DEPLOY_FAILURE" != pull ]] || return 9\n'
        '  fi\n'
        '  return 0\n'
        "}\n"
        # Avoid touching any developer .env while leaving the real preflight check intact.
        "ensure_env_file() { printf 'ensure_env_file\\n' >> \"$DEPLOY_STUB_LOG\"; [[ \"$DEPLOY_FAILURE\" != env ]] || return 9; }\n"
        "harden_env_permissions() { printf 'harden_env_permissions\\n' >> \"$DEPLOY_STUB_LOG\"; [[ \"$DEPLOY_FAILURE\" != harden ]] || return 9; }\n"
        "detect_health_path() { printf '/ready'; }\n"
        "verify_health() { printf 'HEALTH_REACHED\\n'; return 0; }\n"
        "if ! main; then exit 1; fi\n"
    )
    environment = {
        "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "DEPLOY_STUB_LOG": str(event_log),
        "DEPLOY_FAILURE": failure,
        "DEPLOY_UPDATE_COMPOSE": "1" if update_compose else "0",
        "RAG_HEALTH_RETRIES": "1",
        "RAG_HEALTH_INTERVAL_SECONDS": "0",
    }
    result = subprocess.run(
        [BASH, "-c", script], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, env=environment, check=False,
    )
    events = event_log.read_text(encoding="utf-8").splitlines() if event_log.exists() else []
    return result, events


# ── static contract (no bash needed) ────────────────────────────────────────


def test_deploy_script_runs_a_health_gate_after_starting_the_stack():
    script = DEPLOY_SH.read_text(encoding="utf-8")
    main_body = script[script.index("main() {") :]

    assert main_body.index("start_stack") < main_body.index('verify_health "$health_path"')
    assert "RAG_HEALTH_RETRIES" in script
    assert "RAG_HEALTH_INTERVAL_SECONDS" in script


def test_deploy_script_fails_the_run_when_health_never_passes():
    script = DEPLOY_SH.read_text(encoding="utf-8")

    assert "return 1" in script
    assert "/health" in script
    assert "/ready" in script
    # A failure must be visible: non-zero exit plus container state/log tail.
    assert "docker compose ps" in script
    assert "exit 1" in script


def test_deploy_script_tightens_env_permissions():
    script = DEPLOY_SH.read_text(encoding="utf-8")

    assert "chmod 600 .env" in script
    assert "REDIS_PASSWORD" in script or "API Key" in script


def test_deploy_script_warns_about_a_publicly_bound_port():
    script = DEPLOY_SH.read_text(encoding="utf-8")

    assert "127.0.0.1" in script
    assert "端口" in script


def test_deploy_script_keeps_the_backup_gate_before_starting():
    script = DEPLOY_SH.read_text(encoding="utf-8")
    main_body = script[script.index("main() {") :]

    assert main_body.index("backup_before_up") < main_body.index("start_stack")
    assert "python scripts/backup.py create-if-due --trigger deploy" in script


def test_deploy_script_is_syntactically_valid():
    if BASH is None:
        pytest.skip("bash is required to parse deploy.sh")
    result = _run_bash("bash -n ./deploy.sh", env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 0, result.stderr


def test_deploy_script_does_not_install_anything_at_deploy_time():
    script = DEPLOY_SH.read_text(encoding="utf-8").casefold()

    assert "pip install" not in script
    assert "curl | sh" not in script
    assert "curl -fsSL" not in script


# ── behavioural: the health gate itself ────────────────────────────────────


@requires_bash
def test_health_gate_passes_when_health_returns_200():
    with _http_server({"/health": 200, "/ready": 404}) as base_url:
        result = _call(
            "verify_health",
            "/health",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "2",
                 "RAG_HEALTH_INTERVAL_SECONDS": "0"},
        )

    assert result.returncode == 0, result.stderr
    assert "健康检查通过" in result.stdout


@requires_bash
def test_health_gate_fails_when_the_service_is_degraded():
    """``/health`` answers 503 while a dependency is down: deploy must not pass."""
    with _http_server({"/health": 503, "/ready": 404}) as base_url:
        result = _call(
            "verify_health",
            "/health",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "2",
                 "RAG_HEALTH_INTERVAL_SECONDS": "0"},
        )

    assert result.returncode == 1
    assert "健康检查通过" not in result.stdout


@requires_bash
def test_health_gate_fails_when_nothing_is_listening():
    result = _call(
        "verify_health",
        "/health",
        env={"RAG_HEALTH_BASE_URL": "http://127.0.0.1:1", "RAG_HEALTH_RETRIES": "1",
             "RAG_HEALTH_INTERVAL_SECONDS": "0"},
    )

    assert result.returncode == 1


@requires_bash
def test_health_path_prefers_ready_when_the_endpoint_exists():
    with _http_server({"/ready": 200, "/health": 200}) as base_url:
        result = _call(
            "detect_health_path",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "1"},
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/ready"


@requires_bash
def test_health_path_falls_back_to_health_without_ready():
    """Until S7 lands ``/ready`` the deploy must keep using ``/health``, not fail."""
    with _http_server({"/health": 200}) as base_url:
        result = _call(
            "detect_health_path",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "1"},
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/health"


@requires_bash
def test_health_path_waits_on_ready_when_dependencies_are_not_ready():
    """A 503 from ``/ready`` means 'exists but not ready yet', so keep waiting on it."""
    with _http_server({"/ready": 503, "/health": 200}) as base_url:
        result = _call(
            "detect_health_path",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "1"},
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/ready"


@requires_bash
def test_health_gate_retries_before_giving_up():
    with _http_server({"/health": 405}) as base_url:
        result = _call(
            "verify_health",
            "/health",
            env={"RAG_HEALTH_BASE_URL": base_url, "RAG_HEALTH_RETRIES": "3",
                 "RAG_HEALTH_INTERVAL_SECONDS": "0"},
        )

    assert result.returncode == 1
    # Every attempt is reported (attempts 1..3), so an operator can see it kept trying.
    assert (result.stdout + result.stderr).count("等待") == 3


# ── behavioural: .env permissions and port binding warnings ────────────────


@requires_bash
def test_env_file_permissions_are_tightened_or_reported(tmp_path):
    (tmp_path / ".env").write_text("REDIS_PASSWORD=secret\n", encoding="utf-8")
    (tmp_path / ".env").chmod(0o644)

    result = _call("harden_env_permissions", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "600" in combined or "警告" in combined
    if os.name != "nt":
        # Git Bash on Windows cannot express POSIX modes; the real deployment is Linux.
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


@requires_bash
def test_public_port_binding_is_reported_as_a_warning(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        'services:\n  rag-app:\n    ports:\n      - "8000:8000"\n', encoding="utf-8"
    )

    result = _call("check_port_binding", str(compose), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "警告" in result.stderr
    assert "127.0.0.1" in result.stderr


@requires_bash
def test_loopback_port_binding_is_not_warned_about(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        'services:\n  rag-app:\n    ports:\n      - "127.0.0.1:8000:8000"\n', encoding="utf-8"
    )

    result = _call("check_port_binding", str(compose), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "警告" not in result.stderr


@requires_bash
def test_missing_compose_file_is_skipped_without_failing(tmp_path):
    result = _call("check_port_binding", str(tmp_path / "absent.yml"), cwd=tmp_path)

    assert result.returncode == 0, result.stderr


# ── behavioural: the complete main flow under its real if-not-main context ──


@requires_bash
def test_main_checks_the_binding_after_pull_updates_compose(tmp_path):
    result, events = _run_main_with_stubs(tmp_path, update_compose=True)

    assert result.returncode == 0, result.stderr
    # The public mapping itself is ASCII; the warning stays non-blocking even though
    # Git Bash on Windows may render its Chinese text in the active console code page.
    assert '"8000:8000"' in result.stderr
    assert '"127.0.0.1:8000:8000"' not in result.stdout
    deployment_order = [
        "docker:compose version",
        "ensure_env_file",
        "harden_env_permissions",
        "git:rev-parse --is-inside-work-tree",
        "git:pull --ff-only",
        "docker:compose build rag-app",
        "docker:compose run --rm --no-deps rag-app python scripts/backup.py create-if-due --trigger deploy",
        "docker:compose up -d",
        "docker:compose ps",
    ]
    assert [events.index(event) for event in deployment_order] == sorted(
        events.index(event) for event in deployment_order
    )
    assert result.stdout.index("HEALTH_REACHED") < result.stdout.index("部署完成")
    assert "部署完成" in result.stdout


@requires_bash
@pytest.mark.parametrize(
    ("failure", "forbidden_events"),
    [
        ("require", ["ensure_env_file", "git:rev-parse", "docker:compose build rag-app"]),
        ("env", ["harden_env_permissions", "git:rev-parse", "docker:compose build rag-app"]),
        ("harden", ["git:rev-parse", "docker:compose build rag-app"]),
        ("pull", ["docker:compose build rag-app"]),
        ("build", ["docker:compose run --rm --no-deps rag-app python scripts/backup.py create-if-due --trigger deploy"]),
        ("backup", ["docker:compose up -d"]),
        ("up", ["docker:compose ps"]),
    ],
)
def test_main_stops_after_a_failed_deployment_stage(tmp_path, failure, forbidden_events):
    result, events = _run_main_with_stubs(tmp_path, failure)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "部署完成" not in result.stdout
    assert "HEALTH_REACHED" not in result.stdout
    assert all(event not in events for event in forbidden_events)
