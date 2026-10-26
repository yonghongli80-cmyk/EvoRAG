"""Build and validate a source-bound review manifest for unresolved SQL findings.

This module treats inventory and source files as data. It never imports or
executes application modules, evaluates SQL, or declares a SQL expression safe
or portable. A valid manifest proves only exact inventory/source binding and
the presence of per-finding review fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
_DISPOSITIONS = {"unreviewed", "understood_shape", "needs_followup", "external_source"}


class ManifestError(ValueError):
    """The manifest is incomplete, stale, or inconsistent with its source."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_inventory(inventory_path: Path) -> tuple[dict[str, Any], bytes]:
    raw = Path(inventory_path).read_bytes()
    try:
        inventory = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("inventory is not valid UTF-8 JSON") from exc
    if not isinstance(inventory, dict) or not isinstance(inventory.get("unresolved"), list):
        raise ManifestError("inventory must contain an unresolved list")
    return inventory, raw


def _source_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or not relative or any(part in {"..", ""} for part in rel.parts):
        raise ManifestError(f"invalid source path: {relative!r}")
    candidate = root.joinpath(rel)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError(f"source file is missing or unreadable: {relative}") from exc
    if resolved_root not in resolved.parents or not resolved.is_file():
        raise ManifestError(f"source path escapes the source root: {relative}")
    cursor = resolved_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ManifestError(f"source path traverses a symlink: {relative}")
    return resolved


def _evidence_location(reference: str) -> tuple[str, int]:
    if not isinstance(reference, str) or reference.count("#L") != 1:
        raise ManifestError("evidence must use a local path#Lpositive_line reference")
    relative, line_text = reference.rsplit("#L", 1)
    relative_path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or relative != relative_path.as_posix()
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ManifestError("evidence path must be normalized and source-root relative")
    if not line_text.isascii() or not line_text.isdecimal() or int(line_text) < 1:
        raise ManifestError("evidence line must be a positive integer")
    return relative, int(line_text)


def _validate_evidence(finding: dict[str, Any], source_root: Path) -> None:
    evidence = finding.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(reference, str) or not reference.strip() for reference in evidence)
    ):
        raise ManifestError(f"finding {finding['finding_id']} needs source evidence")
    if len(set(evidence)) != len(evidence):
        raise ManifestError(f"finding {finding['finding_id']} contains duplicate evidence refs")
    if finding["source_ref"] not in evidence:
        raise ManifestError(f"finding {finding['finding_id']} must cite its own source_ref")

    current_hashes: dict[str, str] = {}
    for reference in evidence:
        relative, line = _evidence_location(reference)
        source_file = _source_file(source_root, relative)
        raw_source = source_file.read_bytes()
        if line > len(raw_source.splitlines()):
            raise ManifestError(f"evidence line is outside the source file: {reference}")
        current_hashes[relative] = _sha256(raw_source)

    supplied_hashes = finding.get("evidence_sha256")
    if not isinstance(supplied_hashes, dict) or set(supplied_hashes) != set(current_hashes):
        raise ManifestError(f"finding {finding['finding_id']} evidence_sha256 must exactly cover its refs")
    for relative, current_hash in current_hashes.items():
        if supplied_hashes.get(relative) != current_hash:
            raise ManifestError(f"finding {finding['finding_id']} has stale evidence hash for {relative}")


def _finding_identity(item: dict[str, Any]) -> dict[str, Any]:
    required = ("path", "line", "owner", "sink", "classification")
    if any(key not in item for key in required):
        raise ManifestError("unresolved finding is missing an identity field")
    if not isinstance(item["path"], str) or type(item["line"]) is not int:
        raise ManifestError("unresolved finding path/line has an invalid type")
    if item["line"] < 1 or any(not isinstance(item[key], str) for key in ("owner", "sink", "classification")):
        raise ManifestError("unresolved finding identity has an invalid value")
    return {
        "path": item["path"],
        "line": item["line"],
        "owner": item["owner"],
        "sink": item["sink"],
        "scanner_classification": item["classification"],
    }


def _finding_id(identity: dict[str, Any]) -> str:
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sql-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_findings(inventory: dict[str, Any], source_root: Path) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in inventory["unresolved"]:
        if not isinstance(raw_item, dict):
            raise ManifestError("unresolved finding must be an object")
        identity = _finding_identity(raw_item)
        finding_id = _finding_id(identity)
        if finding_id in seen:
            raise ManifestError(f"inventory contains a duplicate finding: {finding_id}")
        seen.add(finding_id)
        source = _source_file(source_root, identity["path"])
        expected.append(
            {
                "finding_id": finding_id,
                **identity,
                "source_sha256": _sha256(source.read_bytes()),
                "source_ref": f"{identity['path']}#L{identity['line']}",
            }
        )
    expected.sort(key=lambda item: (item["path"], item["line"], item["sink"], item["finding_id"]))
    return expected


def _coverage(findings: list[dict[str, Any]]) -> dict[str, Any]:
    unreviewed = sum(item.get("disposition") == "unreviewed" for item in findings)
    bound = sum(
        item.get("disposition") in _DISPOSITIONS - {"unreviewed"}
        and isinstance(item.get("rationale"), str)
        and bool(item.get("rationale", "").strip())
        and isinstance(item.get("evidence"), list)
        and bool(item.get("evidence"))
        for item in findings
    )
    return {
        "finding_count": len(findings),
        "source_bound_dispositions": bound,
        "unreviewed": unreviewed,
        "complete": unreviewed == 0 and bound == len(findings),
    }


