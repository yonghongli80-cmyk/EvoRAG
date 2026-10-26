from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import review_dynamic_sql


def _write_source_tree(root: Path) -> tuple[Path, Path]:
    source_root = root / "rag"
    source_root.mkdir(parents=True)
    source = source_root / "sample.py"
    source.write_text(
        "from pathlib import Path\n"
        f"Path({str(root / 'must-not-exist')!r}).write_text('executed')\n"
        "def load(conn, item_id):\n"
        "    conn.execute(f'SELECT * FROM items WHERE id = {item_id}')\n",
        encoding="utf-8",
    )
    inventory_path = root / "inventory.json"
    inventory = {
        "schema_version": 1,
        "entries": [],
        "unresolved": [
            {
                "path": "sample.py",
                "line": 4,
                "owner": "load",
                "sink": "execute",
                "classification": "dynamic_fstring",
            }
        ],
        "transaction_markers": [],
        "code_markers": [],
        "errors": [],
        "coverage": {
            "files_discovered": 1,
            "files_scanned": 1,
            "unresolved_count": 1,
            "errors_count": 0,
            "complete": False,
        },
    }
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return source_root, inventory_path


def _build(root: Path) -> tuple[Path, Path, dict]:
    source_root, inventory_path = _write_source_tree(root)
    manifest = review_dynamic_sql.build_manifest(inventory_path, source_root)
    return source_root, inventory_path, manifest


def _mark_reviewed(
    manifest: dict,
    source_root: Path,
    *,
    evidence: list[str],
    disposition: str = "understood_shape",
) -> None:
    finding = manifest["findings"][0]
    refs_by_path = {reference.rsplit("#L", 1)[0] for reference in evidence}
    finding.update(
        {
            "disposition": disposition,
            "sql_shape_class": "reviewed_fixture_shape",
            "rationale": "Fixture-backed local evidence for this source shape.",
            "evidence": evidence,
            "evidence_sha256": {
                relative: hashlib.sha256((source_root / relative).read_bytes()).hexdigest() for relative in refs_by_path
            },
            "next_step": "Trace remaining call sites." if disposition == "needs_followup" else "",
        }
    )
    manifest["coverage"] = {
        "finding_count": 1,
        "source_bound_dispositions": 1,
        "unreviewed": 0,
        "complete": True,
    }


def test_build_binds_each_finding_to_exact_inventory_identity_and_source_hash(
    tmp_path: Path,
) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    raw_inventory = inventory_path.read_bytes()
    raw_source = (source_root / "sample.py").read_bytes()

    assert manifest["schema_version"] == 1
    assert manifest["source"]["inventory_sha256"] == hashlib.sha256(raw_inventory).hexdigest()
    assert manifest["source"]["source_root"] == "rag"
    assert manifest["source"]["source_inventory_complete"] is False
    assert manifest["source"]["source_inventory_unresolved_count"] == 1
    assert len(manifest["findings"]) == 1
    finding = manifest["findings"][0]
    assert {key: finding[key] for key in ("path", "line", "owner", "sink", "scanner_classification")} == {
        "path": "sample.py",
        "line": 4,
        "owner": "load",
        "sink": "execute",
        "scanner_classification": "dynamic_fstring",
    }
    assert finding["source_sha256"] == hashlib.sha256(raw_source).hexdigest()
    assert finding["finding_id"]
    assert finding["evidence"] == []
    assert finding["evidence_sha256"] == {}


def test_new_manifest_starts_unreviewed_and_keeps_portability_unassessed(
    tmp_path: Path,
) -> None:
    _, _, manifest = _build(tmp_path)

    assert [item["disposition"] for item in manifest["findings"]] == ["unreviewed"]
    assert manifest["coverage"]["unreviewed"] == 1
    assert manifest["coverage"]["complete"] is False
    assert manifest["portability_readiness"] == "not_assessed"


def test_validator_rejects_missing_or_duplicate_findings(tmp_path: Path) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    missing = json.loads(json.dumps(manifest))
    missing["findings"].clear()
    duplicate = json.loads(json.dumps(manifest))
    duplicate["findings"].append(dict(duplicate["findings"][0]))

    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(missing, inventory_path, source_root)
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(duplicate, inventory_path, source_root)


