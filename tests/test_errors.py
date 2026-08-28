from __future__ import annotations

import sqlite3

from yande_sync.cli import main
from yande_sync.config import bootstrap_config


def test_user_input_error_is_concise_exit_2(tmp_path, capsys):
    config_path = tmp_path / "app" / "config.toml"
    bootstrap_config(config_path)
    assert main([
        "--config", str(config_path), "config", "set", "download-dir", "relative"
    ]) == 2
    captured = capsys.readouterr()
    assert "absolute" in captured.err
    assert "Traceback" not in captured.err


def test_unexpected_read_only_error_does_not_write_log(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "app" / "config.toml"
    bootstrap_config(config_path)

    def fail(*_args, **_kwargs):
        raise AssertionError("unexpected detail")

    monkeypatch.setattr("yande_sync.cli.print_config", fail)
    assert main(["--config", str(config_path), "config"]) == 1
    captured = capsys.readouterr()
    assert "Unexpected internal error" in captured.err
    assert "Traceback" not in captured.err
    assert not (config_path.parent / "logs").exists()


def test_read_only_sqlite_error_is_exit_1_without_log_write(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "app" / "config.toml"
    bootstrap_config(config_path)

    def fail(_config):
        raise sqlite3.DatabaseError("database unavailable")

    monkeypatch.setattr("yande_sync.cli.read_only_database", fail)
    assert main(["--config", str(config_path), "status"]) == 1
    captured = capsys.readouterr()
    assert "database unavailable" in captured.err
    assert "Traceback" not in captured.err
    assert not (config_path.parent / "logs").exists()


def test_unexpected_mutation_error_logs_traceback_under_lock(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "app" / "config.toml"
    bootstrap_config(config_path)

    def fail(*_args, **_kwargs):
        raise AssertionError("mutation detail")

    monkeypatch.setattr("yande_sync.cli.ensure_collection", fail)
    assert main(["--config", str(config_path), "query", "add", "tag"]) == 1
    captured = capsys.readouterr()
    assert "Unexpected internal error" in captured.err
    assert "Traceback" not in captured.err
    log = (config_path.parent / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "Traceback" in log
    assert "mutation detail" in log
