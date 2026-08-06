"""Task 2: streamed, bounded, hashed staging."""

import hashlib

import pytest

from rag.materials.formats import build_default_format_registry
from rag.materials.models import MaterialLimits
from rag.materials.staging import MaterialTooLarge, stream_upload_to_stage

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class CountingAsyncChunks:
    def __init__(self, chunks):
        self._chunks = chunks
        self.max_requested_chunk = 0

    def __aiter__(self):
        return self._iterator()

    async def _iterator(self):
        for chunk in self._chunks:
            self.max_requested_chunk = max(self.max_requested_chunk, len(chunk))
            yield chunk


def async_chunks(data):
    async def gen():
        for chunk in data:
            yield chunk

    return gen()


@pytest.fixture
def registry():
    return build_default_format_registry()


@pytest.mark.asyncio
async def test_streamed_stage_hashes_without_unbounded_read(tmp_path, registry):
    source = CountingAsyncChunks([b"a" * 1024] * 128)
    staged = await stream_upload_to_stage(
        source,
        "report.pdf",
        "application/pdf",
        tmp_path,
        registry,
        MaterialLimits(file_bytes=256 * 1024),
    )
    assert source.max_requested_chunk <= 64 * 1024
    assert staged.byte_size == 128 * 1024
    assert staged.sha256 == hashlib.sha256(b"a" * (128 * 1024)).hexdigest()
    assert staged.path.is_file()


@pytest.mark.asyncio
async def test_oversize_stage_removes_only_its_temporary_file(tmp_path, registry):
    with pytest.raises(MaterialTooLarge):
        await stream_upload_to_stage(
            async_chunks([b"x" * 60_000]),
            "a.docx",
            DOCX_MIME,
            tmp_path,
            registry,
            MaterialLimits(file_bytes=50_000),
        )
    assert list(tmp_path.rglob("*")) == []
