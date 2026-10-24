"""Task 5: safe archive manifest, confirmation, and child version diffing."""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from pathlib import Path

import pytest

from rag.materials.archives import (
    ArchiveLimits,
    ArchiveService,
    IndexingFailed,
    inspect_archive,
)
from rag.materials.formats import build_default_format_registry
from rag.user_db import UserDB

REGISTRY = build_default_format_registry()
LIMITS = ArchiveLimits(
    archive_bytes=1024 * 1024,
    expanded_bytes=512 * 1024 * 1024,
    entry_count=200,
    nesting_depth=2,
    ratio=100,
    entry_bytes=256 * 1024,
)
ZIP_CAP = REGISTRY.resolve("bundle.zip", "application/zip", b"PK\x03\x04").capability
assert ZIP_CAP is not None and ZIP_CAP.archive_container

PDF_A = b"%PDF-1.4\nA"
PDF_B = b"%PDF-1.4\nB"
PDF_B2 = b"%PDF-1.4\nB2"
PDF_C = b"%PDF-1.4\nC"


def build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _mark_encrypted(raw: bytes) -> bytes:
    """Set the general-purpose encryption bit so detection is exercised."""
    out = bytearray(raw)
    assert raw[:4] == b"PK\x03\x04"
    flag = int.from_bytes(raw[6:8], "little")
    out[6:8] = (flag | 0x1).to_bytes(2, "little")
    index = raw.find(b"PK\x01\x02")
    if index != -1:
        flag = int.from_bytes(raw[index + 8 : index + 10], "little")
        out[index + 8 : index + 10] = (flag | 0x1).to_bytes(2, "little")
    return bytes(out)


@pytest.fixture
def archive_service(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    service = ArchiveService(
        db=db,
        versions_root=versions_root,
        registry=REGISTRY,
        limits=LIMITS,
    )
    user_id = db.create_user("alice", "Password-123!")
    db.create_kb_metadata("kb-1", "Archive test KB", owner_id=user_id)
    service.actor = {"id": user_id, "username": "alice", "is_admin": False}
    return service


def write_source(tmp_path: Path, name: str, entries: dict[str, bytes]) -> Path:
    source = tmp_path / name
    source.write_bytes(build_zip(entries))
    return source


# ---------------------------------------------------------------------------
# Manifest inspection: path traversal, limits, per-entry validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["../secret.txt", "/absolute.txt", "C:/evil.txt", "a/../../x.txt", "..\\win.txt"]
)
def test_archive_rejects_escaping_paths(tmp_path, name):
    source = tmp_path / "bundle.zip"
    source.write_bytes(build_zip({name: b"x"}))
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    for entry in manifest.entries:
        assert entry.state == "rejected"
        assert entry.reason_code == "unsafe_path"


def test_archive_rejects_nesting_beyond_depth(tmp_path):
    source = tmp_path / "deep.zip"
    source.write_bytes(build_zip({"a/b/c/d.txt": b"x"}))
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    assert manifest.entries[0].state == "rejected"
    assert manifest.entries[0].reason_code == "nesting_depth_exceeded"


def test_archive_rejects_encrypted_entry(tmp_path):
    source = tmp_path / "enc.zip"
    source.write_bytes(_mark_encrypted(build_zip({"secret.txt": b"hidden"})))
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    assert manifest.entries[0].state == "rejected"
    assert manifest.entries[0].reason_code == "encrypted_entry"


def test_archive_limits_compression_ratio(tmp_path):
    # 101 highly compressible entries => expanded/compressed ratio > 100.
    source = tmp_path / "bomb.zip"
    source.write_bytes(build_zip({f"f{i}.txt": b"a" * 100_000 for i in range(101)}))
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    assert manifest.state == "rejected"
    assert manifest.reason_code == "compression_ratio_exceeded"


def test_archive_limits_entry_count(tmp_path):
    source = tmp_path / "many.zip"
    source.write_bytes(build_zip({f"f{i}.txt": b"x" for i in range(LIMITS.entry_count + 1)}))
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    assert manifest.state == "rejected"
    assert manifest.reason_code == "entry_count_exceeded"


def test_archive_preview_approves_supported_children(tmp_path):
    source = write_source(tmp_path, "onboarding.zip", {"张三/合同.pdf": PDF_A, "说明.md": "# 说明".encode()})
    manifest = inspect_archive(source, ZIP_CAP, LIMITS)
    assert manifest.state == "ok"
    approved = [e for e in manifest.entries if e.state == "approved"]
    assert {e.normalized_path for e in approved} == {"张三/合同.pdf", "说明.md"}


# ---------------------------------------------------------------------------
# Confirmation: children invisible before, searchable after
# ---------------------------------------------------------------------------