def build_manifest(inventory_path: str | Path, source_root: str | Path) -> dict[str, Any]:
    """Create a deterministic manifest whose findings all start unreviewed."""
    inventory_file = Path(inventory_path)
    root = Path(source_root)
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("source root is missing or unreadable") from exc
    if not root.is_dir():
        raise ManifestError("source root is not a directory")
    inventory, raw_inventory = _load_inventory(inventory_file)
    findings = []
    for expected in _expected_findings(inventory, root):
        findings.append(
            {
                **expected,
                "disposition": "unreviewed",
                "sql_shape_class": "",
                "rationale": "",
                "evidence": [],
                "evidence_sha256": {},
                "next_step": "",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "inventory_sha256": _sha256(raw_inventory),
            "source_root": root.name,
            "source_inventory_complete": inventory.get("coverage", {}).get("complete") is True,
            "source_inventory_unresolved_count": len(inventory["unresolved"]),
        },
        "findings": findings,
        "coverage": _coverage(findings),
        "portability_readiness": "not_assessed",
    }


def validate_manifest(manifest: dict[str, Any], inventory_path: str | Path, source_root: str | Path) -> dict[str, Any]:
    """Validate exact finding identity and current hashes, then return coverage.

    This validates structure and binding only. It does not judge whether any
    human rationale is correct, whether generated SQL is safe, or whether it is
    compatible with PostgreSQL.
    """
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema_version")
    if manifest.get("portability_readiness") != "not_assessed":
        raise ManifestError("portability_readiness must remain not_assessed")
    root = Path(source_root)
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError("source root is missing or unreadable") from exc
    inventory, raw_inventory = _load_inventory(Path(inventory_path))
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ManifestError("manifest source binding is missing")
    if source.get("inventory_sha256") != _sha256(raw_inventory):
        raise ManifestError("inventory hash is stale")
    if source.get("source_root") != root.name:
        raise ManifestError("source root binding does not match")
    if source.get("source_inventory_complete") != (inventory.get("coverage", {}).get("complete") is True) or source.get(
        "source_inventory_unresolved_count"
    ) != len(inventory["unresolved"]):
        raise ManifestError("inventory coverage snapshot does not match")

    expected = _expected_findings(inventory, root)
    actual = manifest.get("findings")
    if not isinstance(actual, list):
        raise ManifestError("manifest findings must be a list")
    ids = [item.get("finding_id") for item in actual if isinstance(item, dict)]
    if len(ids) != len(actual) or any(not isinstance(item_id, str) for item_id in ids):
        raise ManifestError("manifest contains a malformed finding_id")
    if len(set(ids)) != len(ids):
        raise ManifestError("manifest contains a malformed or duplicate finding_id")
    expected_by_id = {item["finding_id"]: item for item in expected}
    actual_by_id = {item["finding_id"]: item for item in actual}
    if set(actual_by_id) != set(expected_by_id):
        missing = sorted(set(expected_by_id) - set(actual_by_id))
        extra = sorted(set(actual_by_id) - set(expected_by_id))
        raise ManifestError(f"finding set differs from inventory (missing={missing}, extra={extra})")

    for finding_id, expected_item in expected_by_id.items():
        item = actual_by_id[finding_id]
        for key, value in expected_item.items():
            if item.get(key) != value:
                raise ManifestError(f"finding {finding_id} has stale or mismatched {key}")
        disposition = item.get("disposition")
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            raise ManifestError(f"finding {finding_id} has an unknown disposition")
        evidence = item.get("evidence")
        evidence_sha256 = item.get("evidence_sha256")
        rationale = item.get("rationale")
        next_step = item.get("next_step", "")
        sql_shape_class = item.get("sql_shape_class", "")
        if disposition == "unreviewed":
            if evidence or evidence_sha256 or rationale or next_step or sql_shape_class:
                raise ManifestError(f"unreviewed finding {finding_id} contains review claims")
            continue
        _validate_evidence(item, root)
        if not isinstance(rationale, str) or not rationale.strip():
            raise ManifestError(f"finding {finding_id} needs a rationale")
        if not isinstance(sql_shape_class, str) or not sql_shape_class.strip():
            raise ManifestError(f"finding {finding_id} needs a SQL shape classification")
        if disposition == "needs_followup" and (not isinstance(next_step, str) or not next_step.strip()):
            raise ManifestError(f"finding {finding_id} needs a next_step")

    coverage = _coverage(actual)
    supplied_coverage = manifest.get("coverage")
    if supplied_coverage != coverage:
        raise ManifestError("manifest coverage summary does not match its findings")
    return {
        "coverage": coverage,
        "source_inventory_complete": source["source_inventory_complete"],
        "portability_readiness": "not_assessed",
    }


def write_manifest(manifest: dict[str, Any], output_path: str | Path) -> None:
    """Write stable UTF-8 JSON to a new path; never overwrite evidence."""
    with Path(output_path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="create an all-unreviewed source-bound manifest")
    build.add_argument("--inventory", required=True, type=Path)
    build.add_argument("--source-root", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("check", help="validate exact finding and source bindings")
    check.add_argument("--inventory", required=True, type=Path)
    check.add_argument("--source-root", required=True, type=Path)
    check.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_manifest(args.inventory, args.source_root)
            write_manifest(manifest, args.output)
            print(
                json.dumps(
                    {
                        "coverage": manifest["coverage"],
                        "source_inventory_complete": manifest["source"]["source_inventory_complete"],
                        "portability_readiness": "not_assessed",
                    },
                    sort_keys=True,
                )
            )
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            result = validate_manifest(manifest, args.inventory, args.source_root)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (ManifestError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"manifest check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.command == "check":
        return 0 if result["coverage"]["complete"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