def test_validator_rejects_stale_inventory_and_source_hashes(tmp_path: Path) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    (source_root / "sample.py").write_text(
        (source_root / "sample.py").read_text(encoding="utf-8") + "# changed source\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)

    source_root, inventory_path, manifest = _build(tmp_path / "second")
    inventory_path.write_text(
        inventory_path.read_text(encoding="utf-8").replace('"owner": "load"', '"owner": "other"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_validator_rejects_identity_drift_and_unknown_source_path(tmp_path: Path) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    bad_line = json.loads(json.dumps(manifest))
    bad_line["findings"][0]["line"] = 3
    bad_path = json.loads(json.dumps(manifest))
    bad_path["findings"][0]["path"] = "outside.py"

    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(bad_line, inventory_path, source_root)
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(bad_path, inventory_path, source_root)


@pytest.mark.parametrize(
    "disposition,changes",
    [
        ("understood_shape", {}),
        ("needs_followup", {"rationale": "reviewed but unresolved"}),
        ("external_source", {}),
    ],
)
def test_nondefault_disposition_requires_finding_specific_rationale_and_evidence(
    tmp_path: Path, disposition: str, changes: dict[str, str]
) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    finding = manifest["findings"][0]
    finding["disposition"] = disposition
    finding.update(changes)

    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_followup_requires_next_step_and_validator_does_not_upgrade_portability(
    tmp_path: Path,
) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    finding = manifest["findings"][0]
    finding.update(
        {
            "disposition": "needs_followup",
            "rationale": "Call-site provenance is not established.",
            "evidence": ["sample.py#L4"],
            "evidence_sha256": {"sample.py": hashlib.sha256((source_root / "sample.py").read_bytes()).hexdigest()},
            "sql_shape_class": "opaque_runtime_query",
            "next_step": "Trace callers and the query value source.",
        }
    )
    manifest["coverage"] = {
        "finding_count": 1,
        "source_bound_dispositions": 1,
        "unreviewed": 0,
        "complete": True,
    }

    result = review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)

    assert result["coverage"]["complete"] is True
    assert result["source_inventory_complete"] is False
    assert result["portability_readiness"] == "not_assessed"


@pytest.mark.parametrize(
    "reference",
    [
        "missing.py#L1",
        "../outside.py#L1",
        "sample.py#L0",
        "sample.py#L99",
        "sample.py#Lnot-a-line",
    ],
)
def test_validator_rejects_nonexistent_escaping_or_invalid_line_evidence(tmp_path: Path, reference: str) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    _mark_reviewed(manifest, source_root, evidence=["sample.py#L4"])
    finding = manifest["findings"][0]
    finding["evidence"] = [reference]
    relative = reference.rsplit("#L", 1)[0]
    finding["evidence_sha256"] = {
        relative: hashlib.sha256((source_root / relative).read_bytes()).hexdigest()
        if relative == "sample.py"
        else "0" * 64
    }

    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_validator_requires_exact_hash_map_and_rejects_missing_or_forged_hashes(
    tmp_path: Path,
) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    _mark_reviewed(manifest, source_root, evidence=["sample.py#L4"])
    finding = manifest["findings"][0]

    finding["evidence_sha256"] = {}
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)

    finding["evidence_sha256"] = {
        "sample.py": hashlib.sha256((source_root / "sample.py").read_bytes()).hexdigest(),
        "unused.py": "0" * 64,
    }
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)

    finding["evidence_sha256"] = {"sample.py": "0" * 64}
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_validator_requires_each_finding_source_ref_in_its_evidence(tmp_path: Path) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    _mark_reviewed(manifest, source_root, evidence=["sample.py#L3"])

    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_validator_binds_cross_file_evidence_and_rejects_stale_content(
    tmp_path: Path,
) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    supporting_file = source_root / "parser.py"
    supporting_file.write_text("def build_key():\n    return 'fixed'\n", encoding="utf-8")
    _mark_reviewed(
        manifest,
        source_root,
        evidence=["sample.py#L4", "parser.py#L1"],
    )

    result = review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)
    assert result["coverage"]["complete"] is True

    supporting_file.write_text("def build_key():\n    return input()\n", encoding="utf-8")
    with pytest.raises(ValueError):
        review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)


def test_external_source_disposition_requires_local_source_binding(tmp_path: Path) -> None:
    source_root, inventory_path, manifest = _build(tmp_path)
    _mark_reviewed(
        manifest,
        source_root,
        evidence=["sample.py#L4"],
        disposition="external_source",
    )

    result = review_dynamic_sql.validate_manifest(manifest, inventory_path, source_root)

    assert result["coverage"]["complete"] is True
    assert result["portability_readiness"] == "not_assessed"


def test_build_reads_source_without_importing_or_executing_it(tmp_path: Path) -> None:
    source_root, inventory_path = _write_source_tree(tmp_path)

    review_dynamic_sql.build_manifest(inventory_path, source_root)

    assert not (tmp_path / "must-not-exist").exists()


def test_manifest_writer_is_stable_and_refuses_to_overwrite(tmp_path: Path) -> None:
    source_root, _, manifest = _build(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    review_dynamic_sql.write_manifest(manifest, first)
    review_dynamic_sql.write_manifest(manifest, second)

    assert first.read_bytes() == second.read_bytes()
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        review_dynamic_sql.write_manifest(manifest, first)
    assert first.read_bytes() == before