def test_archive_has_no_searchable_children_before_confirmation(tmp_path, archive_service):
    source = write_source(tmp_path, "onboarding.zip", {"张三/合同.pdf": PDF_A})
    preview = archive_service.upload_zip("onboarding.zip", "kb-1", archive_service.actor, source)
    assert archive_service.search("张三 合同") == []

    result = archive_service.confirm(
        preview.manifest_id,
        archive_service.actor,
        selected_entry_ids=preview.supported_ids,
        expected_version=0,
        idempotency_key="arc-1",
    )
    assert result.status == "confirmed"
    hits = archive_service.search("张三 合同")
    assert [h["relative_path"] for h in hits] == ["张三/合同.pdf"]


def test_archive_confirmation_is_idempotent(tmp_path, archive_service):
    source = write_source(tmp_path, "bundle.zip", {"a.pdf": PDF_A})
    preview = archive_service.upload_zip("bundle.zip", "kb-1", archive_service.actor, source)
    first = archive_service.confirm(
        preview.manifest_id, archive_service.actor, preview.supported_ids,
        expected_version=0, idempotency_key="arc-x",
    )
    second = archive_service.confirm(
        preview.manifest_id, archive_service.actor, preview.supported_ids,
        expected_version=0, idempotency_key="arc-x",
    )
    assert first.status == "confirmed"
    assert second.status == "already_confirmed"
    assert second.container_version_id == first.container_version_id
    # No duplicate child documents were created by the second call.
    assert len(archive_service.current_children()) == 1


def test_archive_confirm_rejects_viewer(tmp_path, archive_service):
    source = write_source(tmp_path, "bundle.zip", {"a.pdf": PDF_A})
    preview = archive_service.upload_zip("bundle.zip", "kb-1", archive_service.actor, source)
    viewer = {"id": 43, "username": "mallory", "is_admin": False}
    with pytest.raises(ArchiveService.ConfirmationError):
        archive_service.confirm(
            preview.manifest_id, viewer, preview.supported_ids,
            expected_version=0, idempotency_key="arc-v",
        )
    assert archive_service.current_children() == []


def test_archive_confirm_version_conflict(tmp_path, archive_service):
    source = write_source(tmp_path, "bundle.zip", {"a.pdf": PDF_A})
    preview = archive_service.upload_zip("bundle.zip", "kb-1", archive_service.actor, source)
    with pytest.raises(ArchiveService.ConfirmationError):
        archive_service.confirm(
            preview.manifest_id, archive_service.actor, preview.supported_ids,
            expected_version=99, idempotency_key="arc-c",
        )


# ---------------------------------------------------------------------------
# Replacement diff atomicity
# ---------------------------------------------------------------------------


def test_archive_replacement_diffs_children_atomically(tmp_path, archive_service):
    first_zip = write_source(tmp_path, "bundle.zip", {"a.pdf": PDF_A, "b.pdf": PDF_B})
    p1 = archive_service.upload_zip("bundle.zip", "kb-1", archive_service.actor, first_zip)
    r1 = archive_service.confirm(
        p1.manifest_id, archive_service.actor, p1.supported_ids,
        expected_version=0, idempotency_key="arc-1",
    )
    assert archive_service.current_children() == ["a.pdf", "b.pdf"]
    assert r1.status == "confirmed"

    replace_zip = write_source(tmp_path, "bundle2.zip", {"a.pdf": PDF_A, "b.pdf": PDF_B2, "c.pdf": PDF_C})
    p2 = archive_service.upload_zip("bundle.zip", "kb-1", archive_service.actor, replace_zip)
    archive_service.fail_before_batch_activation()
    with pytest.raises(IndexingFailed):
        archive_service.confirm(
            p2.manifest_id, archive_service.actor, p2.supported_ids,
            expected_version=1, idempotency_key="arc-2",
        )
    # Old container children stay intact after the failed replacement.
    assert archive_service.current_children() == ["a.pdf", "b.pdf"]

    # A clean retry replaces only the changed child and adds the new one.
    archive_service.fail_before_batch_activation()
    archive_service.bypass_failure_hook = True
    r2 = archive_service.confirm(
        p2.manifest_id, archive_service.actor, p2.supported_ids,
        expected_version=1, idempotency_key="arc-3",
    )
    assert r2.status == "confirmed"
    assert r2.unchanged_children == ("a.pdf",)
    assert sorted(r2.created_children) == ["b.pdf", "c.pdf"]
    assert sorted(archive_service.current_children()) == ["a.pdf", "b.pdf", "c.pdf"]
    assert r2.container_version_id != r1.container_version_id


def test_copyfileobj_bounded_rejects_oversized_stream():
    from rag.materials.archives import ArchiveRejected, _copyfileobj_bounded
    src = io.BytesIO(b"A" * 1024)
    dst = io.BytesIO()
    with pytest.raises(ArchiveRejected) as exc_info:
        _copyfileobj_bounded(src, dst, max_bytes=500, chunk_size=128)
    assert exc_info.value.reason_code == "entry_size_exceeded"



