"""One format-capability registry controlling every upload/index entry point."""

from __future__ import annotations

from pathlib import Path

from rag.materials.models import FormatCapability, FormatResolution

# Magic-byte families. The registry compares extension, declared MIME, and the
# detected family; any disagreement quarantines the material.
_TEXT_BYTES = {9, 10, 13, *range(32, 127)}


def detect_family(signature: bytes) -> str | None:
    """Detect a normalized magic family from a byte prefix, or None if unknown."""
    if not signature:
        return None
    head = signature[:16]
    if head[:2] == b"MZ":
        return "exe"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"
    if head[:4] == b"%PDF":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:4] == b"Rar!":
        return "rar"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM":
        return "bmp"
    if all(b in _TEXT_BYTES for b in head[:1024]):
        return "text"
    return None


def _cap(
    key: str,
    extensions,
    mime_types,
    magic_families,
    *,
    archive=True,
    preview=False,
    extract_text=False,
    local_ocr=False,
    structured_query=False,
    semantic_index=False,
    archive_container=False,
    citation_kind="paragraph",
) -> FormatCapability:
    return FormatCapability(
        key=key,
        extensions=frozenset(extensions),
        mime_types=frozenset(mime_types),
        magic_families=frozenset(magic_families),
        archive=archive,
        preview=preview,
        extract_text=extract_text,
        local_ocr=local_ocr,
        structured_query=structured_query,
        semantic_index=semantic_index,
        archive_container=archive_container,
        citation_kind=citation_kind,
    )


def build_default_format_registry() -> "FormatCapabilityRegistry":
    capabilities = [
        _cap("txt", ("txt", "md"), ("text/plain", "text/markdown"), ("text",), archive=False, preview=True, extract_text=True, semantic_index=True, citation_kind="paragraph"),
        _cap("pdf", ("pdf",), ("application/pdf",), ("pdf",), archive=False, preview=True, extract_text=True, local_ocr=True, semantic_index=True, citation_kind="page"),
        _cap("docx", ("docx",), ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",), ("zip",), archive=False, preview=True, extract_text=True, semantic_index=True, citation_kind="paragraph"),
        _cap("xlsx", ("xlsx",), ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",), ("zip",), archive=False, preview=True, extract_text=True, structured_query=True, semantic_index=True, citation_kind="cell_range"),
        _cap("csv", ("csv",), ("text/csv",), ("text",), archive=False, preview=True, extract_text=True, structured_query=True, semantic_index=True, citation_kind="cell_range"),
        _cap("pptx", ("pptx",), ("application/vnd.openxmlformats-officedocument.presentationml.presentation",), ("zip",), archive=False, preview=True, extract_text=True, semantic_index=True, citation_kind="slide"),
        _cap("doc", ("doc",), ("application/msword",), ("ole",), preview=False, extract_text=False, citation_kind="paragraph"),
        _cap("xls", ("xls",), ("application/vnd.ms-excel",), ("ole",), preview=False, extract_text=False, citation_kind="cell_range"),
        _cap("ppt", ("ppt",), ("application/vnd.ms-powerpoint",), ("ole",), preview=False, extract_text=False, citation_kind="slide"),
        _cap("jpg", ("jpg", "jpeg"), ("image/jpeg",), ("jpeg",), preview=True, extract_text=False, citation_kind="metadata"),
        _cap("png", ("png",), ("image/png",), ("png",), preview=True, extract_text=False, citation_kind="metadata"),
        _cap("tiff", ("tif", "tiff"), ("image/tiff",), ("tiff",), preview=True, extract_text=False, citation_kind="metadata"),
        _cap("bmp", ("bmp",), ("image/bmp",), ("bmp",), preview=True, extract_text=False, citation_kind="metadata"),
        _cap("zip", ("zip",), ("application/zip", "application/x-zip-compressed"), ("zip",), preview=True, extract_text=False, archive_container=True, citation_kind="archive_entry"),
        _cap("rar", ("rar",), ("application/vnd.rar",), ("rar",), preview=True, extract_text=False, archive_container=True, citation_kind="archive_entry"),
        _cap("7z", ("7z",), ("application/x-7z-compressed",), ("7z",), preview=True, extract_text=False, archive_container=True, citation_kind="archive_entry"),
    ]
    return FormatCapabilityRegistry({c.key: c for c in capabilities})


class FormatCapabilityRegistry:
    """Resolve filename + declared MIME + signature to one capability."""

    def __init__(self, capabilities: dict[str, FormatCapability]):
        self._by_key = capabilities
        self._by_ext: dict[str, FormatCapability] = {}
        self._by_mime: dict[str, FormatCapability] = {}
        for capability in capabilities.values():
            for ext in capability.extensions:
                self._by_ext[ext] = capability
            for mime in capability.mime_types:
                self._by_mime[mime] = capability

    def resolve(
        self, filename: str, declared_mime: str | None, signature: bytes
    ) -> FormatResolution:
        ext = Path(filename).suffix.lower().lstrip(".")
        ext_capability = self._by_ext.get(ext)
        detected = detect_family(signature)
        detected_mime = detected or ""

        if ext_capability is None:
            return FormatResolution(
                state="rejected",
                capability=None,
                detected_mime=detected_mime,
                reason_code="unsupported_extension",
                can_index=False,
            )

        if detected is not None and detected not in ext_capability.magic_families:
            return FormatResolution(
                state="format_mismatch",
                capability=ext_capability,
                detected_mime=detected_mime,
                reason_code="signature_mismatch",
                can_index=False,
            )

        if declared_mime:
            normalized_mime = declared_mime.lower().split(";")[0].strip()
            mime_capability = self._by_mime.get(normalized_mime)
            if mime_capability is not None and mime_capability is not ext_capability:
                return FormatResolution(
                    state="format_mismatch",
                    capability=ext_capability,
                    detected_mime=detected_mime,
                    reason_code="mime_mismatch",
                    can_index=False,
                )

        if ext_capability.archive_container:
            return FormatResolution(
                state="archive_only",
                capability=ext_capability,
                detected_mime=detected_mime,
                reason_code="archive_container",
                can_index=False,
            )
        if not ext_capability.extract_text:
            if ext_capability.key in {"doc", "xls", "ppt"}:
                return FormatResolution(
                    state="temporarily_unavailable",
                    capability=ext_capability,
                    detected_mime=detected_mime,
                    reason_code="legacy_conversion_disabled",
                    can_index=False,
                )
            return FormatResolution(
                state="archive_only",
                capability=ext_capability,
                detected_mime=detected_mime,
                reason_code="metadata_only",
                can_index=False,
            )
        return FormatResolution(
            state="supported",
            capability=ext_capability,
            detected_mime=detected_mime,
            reason_code="ok",
            can_index=True,
        )
