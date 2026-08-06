"""Task 1: unified format capability registry."""

import pytest

from rag.materials.formats import build_default_format_registry
from rag.materials.models import FormatResolution

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def pptx_signature() -> bytes:
    return b"PK\x03\x04" + b"\x00" * 12


def exe_signature() -> bytes:
    return b"MZ" + b"\x00" * 14


def pdf_signature() -> bytes:
    return b"%PDF-1.4"


@pytest.fixture
def default_format_registry():
    return build_default_format_registry()


def test_all_entry_points_share_one_format_registry(default_format_registry):
    pptx = default_format_registry.resolve(
        "policy.pptx",
        PPTX_MIME,
        pptx_signature(),
    )
    assert pptx.capability.key == "pptx"
    assert pptx.capability.extract_text is True
    assert pptx.capability.citation_kind == "slide"


def test_extension_mime_signature_mismatch_is_quarantined(default_format_registry):
    result = default_format_registry.resolve("invoice.pdf", "application/pdf", exe_signature())
    assert result.state == "format_mismatch"
    assert result.can_index is False


def test_pdf_resolves_as_supported(default_format_registry):
    result = default_format_registry.resolve("policy.pdf", "application/pdf", pdf_signature())
    assert result.state == "supported"
    assert result.can_index is True
    assert result.capability.local_ocr is True


def test_archive_container_is_archive_only(default_format_registry):
    result = default_format_registry.resolve("bundle.zip", "application/zip", pptx_signature())
    assert result.state == "archive_only"
    assert result.can_index is False
    assert result.capability.archive_container is True


def test_legacy_doc_is_temporarily_unavailable(default_format_registry):
    result = default_format_registry.resolve(
        "old.doc", "application/msword", b"\xd0\xcf\x11\xe0" + b"\x00" * 12
    )
    assert result.state == "temporarily_unavailable"
    assert result.can_index is False


def test_unknown_extension_is_rejected(default_format_registry):
    result = default_format_registry.resolve("notes.xyz", None, b"whatever")
    assert result.state == "rejected"
    assert result.capability is None


def test_mime_mismatch_is_quarantined(default_format_registry):
    result = default_format_registry.resolve("policy.pdf", PPTX_MIME, pdf_signature())
    assert result.state == "format_mismatch"
