from __future__ import annotations

import subprocess
import sys


def test_help_subprocess_from_unrelated_cwd(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "yande_sync.cli", "--help"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "{sync,verify,status,config,query}" in result.stdout
    for removed in ("doctor", "add-tag", "check", "audit", "download", "history"):
        assert f"\n    {removed} " not in result.stdout


def test_unicode_config_output_can_be_redirected(tmp_path):
    config_path = tmp_path / "应用" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """[network]
proxy = "http://127.0.0.1:10090"
require_proxy = true
allow_direct = false
[storage]
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "yande_sync.cli", "--config", str(config_path), "config"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0
    assert "应用" in result.stdout
