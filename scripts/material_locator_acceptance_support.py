"""Isolation, freeze, hashing, and instrumentation helpers for locator runs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "artifacts" / "material-locator-acceptance"
BASELINE = ROOT / "tests" / "fixtures" / "material_locator_542a36f.py"
BASELINE_LF_SHA256 = "54593297c81a31000dee9c26336db3198bc85e3e334f001cddb7f33766434bfc"
FROZEN_TOOL_FILES = (
    "scripts/material_locator_resources.py",
    "scripts/material_locator_acceptance_support.py",
    "scripts/paired_material_locator_acceptance.py",
    "tests/test_material_locator_resources.py",
    "tests/test_material_locator_acceptance_tool.py",
)
SOURCE_DIRS = ("rag", "tests", "scripts", "config")
SOURCE_FILES = ("config.py", "pyproject.toml", "requirements.txt", "requirements.lock", "uv.lock", "poetry.lock")
REQUIRED_SOURCE_FILES = (
    "rag/repositories/material_repo.py",
    "rag/materials/locator_visibility.py",
    "rag/materials/locator.py",
    "tests/test_material_locator_overhead.py",
    "tests/test_material_locator_equivalence.py",
    "tests/test_material_visibility_batch_plan.py",
    "tests/test_material_visibility_projection.py",
    "pyproject.toml",
)
TEXT_SUFFIXES = {".py", ".pyi", ".sql", ".toml", ".txt", ".ini", ".cfg", ".json", ".yaml", ".yml", ".md"}
COUNTER_METHODS = (
    "list_material_documents_with_current_version",
    "list_material_document_kb_ids",
    "list_material_documents_for_kb",
    "list_material_documents_for_access_plan",
)
COUNTER_PHASES = ("diagnostic_probe", "semantic_probe", "warmup", "timed_measurement", "functional")


class AcceptanceError(RuntimeError):
    """A reproducibility or safety precondition failed."""

    def __init__(
        self, message: str, *, code: str = "acceptance_preflight_failed", details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_digest(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    return sha256_bytes(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def source_hashes(path: Path) -> dict[str, str | None]:
    raw = path.read_bytes()
    lf_hash = None
    if path.suffix.lower() in TEXT_SUFFIXES:
        text = raw.decode("utf-8")
        lf_hash = sha256_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    return {"raw_sha256": sha256_bytes(raw), "lf_sha256": lf_hash}


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceError(
            "Git source-freeze query failed", code="git_unavailable", details={"args": list(args), "error": str(exc)}
        ) from exc
    return result.stdout.strip()


def _relevant_status(root: Path) -> list[str]:
    paths = [*SOURCE_DIRS, *SOURCE_FILES, "*.lock", "requirements*.txt", ".python-version", "Pipfile", "Pipfile.lock"]
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
    return [line for line in status.splitlines() if line.strip()]


def _frozen_paths(root: Path) -> list[Path]:
    paths = set()
    for relative in SOURCE_DIRS:
        absolute = root / relative
        if absolute.exists():
            listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "--", relative)
            paths.update(line.strip() for line in listed.splitlines() if line.strip())
    for relative in SOURCE_FILES:
        if (root / relative).is_file():
            paths.add(relative)
    paths.update(REQUIRED_SOURCE_FILES)
    for candidate in root.iterdir():
        if candidate.is_file() and (candidate.name.endswith(".lock") or candidate.name.startswith("requirements")):
            paths.add(candidate.relative_to(root).as_posix())
    paths.update(FROZEN_TOOL_FILES)
    paths.add("tests/fixtures/material_locator_542a36f.py")
    missing = [relative for relative in sorted(paths) if not (root / relative).is_file()]
    if missing:
        raise AcceptanceError("freeze source files are missing", code="freeze_file_missing", details={"paths": missing})
    return [root / relative for relative in sorted(paths)]


def create_source_freeze(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    status = _relevant_status(root)
    if status:
        raise AcceptanceError(
            "source roots contain modified or untracked files", code="source_tree_dirty", details={"status": status}
        )
    commit = _git(root, "rev-parse", "HEAD")
    entries = []
    for path in _frozen_paths(root):
        relative = path.relative_to(root).as_posix()
        entries.append({"path": relative, **source_hashes(path)})
    baseline = next((entry for entry in entries if entry["path"] == "tests/fixtures/material_locator_542a36f.py"), None)
    if baseline is None or baseline["lf_sha256"] != BASELINE_LF_SHA256:
        raise AcceptanceError(
            "baseline fixture LF hash mismatch", code="baseline_hash_mismatch", details={"actual": baseline}
        )
    payload = {
        "format_version": 1,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository_root": str(root),
        "commit": commit,
        "source_roots": list(SOURCE_DIRS),
        "source_files": list(SOURCE_FILES),
        "hash_policy": "raw SHA256 for every file; UTF-8 text additionally has LF-normalized SHA256",
        "baseline_fixture_lf_sha256": BASELINE_LF_SHA256,
        "status_clean": True,
        "files": entries,
    }
    payload["manifest_sha256"] = _manifest_digest(payload)
    return payload


def verify_source_freeze(root: Path, manifest_path: Path) -> dict[str, Any]:
    root = root.resolve()
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(
            "freeze manifest cannot be read",
            code="freeze_manifest_unreadable",
            details={"path": str(manifest_path), "error": str(exc)},
        ) from exc
    if expected.get("format_version") != 1:
        raise AcceptanceError("unsupported freeze manifest version", code="freeze_manifest_version")
    if expected.get("manifest_sha256") != _manifest_digest(expected):
        raise AcceptanceError("freeze manifest digest mismatch", code="freeze_manifest_digest_mismatch")
    if Path(expected.get("repository_root", "")).resolve() != root:
        raise AcceptanceError("freeze manifest belongs to another repository root", code="freeze_root_mismatch")
    status = _relevant_status(root)
    if status:
        raise AcceptanceError("source roots changed after freeze", code="source_tree_dirty", details={"status": status})
    commit = _git(root, "rev-parse", "HEAD")
    if expected.get("commit") != commit:
        raise AcceptanceError(
            "repository commit differs from freeze",
            code="freeze_commit_mismatch",
            details={"expected": expected.get("commit"), "actual": commit},
        )
    actual_entries = [
        {"path": path.relative_to(root).as_posix(), **source_hashes(path)} for path in _frozen_paths(root)
    ]
    if expected.get("files") != actual_entries:
        expected_by_path = {entry.get("path"): entry for entry in expected.get("files", [])}
        actual_by_path = {entry["path"]: entry for entry in actual_entries}
        differences = sorted(
            path
            for path in set(expected_by_path) | set(actual_by_path)
            if expected_by_path.get(path) != actual_by_path.get(path)
        )
        raise AcceptanceError(
            "source hash manifest differs", code="freeze_hash_mismatch", details={"paths": differences}
        )
    return {
        "verified": True,
        "commit": commit,
        "file_count": len(actual_entries),
        "manifest_sha256": expected["manifest_sha256"],
    }


def validate_output_path(path: Path, root: Path = ROOT, *, must_not_exist: bool = True) -> Path:
    absolute = path.resolve()
    allowed = (root / "artifacts" / "material-locator-acceptance").resolve()
    try:
        absolute.relative_to(allowed)
    except ValueError as exc:
        raise AcceptanceError(
            "output must be inside artifacts/material-locator-acceptance",
            code="output_path_forbidden",
            details={"path": str(absolute)},
        ) from exc
    if must_not_exist and absolute.exists():
        raise AcceptanceError(
            "refusing to overwrite existing output", code="output_exists", details={"path": str(absolute)}
        )
    return absolute


def write_json_new(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise AcceptanceError(
            "refusing to overwrite existing output", code="output_exists", details={"path": str(path)}
        ) from exc


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def sanitize_process_environment(run_dir: Path) -> dict[str, Any]:
    """Drop inherited configuration/secrets and bind user caches to this run."""
    essential = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "VIRTUAL_ENV")
    retained = {key: os.environ[key] for key in essential if key in os.environ}
    os.environ.clear()
    os.environ.update(retained)
    os.environ["PYTHONPATH"] = str(ROOT)
    home = run_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        os.environ[key] = str(home if key in {"HOME", "USERPROFILE"} else home / key.lower())
        if key not in {"HOME", "USERPROFILE"}:
            Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    temp_dir = run_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(temp_dir)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    return {
        "environment_allowlist": sorted(os.environ),
        "home": str(home),
        "secrets_retained": False,
        "blas_threads": "1",
        "temporary_directory": str(temp_dir),
    }


def install_offline_network_guard() -> dict[str, Any]:
    """Reject DNS and all IP socket connection attempts, including loopback."""
    if getattr(socket, "_material_locator_guard_installed", False):
        return {"installed": True, "policy": "no_dns_or_ip_connect"}
    sendmsg_supported = hasattr(socket.socket, "sendmsg")

    def blocked(_sock: socket.socket, address: Any) -> Any:
        raise OSError("acceptance harness blocks all network connections")

    def blocked_create_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("acceptance harness blocks all network connections")

    def blocked_getaddrinfo(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("acceptance harness blocks DNS resolution")

    def blocked_name_lookup(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("acceptance harness blocks DNS resolution")

    def blocked_datagram(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("acceptance harness blocks UDP network traffic")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.socket.sendto = blocked_datagram
    if sendmsg_supported:
        socket.socket.sendmsg = blocked_datagram
    socket.create_connection = blocked_create_connection
    socket.getaddrinfo = blocked_getaddrinfo
    socket.gethostbyname = blocked_name_lookup
    socket.gethostbyname_ex = blocked_name_lookup
    socket.gethostbyaddr = blocked_name_lookup
    socket.getnameinfo = blocked_name_lookup
    socket._material_locator_guard_installed = True  # type: ignore[attr-defined]
    return {
        "installed": True,
        "policy": "no_dns_or_ip_connect",
        "patched": [
            "socket.connect",
            "socket.connect_ex",
            "socket.sendto",
            "socket.sendmsg",
            "socket.create_connection",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.gethostbyaddr",
            "socket.getnameinfo",
        ],
    }


def disable_dotenv_loading() -> None:
    try:
        from pydantic_settings import BaseSettings
    except ImportError as exc:
        raise AcceptanceError("pydantic-settings is unavailable", code="dotenv_guard_unavailable") from exc

    def environment_only(
        cls: Any,
        settings_cls: Any,
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        del cls, settings_cls, dotenv_settings, file_secret_settings
        return init_settings, env_settings

    BaseSettings.settings_customise_sources = classmethod(environment_only)


def install_sqlite_path_audit(run_root: Path) -> dict[str, Any]:
    allowed_root = run_root.resolve()
    original = sqlite3.connect
    opened: list[str] = []
    lock = threading.Lock()

    def audited(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        value = os.fspath(database) if isinstance(database, os.PathLike) else str(database)
        if value == ":memory:":
            resolved = value
        elif value.startswith("file:"):
            raise AcceptanceError(
                "SQLite URI connections are disabled", code="sqlite_uri_forbidden", details={"database": value}
            )
        else:
            candidate = Path(value).resolve()
            try:
                candidate.relative_to(allowed_root)
            except ValueError as exc:
                raise AcceptanceError(
                    "SQLite path is outside this run", code="sqlite_path_forbidden", details={"path": str(candidate)}
                ) from exc
            resolved = str(candidate)
        with lock:
            opened.append(resolved)
        return original(database, *args, **kwargs)

    sqlite3.connect = audited  # type: ignore[assignment]
    sqlite3.dbapi2.connect = audited  # type: ignore[assignment]
    return {"allowed_root": str(allowed_root), "installed_before_rag_import": True, "opened_paths": opened}


def configure_isolated_context(run_dir: Path) -> tuple[Any, dict[str, str]]:
    context = run_dir / "context"
    context.mkdir(parents=True, exist_ok=False)
    paths = {
        "RAG_USERS_DB_PATH": context / "users.sqlite3",
        "RAG_MEMORY_DB_PATH": context / "memory.sqlite3",
        "RAG_ANALYSIS_DB_PATH": context / "analysis.sqlite3",
        "RAG_WIKI_DB_PATH": context / "wiki.sqlite3",
        "RAG_BM25_DB_PATH": context / "bm25.sqlite3",
        "RAG_EVOLUTION_DB_PATH": context / "evolution.sqlite3",
        "RAG_QDRANT_PATH": context / "qdrant",
        "RAG_DOCUMENT_VERSIONS_ROOT": context / "document_versions",
        "RAG_STRUCTURED_DATASETS_ROOT": context / "structured_datasets",
    }
    for key, path in paths.items():
        os.environ[key] = str(path)
    disable_dotenv_loading()
    import config

    if Path(config.settings.users_db_path).resolve() != paths["RAG_USERS_DB_PATH"].resolve():
        raise AcceptanceError("synthetic users database path was not honored", code="context_path_mismatch")
    import rag.context as rag_context

    if rag_context._DB_PATH.resolve() != paths["RAG_USERS_DB_PATH"].resolve():
        raise AcceptanceError("RAG context opened a non-isolated database path", code="context_path_mismatch")
    return rag_context, {key: str(value) for key, value in paths.items()}


def logical_database_hash(db: Any) -> dict[str, Any]:
    tables = [
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    digest = hashlib.sha256()
    table_rows: dict[str, int] = {}
    for table in tables:
        escaped_table = table.replace('"', '""')
        columns = [row[1] for row in db._conn.execute(f'PRAGMA table_info("{escaped_table}")')]
        if not columns:
            continue
        selected = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
        rows = db._conn.execute(f'SELECT {selected} FROM "{escaped_table}" ORDER BY {selected}').fetchall()
        table_rows[table] = len(rows)
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        for row in rows:
            digest.update(
                json.dumps(tuple(row), ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")
    return {
        "algorithm": "sha256-json-rows-v1",
        "sha256": digest.hexdigest(),
        "tables": table_rows,
        "row_count": sum(table_rows.values()),
    }


def new_counter_store() -> dict[str, Any]:
    lock = threading.Lock()
    counts: dict[str, Any] = {}
    for implementation in ("old", "new"):
        counts[implementation] = {}
        for method in COUNTER_METHODS:
            row_kind = "kb_id_rows" if method == "list_material_document_kb_ids" else "document_rows"
            counts[implementation][method] = {
                "row_kind": row_kind,
                **{phase: {"calls": 0, "returned_list_rows": 0} for phase in COUNTER_PHASES},
            }
    return {
        "lock": lock,
        "counts": counts,
        "sql": {"old": [], "new": []},
        "sql_by_phase": {implementation: {phase: [] for phase in COUNTER_PHASES} for implementation in ("old", "new")},
        "query_counts": {
            "old": {"all": 0, "document_projection": 0, "kb_id_projection": 0, "access_plan_batch": 0},
            "new": {"all": 0, "document_projection": 0, "kb_id_projection": 0, "access_plan_batch": 0},
        },
        "fetched_rows": {"old": {"document_rows": 0, "kb_id_rows": 0}, "new": {"document_rows": 0, "kb_id_rows": 0}},
        "lock_guard": threading.Lock(),
    }


def install_repository_counters(repo: Any, store: dict[str, Any], implementation: str, active_label: Any) -> list[str]:
    installed: list[str] = []
    for name in COUNTER_METHODS:
        original = getattr(repo, name, None)
        if original is None:
            continue

        def wrapped(*args: Any, _original: Any = original, _name: str = name, **kwargs: Any) -> Any:
            result = _original(*args, **kwargs)
            phase = getattr(active_label, "phase", None)
            label = getattr(active_label, "name", None)
            if phase and label == implementation:
                metric = store["counts"][implementation][_name][phase]
                with store["lock"]:
                    metric["calls"] += 1
                    if isinstance(result, list):
                        metric["returned_list_rows"] += len(result)
            return result

        setattr(repo, name, wrapped)
        installed.append(name)
    return installed


def repository_counter_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    with store["lock"]:
        return json.loads(json.dumps(store["counts"]))


def database_counter_snapshot(store: dict[str, Any]) -> dict[str, Any]:
    with store["lock_guard"]:
        return {
            "query_counts": json.loads(json.dumps(store["query_counts"])),
            "fetched_rows": json.loads(json.dumps(store["fetched_rows"])),
        }


def append_sql_trace(store: dict[str, Any], active_label: Any, statement: str) -> None:
    name = getattr(active_label, "name", None)
    if name in store["sql"]:
        with store["lock_guard"]:
            store["sql"][name].append(statement)
            phase = getattr(active_label, "phase", None)
            if phase in store["sql_by_phase"][name]:
                store["sql_by_phase"][name][phase].append(statement)


class _CursorCounter:
    def __init__(self, cursor: Any, statement: str, store: dict[str, Any], active_label: Any) -> None:
        self._cursor = cursor
        self._statement = statement
        self._store = store
        self._active_label = active_label

    def _record(self, rows: list[Any]) -> None:
        name = getattr(self._active_label, "name", None)
        if name not in self._store["fetched_rows"]:
            return
        upper = self._statement.upper()
        if "DOCUMENT_PERMISSIONS DP" not in upper:
            return
        if "SELECT DP.KB_ID" in upper:
            kind, query = "kb_id_rows", "kb_id_projection"
        elif "SELECT DP.ID, DP.DOC_NAME" in upper:
            kind, query = "document_rows", "document_projection"
        else:
            return
        with self._store["lock_guard"]:
            self._store["fetched_rows"][name][kind] += len(rows)
            self._store["query_counts"][name][query] += 1
            if "WITH ACCESS_PLAN" in upper:
                self._store["query_counts"][name]["access_plan_batch"] += 1

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall()
        self._record(rows)
        return rows

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        if row is not None:
            self._record([row])
        return row

    def __iter__(self) -> Any:
        for row in self._cursor:
            self._record([row])
            yield row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionCounter:
    def __init__(self, connection: Any, store: dict[str, Any], active_label: Any) -> None:
        self._connection = connection
        self._store = store
        self._active_label = active_label

    def execute(self, statement: str, parameters: Any = ()) -> _CursorCounter:
        name = getattr(self._active_label, "name", None)
        if name in self._store["query_counts"]:
            with self._store["lock_guard"]:
                self._store["query_counts"][name]["all"] += 1
        return _CursorCounter(
            self._connection.execute(statement, parameters), statement, self._store, self._active_label
        )

    def executemany(self, statement: str, parameters: Any) -> _CursorCounter:
        name = getattr(self._active_label, "name", None)
        if name in self._store["query_counts"]:
            with self._store["lock_guard"]:
                self._store["query_counts"][name]["all"] += 1
        return _CursorCounter(
            self._connection.executemany(statement, parameters), statement, self._store, self._active_label
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def semantic_probe(locator_cls: Any, db: Any, query: Any, user: Any) -> dict[str, Any]:
    """Capture deterministic semantic call parameters without a model service."""
    calls: list[dict[str, Any]] = []

    def probe(text: str, allowed_version_ids: Any, top_k: int) -> list[Any]:
        calls.append({"text": text, "allowed_version_ids": sorted(allowed_version_ids), "top_k": top_k})
        return []

    locator = locator_cls(db, semantic_retriever=probe)
    rows = locator.locate(query, user)
    return {
        "calls": calls,
        "result": [
            [
                row.document_id,
                row.document_version_id,
                row.filename,
                row.logical_path,
                row.document_type,
                row.updated_at,
                row.ocr_warning,
                row.citation.location.kind,
                row.citation.location.value,
                row.rank,
                row.source,
            ]
            for row in rows
        ],
        "model_input": locator.model_input,
    }


def load_frozen_baseline(path: Path = BASELINE, expected_lf_sha: str = BASELINE_LF_SHA256) -> Any:
    if source_hashes(path)["lf_sha256"] != expected_lf_sha:
        raise AcceptanceError(
            "frozen baseline source hash mismatch", code="baseline_hash_mismatch", details={"path": str(path)}
        )
    spec = importlib.util.spec_from_file_location("material_locator_542a36f", path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("cannot load frozen baseline source", code="baseline_load_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MaterialLocator


def write_json_progress(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)
