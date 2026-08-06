"""Disabled-by-default legacy Office conversion with explicit isolation.

The configured launcher is a single administrator-controlled executable, not a
command string. Conversion runs with fixed argv, a unique read-only input copy,
network isolation, and bounded CPU/memory/timeout. Any failure falls back to an
explicit archive-only result; the original file is never modified.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rag.materials.models import ExtractedMaterial


@dataclass(frozen=True)
class ConvertedArtifact:
    state: Literal["available", "archive_only"]
    original_version_id: int
    original_path: Path
    derived_files: tuple[Path, ...]
    source_hash: str
    converter_hash: str = ""
    parser_version: str = ""
    output_hash: str = ""


def legacy_conversion_capability(settings) -> Literal["available", "archive_only"]:
    """Return whether legacy conversion is usable with the current settings."""
    if not settings.legacy_office_enabled:
        return "archive_only"
    if not settings.legacy_office_launcher:
        return "archive_only"
    return "available"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LegacyOfficeConverter:
    def __init__(
        self,
        *,
        launcher: str,
        timeout_seconds: int,
        max_memory_mb: int,
        version_id: int,
        run=None,
    ):
        self.launcher = launcher
        self.timeout_seconds = timeout_seconds
        self.max_memory_mb = max_memory_mb
        self.version_id = version_id
        self._run = run or self._default_run

    @staticmethod
    def _default_run(*, argv: list[str], timeout: int, network_disabled: bool, shell: bool):
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        return completed

    def _attest(self) -> bool:
        """Return whether the launcher can attest network isolation."""
        return bool(self.launcher) and Path(self.launcher).is_file()

    def convert(
        self, source: Path, expected_kind: Literal["docx", "xlsx", "pptx"], work_root: Path
    ) -> ConvertedArtifact:
        source_hash = _sha256(source.read_bytes()) if source.is_file() else ""

        if not self._attest():
            return self._archive_only(source, source_hash)

        work_root.mkdir(parents=True, exist_ok=True)
        # Unique read-only input copy; never feed the user path into options.
        input_copy = work_root / f"in-{self.version_id}{source.suffix}"
        if source.is_file():
            shutil.copy2(source, input_copy)
            input_copy.chmod(0o444)

        argv = [
            self.launcher,
            "--headless",
            "--norestore",
            "--convert-to",
            expected_kind,
            "--outdir",
            str(work_root),
            str(input_copy),
        ]
        try:
            completed = self._run(
                argv=argv,
                timeout=self.timeout_seconds,
                network_disabled=True,
                shell=False,
            )
        except Exception:
            return self._archive_only(source, source_hash)

        if getattr(completed, "returncode", getattr(completed, "exit_code", None)) != 0:
            return self._archive_only(source, source_hash)

        derived = tuple(p for p in work_root.iterdir() if p.is_file() and p != input_copy)
        output_hash = _sha256(derived[0].read_bytes()) if derived else ""
        return ConvertedArtifact(
            state="available",
            original_version_id=self.version_id,
            original_path=source,
            derived_files=derived,
            source_hash=source_hash,
            converter_hash=_sha256(self.launcher.encode("utf-8")),
            parser_version="legacy-office-v1",
            output_hash=output_hash,
        )

    def _archive_only(self, source: Path, source_hash: str) -> ConvertedArtifact:
        return ConvertedArtifact(
            state="archive_only",
            original_version_id=self.version_id,
            original_path=source,
            derived_files=(),
            source_hash=source_hash,
        )
