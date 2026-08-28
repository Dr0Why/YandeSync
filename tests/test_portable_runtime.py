from __future__ import annotations

from pathlib import Path

import pytest

import yande_sync.config as config_module
from yande_sync.config import (
    ConfigError,
    assess_download_dir,
    bootstrap_config,
    change_download_dir,
    discover_runtime_root,
    load_config,
    write_download_dir,
)
from yande_sync.database import Database
from yande_sync.models import Post
from yande_sync.security import SecurityError, safe_library_path, validate_download_root


def configured(tmp_path, download_root):
    config_path = tmp_path / "app" / "config.toml"
    bootstrap_config(config_path)
    write_download_dir(config_path, download_root)
    return load_config(config_path)


def tracked_database(config, old_root):
    old_root.mkdir(parents=True, exist_ok=True)
    database = Database(config.storage.database, old_root)
    query = database.add_query("tag", "tag")
    post = Post(
        1, "1.jpg", "jpg", 1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    database.store_posts(query["query_id"], [post])
    database.set_materialization_status(
        query["query_id"], 1, "downloaded", relative_path="1.jpg"
    )
    return database


def test_runtime_modes_are_explicit_and_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_module.sys, "executable", str(tmp_path / "moved" / "yande-sync.exe"))
    assert discover_runtime_root() == ((tmp_path / "moved").resolve(), "frozen")

    monkeypatch.delattr(config_module.sys, "frozen", raising=False)
    fake_package = tmp_path / "site" / "yande_sync" / "config.py"
    monkeypatch.setattr(config_module, "__file__", str(fake_package))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.chdir(tmp_path)
    assert discover_runtime_root() == ((tmp_path / "local" / "YandeSync").resolve(), "installed")


def test_frozen_runtime_does_not_dereference_executable_path(tmp_path, monkeypatch):
    target = tmp_path / "physical" / "yande-sync.exe"
    target.parent.mkdir()
    target.write_bytes(b"exe")
    launcher_dir = tmp_path / "portable-link"
    launcher_dir.mkdir()
    launcher = launcher_dir / "yande-sync.exe"
    try:
        launcher.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_module.sys, "executable", str(launcher))

    assert discover_runtime_root() == (launcher_dir, "frozen")


def test_source_runtime_ignores_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root, mode = discover_runtime_root()
    assert mode == "source"
    assert root == Path(__file__).resolve().parents[1]


def test_relative_download_root_and_escape_are_rejected(tmp_path):
    with pytest.raises(SecurityError, match="absolute"):
        validate_download_root(Path("relative"))
    root = tmp_path / "pictures"
    root.mkdir()
    with pytest.raises(SecurityError, match="escapes"):
        safe_library_path(root, "..\\outside.jpg")


@pytest.mark.parametrize(
    "relative",
    ["1.jpg:evil", "CON", "aux.txt", "CON .txt", "COM1 .foo", "aux .txt", "name.",
     "name ", "bad\x00.jpg", "folder\\NUL.jpg"],
)
def test_windows_ambiguous_relative_paths_are_rejected(tmp_path, relative):
    root = tmp_path / "pictures"
    root.mkdir()
    with pytest.raises(SecurityError, match="Windows"):
        safe_library_path(root, relative)


def test_in_root_links_are_not_resolved_away(tmp_path):
    root = tmp_path / "pictures"
    inner = root / "inner"
    inner.mkdir(parents=True)
    target = inner / "target.jpg"
    target.write_bytes(b"abc")
    file_link = root / "linked.jpg"
    directory_link = root / "linked-directory"
    try:
        file_link.symlink_to(target)
        directory_link.symlink_to(inner, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SecurityError, match="links or reparse"):
        safe_library_path(root, "linked.jpg")
    with pytest.raises(SecurityError, match="links or reparse"):
        safe_library_path(root, "linked-directory\\target.jpg")


def test_complete_download_dir_change_is_committed_without_row_rewrite(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    config = configured(tmp_path, old_root)
    with tracked_database(config, old_root) as database:
        new_root.mkdir()
        (new_root / "1.jpg").write_bytes(b"abc")
        before = database.connection.execute(
            "SELECT relative_path,status FROM collection_posts WHERE post_id=1"
        ).fetchone()
        _changed, assessment = change_download_dir(config, database, new_root)
        after = database.connection.execute(
            "SELECT relative_path,status FROM collection_posts WHERE post_id=1"
        ).fetchone()
    assert (assessment.tracked, assessment.found, assessment.missing) == (1, 1, 0)
    assert tuple(before) == tuple(after)
    assert load_config(config.source_path).storage.download_dir == new_root.resolve()


def test_incomplete_download_dir_defaults_to_no_change(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "empty"
    config = configured(tmp_path, old_root)
    with tracked_database(config, old_root) as database:
        with pytest.raises(ConfigError, match="unchanged"):
            change_download_dir(config, database, new_root, interactive=False)
        with pytest.raises(ConfigError, match="unchanged"):
            change_download_dir(
                config, database, new_root, interactive=True, confirm=lambda _prompt: ""
            )
        assert database.connection.execute(
            "SELECT relative_path FROM collection_posts WHERE post_id=1"
        ).fetchone()[0] == "1.jpg"
    assert load_config(config.source_path).storage.download_dir == old_root.resolve()
    assert not new_root.exists()


def test_accept_missing_is_narrow_explicit_override(tmp_path):
    old_root = tmp_path / "old"
    new_root = tmp_path / "empty"
    config = configured(tmp_path, old_root)
    with tracked_database(config, old_root) as database:
        changed, assessment = change_download_dir(
            config, database, new_root, accept_missing=True
        )
    assert assessment.missing == 1
    assert changed.storage.download_dir == new_root.resolve()
    assert new_root.is_dir()


def test_download_dir_assessment_counts_query_materializations(tmp_path):
    root = tmp_path / "pictures"
    candidate = tmp_path / "candidate"
    root.mkdir()
    candidate.mkdir()
    with Database(tmp_path / "state.db", root) as database:
        first = database.add_query("one", "one")
        second = database.add_query("two", "two")
        post = Post(
            1, "1.jpg", "jpg", 1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
            "https://files.yande.re/1.jpg", "tag", "", None,
        )
        database.store_posts(first["query_id"], [post])
        database.store_posts(second["query_id"], [post])
        database.set_materialization_status(
            first["query_id"], 1, "downloaded", relative_path="one/1.jpg"
        )
        database.set_materialization_status(
            second["query_id"], 1, "downloaded", relative_path="two/1.jpg"
        )
        (candidate / "one").mkdir()
        (candidate / "one" / "1.jpg").write_bytes(b"abc")
        assessment = assess_download_dir(database, candidate)
    assert (assessment.tracked, assessment.found, assessment.missing) == (2, 1, 1)