# ---------------------------------------------------------------------------
# D-06: 7z members are streamed to disk under the entry cap, never read whole
# ---------------------------------------------------------------------------

SEVENZ_CAP = REGISTRY.resolve("bundle.7z", None, None).capability
assert SEVENZ_CAP is not None and SEVENZ_CAP.archive_container


def build_7z(tmp_path: Path, entries: dict[str, bytes], name: str = "bundle.7z") -> Path:
    import py7zr

    source_dir = tmp_path / f"7z-source-{uuid.uuid4().hex}"
    source_dir.mkdir()
    archive = tmp_path / name
    with py7zr.SevenZipFile(archive, "w") as seven_zip:
        for index, (entry_name, data) in enumerate(entries.items()):
            member = source_dir / f"member-{index}.bin"
            member.write_bytes(data)
            seven_zip.write(member, entry_name)
    return archive


def test_7z_manifest_lists_and_approves_supported_children(tmp_path):
    archive = build_7z(tmp_path, {"a.pdf": PDF_A, "nested/b.pdf": PDF_B})

    manifest = inspect_archive(archive, SEVENZ_CAP, LIMITS)

    assert manifest.backend == "7z"
    assert manifest.state == "ok"
    assert {e.normalized_path for e in manifest.entries if e.state == "approved"} == {
        "a.pdf",
        "nested/b.pdf",
    }


def test_7z_confirmation_activates_streamed_children(tmp_path, archive_service):
    archive = build_7z(tmp_path, {"a.pdf": PDF_A, "b.pdf": PDF_B})

    preview = archive_service.upload_zip(
        "bundle.7z", "kb-1", archive_service.actor, archive
    )
    result = archive_service.confirm(
        preview.manifest_id,
        archive_service.actor,
        preview.supported_ids,
        expected_version=0,
        idempotency_key="arc-7z",
    )

    assert result.status == "confirmed"
    assert archive_service.current_children() == ["a.pdf", "b.pdf"]


def test_7z_oversized_member_is_rejected_without_promoting(tmp_path, archive_service):
    # Incompressible padding keeps the archive inside the compression-ratio limit
    # so the rejection under test is the per-entry streaming cap, not the manifest.
    archive = build_7z(
        tmp_path,
        {"ok.pdf": PDF_A, "big.pdf": os.urandom(LIMITS.entry_bytes + 1)},
    )

    preview = archive_service.upload_zip(
        "bundle.7z", "kb-1", archive_service.actor, archive
    )
    result = archive_service.confirm(
        preview.manifest_id,
        archive_service.actor,
        preview.supported_ids,
        expected_version=0,
        idempotency_key="arc-7z-big",
    )

    assert result.status == "confirmed"
    assert result.created_children == ("ok.pdf",)
    assert [entry["reason_code"] for entry in result.rejected_entries] == [
        "entry_size_exceeded"
    ]
    assert archive_service.current_children() == ["ok.pdf"]


def test_7z_extraction_never_materializes_the_member_in_memory(
    tmp_path, monkeypatch
):
    import py7zr

    from rag.materials.archives import _extract_entry

    def _fail_read(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("SevenZipFile.read() would load the member into memory")

    monkeypatch.setattr(py7zr.SevenZipFile, "read", _fail_read, raising=False)
    archive = build_7z(tmp_path, {"a.pdf": PDF_A * 2000})
    destination = tmp_path / "member.bin"

    _extract_entry(archive, "7z", "a.pdf", destination, max_bytes=LIMITS.entry_bytes)

    assert destination.read_bytes() == PDF_A * 2000


def test_7z_extraction_stops_at_the_entry_cap(tmp_path):
    from rag.materials.archives import ArchiveRejected, _extract_entry

    archive = build_7z(tmp_path, {"a.pdf": b"x" * (2 * 1024 * 1024)})
    destination = tmp_path / "member.bin"

    with pytest.raises(ArchiveRejected) as caught:
        _extract_entry(archive, "7z", "a.pdf", destination, max_bytes=64 * 1024)

    assert caught.value.reason_code == "entry_size_exceeded"
    assert not destination.exists()


def test_7z_extraction_without_a_bound_is_refused(tmp_path):
    from rag.materials.archives import ArchiveConfirmationError, _extract_entry

    archive = build_7z(tmp_path, {"a.pdf": PDF_A})

    with pytest.raises(ArchiveConfirmationError) as caught:
        _extract_entry(archive, "7z", "a.pdf", tmp_path / "member.bin", max_bytes=None)

    assert "archive_entry_limit_required" in str(caught.value)
