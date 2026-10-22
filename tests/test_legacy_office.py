"""Task 4: disabled-by-default isolated legacy Office conversion."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from rag.materials.legacy_office import LegacyOfficeConverter, legacy_conversion_capability


@pytest.fixture
def settings():
    from config import settings

    return settings


@pytest.fixture
def converter_fixture(tmp_path):
    version_id = 9
    source = tmp_path / "policy.ppt"
    source.write_bytes(b"old ppt content")
    work_root = tmp_path / "out"

    class Runner:
        def __init__(self):
            self.calls = []
            self.fail_code = None

        def fail(self, exit_code):
            self.fail_code = exit_code

        def run(self, *, argv, timeout, network_disabled, shell):
            self.calls.append(
                {
                    "argv": argv,
                    "timeout": timeout,
                    "network_disabled": network_disabled,
                    "shell": shell,
                }
            )
            if self.fail_code is not None:
                return SimpleNamespace(exit_code=self.fail_code)
            out_dir = Path(argv[argv.index("--outdir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "policy.pptx").write_bytes(b"converted")
            return SimpleNamespace(exit_code=0)

    runner_obj = Runner()
    converter = LegacyOfficeConverter(
        launcher=str(tmp_path / "launcher.exe"),
        timeout_seconds=60,
        max_memory_mb=512,
        version_id=version_id,
        run=runner_obj.run,
    )
    # The launcher must exist to attest network isolation.
    (tmp_path / "launcher.exe").write_bytes(b"launcher")

    vid = version_id
    src_path = source
    wroot = work_root

    class Fixture:
        version_id = vid
        runner = runner_obj
        source = src_path
        work_root = wroot

        def convert(self, path: Path, expected_kind: str):
            return converter.convert(path, expected_kind, wroot)

        def run(self):
            return converter.convert(src_path, "pptx", wroot)

    return Fixture()


def test_legacy_conversion_is_archive_only_without_isolated_launcher(settings, monkeypatch):
    monkeypatch.setattr(settings, "legacy_office_enabled", False)
    assert legacy_conversion_capability(settings) == "archive_only"


def test_conversion_capability_requires_enabled_and_launcher(settings, monkeypatch):
    monkeypatch.setattr(settings, "legacy_office_enabled", True)
    monkeypatch.setattr(settings, "legacy_office_launcher", "")
    assert legacy_conversion_capability(settings) == "archive_only"


def test_converter_uses_fixed_argv_and_unique_readonly_input(converter_fixture):
    artifact = converter_fixture.convert(Path("policy.ppt"), expected_kind="pptx")
    call = converter_fixture.runner.calls[0]
    assert call["shell"] is False
    assert call["network_disabled"] is True
    assert "--convert-to" in call["argv"]
    assert artifact.original_version_id == converter_fixture.version_id
    assert artifact.state == "available"
    assert len(artifact.derived_files) == 1


def test_conversion_failure_keeps_original_archive_only(converter_fixture):
    converter_fixture.runner.fail(exit_code=2)
    result = converter_fixture.run()
    assert result.state == "archive_only"
    assert result.original_path.exists()
    assert result.derived_files == ()
