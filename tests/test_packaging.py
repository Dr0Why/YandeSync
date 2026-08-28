from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from yande_sync.config import default_config_text


def test_packaged_config_template_matches_bootstrap_defaults():
    packaged = files("yande_sync").joinpath("config.example.toml").read_text(encoding="utf-8")
    assert packaged == default_config_text()


def test_portable_spec_and_windows_ci_are_present():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "yande-sync.spec").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'name="YandeSync"' in spec
    assert "pyinstaller yande-sync.spec" in workflow
    assert "windows-latest" in workflow
