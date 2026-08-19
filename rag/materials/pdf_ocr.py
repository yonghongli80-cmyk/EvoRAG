"""Text-first PDF extraction with a local OCR fallback for sparse pages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rag.materials.models import CitationLocation, ExtractedMaterial, ExtractedUnit

_MIN_TEXT_CHARS = 20


@dataclass(frozen=True)
class OcrPage:
    text: str
    confidence: float
    boxes: tuple[tuple[float, float, float, float], ...] = ()


class LocalOcrEngine:
    """Run RapidOCR locally; never calls the LLM or embedding client.

    The heavy model is imported lazily so tests and non-OCR deployments never
    pay for it, and OCR never decides identity, ownership, or subject.
    """

    def __init__(self) -> None:
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def ocr_page(self, render: bytes) -> OcrPage:
        engine = self._get_engine()
        result, _ = engine(render)
        if not result:
            return OcrPage(text="", confidence=0.0)
        lines = []
        boxes = []
        for item in result:
            box, text = item[0], item[1]
            lines.append(text)
            if len(box) == 4:
                x0, y0 = box[0]
                x2, y2 = box[2]
                boxes.append((float(x0), float(y0), float(x2), float(y2)))
        return OcrPage(
            text="\n".join(lines),
            confidence=min(1.0, sum(float(i[2]) for i in result) / max(1, len(result))),
            boxes=tuple(boxes),
        )


def _significant(text: str) -> bool:
    return len(text.strip()) >= _MIN_TEXT_CHARS


def _render_page(page, max_pixels: int = 4_000_000) -> bytes:
    """Render a page at bounded DPI for OCR."""
    import fitz

    dpi = 200
    while True:
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix)
        if pix.width * pix.height <= max_pixels or dpi <= 72:
            return pix.tobytes("png")
        dpi -= 25


def extract_pdf(path: Path, version_id: int, ocr: LocalOcrEngine | None = None) -> ExtractedMaterial:
    """Extract native text per page; OCR only sparse pages when an engine is given."""
    import fitz

    units: list[ExtractedUnit] = []
    warnings: list[str] = []
    document = fitz.open(str(path))
    try:
        for page_no in range(document.page_count):
            page = document[page_no]
            native = (page.get_text("text") or "").strip()
            if _significant(native):
                units.append(
                    ExtractedUnit(
                        unit_key=f"page:{page_no + 1}",
                        text=native,
                        location=CitationLocation(kind="page", value=str(page_no + 1)),
                        source="native_text",
                        confidence=1.0,
                        metadata={},
                    )
                )
                continue
            if ocr is None:
                units.append(
                    ExtractedUnit(
                        unit_key=f"page:{page_no + 1}",
                        text="",
                        location=CitationLocation(kind="page", value=str(page_no + 1)),
                        source="native_text",
                        confidence=0.0,
                        metadata={},
                    )
                )
                continue
            ocr_page = ocr.ocr_page(_render_page(page))
            if ocr_page.text.strip():
                units.append(
                    ExtractedUnit(
                        unit_key=f"page:{page_no + 1}",
                        text=ocr_page.text,
                        location=CitationLocation(kind="page_region", value=str(page_no + 1)),
                        source="local_ocr",
                        confidence=ocr_page.confidence,
                        metadata={"boxes": str(ocr_page.boxes)},
                    )
                )
            else:
                units.append(
                    ExtractedUnit(
                        unit_key=f"page:{page_no + 1}",
                        text="",
                        location=CitationLocation(kind="page", value=str(page_no + 1)),
                        source="local_ocr",
                        confidence=0.0,
                        metadata={},
                    )
                )
    finally:
        document.close()
    return ExtractedMaterial(
        version_id=version_id,
        parser_schema_version="pdf-v1",
        units=tuple(units),
        warnings=tuple(warnings),
    )
