"""F-10: operational scripts must not run unknown code or write production state by accident.

Two scripts are pinned here:

* ``scripts/run_stress_test.sh`` used to ``pip install locust`` on demand (whatever version
  PyPI had that day) and would happily point a load generator at any host, including
  production.  It now refuses non-loopback targets unless ``ALLOW_REMOTE_STRESS=1`` and only
  *checks* the locust version recorded in ``scripts/requirements-stress.lock``.
* ``scripts/feed_question_bank.py`` used to write observations into the default production
  evolution database.  It is now dry-run by default, and ``--commit`` requires an explicit
  ``--evolution-db``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STRESS = ROOT / "scripts" / "run_stress_test.sh"
STRESS_LOCK = ROOT / "scripts" / "requirements-stress.lock"


def _bash_executable() -> str | None:
    if os.name == "nt":
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
        return None
    return shutil.which("bash")


BASH = _bash_executable()
requires_bash = pytest.mark.skipif(BASH is None, reason="bash is required to run the stress script")


def _run_stress(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(ROOT)),
        "LANG": "C.UTF-8",
    }
    environment.update(env or {})
    return subprocess.run(
        [BASH, str(STRESS), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
        check=False,
    )


# ── run_stress_test.sh ─────────────────────────────────────────────────────


def test_stress_script_never_installs_locust_at_run_time():
    script = STRESS.read_text(encoding="utf-8")
    executed = [line for line in script.splitlines() if not line.lstrip().startswith("#")]

    assert not any("pip install locust" in line for line in executed)
    assert "requirements-stress.lock" in script
    assert "set -euo pipefail" in script


def test_stress_script_blocks_remote_targets_without_confirmation():
    script = STRESS.read_text(encoding="utf-8")

    assert "ALLOW_REMOTE_STRESS" in script
    assert "return 2" in script
    assert "127.0.0.1" in script


@requires_bash
def test_stress_script_refuses_a_remote_target_by_default():
    result = _run_stress("http://192.0.2.1:8000", "1", "1s")

    assert result.returncode == 2
    assert "ALLOW_REMOTE_STRESS" in result.stdout + result.stderr


@requires_bash
def test_stress_script_confirms_a_loopback_target_without_the_remote_override():
    result = _run_stress("http://127.0.0.1:1", "1", "1s")

    combined = result.stdout + result.stderr
    assert result.returncode != 2
    assert "不是回环地址" not in combined


@requires_bash
def test_stress_script_is_syntactically_valid():
    result = subprocess.run(
        [BASH, "-n", str(STRESS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_stress_lock_records_an_exact_locust_version():
    assert STRESS_LOCK.exists()
    pins = [
        line.strip()
        for line in STRESS_LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("locust")
    ]
    assert pins == ["locust==2.46.7"]


def test_stress_script_writes_a_reproducible_command_record():
    script = STRESS.read_text(encoding="utf-8")

    assert "benchmarks/stress_test.command.txt" in script
    assert "date -u" in script


# ── feed_question_bank.py ──────────────────────────────────────────────────


def _write_bank(path: Path, questions: list[str]) -> Path:
    import json

    payload = [
        {"case_id": f"q{index}", "kb_id": "rag_docs", "question": question, "family": "smoke"}
        for index, question in enumerate(questions)
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_observations(path: Path, questions: list[str]) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE failure_observations (id INTEGER PRIMARY KEY, question TEXT)")
        connection.executemany(
            "INSERT INTO failure_observations(question) VALUES (?)",
            [(question,) for question in questions],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_question_bank_feeder_defaults_to_dry_run(tmp_path, capsys):
    from scripts.feed_question_bank import main

    bank = _write_bank(tmp_path / "bank.json", ["问题一", "问题二"])
    evolution_db = tmp_path / "evolution.db"

    result = main(["--bank", str(bank), "--evolution-db", str(evolution_db)])

    output = capsys.readouterr().out
    assert result == 0
    assert '"mode": "dry-run"' in output
    assert "干跑" in output
    assert not evolution_db.exists(), "a dry run must not create or write the observations database"


def test_question_bank_feeder_requires_an_explicit_database_when_committing(tmp_path):
    from scripts.feed_question_bank import main

    bank = _write_bank(tmp_path / "bank.json", ["问题一"])

    with pytest.raises(SystemExit) as excinfo:
        main(["--bank", str(bank), "--commit"])

    assert excinfo.value.code == 2


def test_question_bank_feeder_counts_already_recorded_questions(tmp_path, capsys):
    from scripts.feed_question_bank import main

    bank = _write_bank(tmp_path / "bank.json", ["问题一", "问题二"])
    evolution_db = _write_observations(tmp_path / "evolution.db", ["问题一"])

    result = main(["--bank", str(bank), "--evolution-db", str(evolution_db)])

    output = capsys.readouterr().out
    assert result == 0
    assert '"already_ran": 1' in output
    assert '"pending": 1' in output


def test_question_bank_feeder_tolerates_a_missing_database(tmp_path, capsys):
    from scripts.feed_question_bank import main

    bank = _write_bank(tmp_path / "bank.json", ["问题一"])

    result = main(["--bank", str(bank), "--evolution-db", str(tmp_path / "absent.db")])

    output = capsys.readouterr().out
    assert result == 0
    assert '"already_ran": 0' in output


def test_question_bank_feeder_reports_the_target_database(tmp_path, capsys):
    from scripts.feed_question_bank import main

    bank = _write_bank(tmp_path / "bank.json", ["问题一"])
    evolution_db = tmp_path / "evolution.db"

    main(["--bank", str(bank), "--evolution-db", str(evolution_db)])

    output = capsys.readouterr().out
    assert '"evolution_db"' in output
    assert evolution_db.name in output
