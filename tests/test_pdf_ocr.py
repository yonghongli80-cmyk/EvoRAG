"""Task 3: text-first PDF extraction and local OCR fallback."""

from pathlib import Path

import pytest

from rag.materials.pdf_ocr import OcrPage, extract_pdf


class FakeOCR:
    def __init__(self):
        self.calls = 0
        self.result = OcrPage(text="", confidence=0.0)

    def ocr_page(self, render: bytes) -> OcrPage:
        self.calls += 1
        return self.result


@pytest.fixture
def fake_ocr():
    return FakeOCR()


@pytest.fixture
def pdf_fixture(tmp_path):
    class Fixture:
        def __init__(self, root: Path):
            self.root = root

        def text_pdf(self) -> Path:
            import fitz

            path = self.root / "text.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text(
                (72, 72),
                "劳动合同 保密义务 员工手册 部门职责 考勤制度 请假流程",
                fontname="china-s",
            )
            doc.save(str(path))
            doc.close()
            return path

        def scan_pdf(self) -> Path:
            import fitz

            path = self.root / "scan.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(str(path))
            doc.close()
            return path

    return Fixture(tmp_path)


def test_pdf_uses_text_layer_before_local_ocr(pdf_fixture, fake_ocr):
    result = extract_pdf(pdf_fixture.text_pdf(), version_id=9, ocr=fake_ocr)
    assert "劳动合同" in result.units[0].text
    assert result.units[0].source == "native_text"
    assert fake_ocr.calls == 0


def test_scanned_pdf_uses_local_ocr_and_marks_uncertainty(pdf_fixture, fake_ocr):
    fake_ocr.result = OcrPage(
        text="姓名 张三", confidence=0.83, boxes=((10, 20, 50, 40),)
    )
    result = extract_pdf(pdf_fixture.scan_pdf(), version_id=9, ocr=fake_ocr)
    assert result.units[0].source == "local_ocr"
    assert result.units[0].confidence == 0.83
    assert result.units[0].location.kind == "page_region"


def test_pdf_without_ocr_keeps_blank_sparse_pages(pdf_fixture):
    result = extract_pdf(pdf_fixture.scan_pdf(), version_id=9, ocr=None)
    assert result.units[0].text == ""
    assert result.units[0].source == "native_text"
