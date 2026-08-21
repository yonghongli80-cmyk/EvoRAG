"""Task 3: normalized extraction dispatch over the format registry."""


import pytest

from rag.materials.extraction import ExtractedMaterial, extract_material
from rag.materials.formats import build_default_format_registry


@pytest.fixture
def registry():
    return build_default_format_registry()


def test_text_file_extraction(tmp_path, registry):
    path = tmp_path / "notes.txt"
    path.write_text("hello world", encoding="utf-8")
    resolution = registry.resolve("notes.txt", None, path.read_bytes()[:16])

    material = extract_material(path, version_id=5, resolution=resolution)

    assert isinstance(material, ExtractedMaterial)
    assert material.units[0].text == "hello world"
    assert material.units[0].location.kind == "paragraph"


def test_unsupported_capability_raises(tmp_path, registry):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
    resolution = registry.resolve("image.jpg", None, path.read_bytes()[:16])

    with pytest.raises(ValueError, match="unsupported extraction"):
        extract_material(path, version_id=5, resolution=resolution)
