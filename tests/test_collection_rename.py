from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import yande_sync.collection_rename as rename_module
from yande_sync.cli import main, parser
from yande_sync.collection_rename import (
    CollectionRenameError,
    journal_path,
    recover_collection_rename,
    rename_collection_folder,
)
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import Database
from yande_sync.models import Post
from yande_sync.security import SecurityError, validate_collection_folder_name


def _post(post_id: int, name: str) -> Post:
    return Post(
        post_id=post_id,
        file_name=name,
        file_ext=Path(name).suffix.lstrip("."),
        width=1,
        height=1,
        file_size=1,
        md5="0" * 32,
        file_url="https://files.yande.re/image/test.jpg",
        tags="tag",
        source="yande.re",
        remote_created_at="now",
    )


def _seed(db, root: Path):
    root.mkdir()
    collection = db.add_collection(["one", "two"], "A")
    first, second = db.collection_sources(int(collection["collection_id"]))
    db.store_source_posts(int(first["source_id"]), 1, [_post(100, "100.jpg")])
    db.store_source_posts(int(second["source_id"]), 1, [_post(101, "101.png")])
    old = root / "A"
    (old / "sub").mkdir(parents=True)
    (old / "100.jpg").write_bytes(b"first-image")
    (old / "sub" / "101.png").write_bytes(b"second-image")
    db.set_materialization_status(1, 100, "downloaded", local_path=old / "100.jpg")
    db.set_materialization_status(1, 101, "downloaded", local_path=old / "sub" / "101.png")
    return collection


def _journal(path: Path, phase: str = "prepared") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "operation": "collection_folder_rename",
                "collection_id": 1,
                "old_folder_name": "A",
                "new_folder_name": "New",
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )


def test_cli_parser_and_success_output(tmp_path, capsys):
    args = parser().parse_args(["query", "rename", "1", "New Name"])
    assert (args.collection, args.new_folder_name) == (1, "New Name")
    config_path = tmp_path / "app" / "config.toml"
    root = tmp_path / "downloads"
    bootstrap_config(config_path)
    write_download_dir(config_path, root)
    config = load_config(config_path)
    config.storage.create_directories()
    with Database(config.storage.database, root) as database:
        _seed(database, root)
    assert main(["--config", str(config_path), "query", "rename", "1", "New Name"]) == 0
    assert "Renamed collection 1:" in capsys.readouterr().out
    assert (root / "New Name" / "100.jpg").is_file()


def test_success_preserves_files_sources_membership_and_unrelated_collection(db, tmp_path):
    root = tmp_path / "downloads"
    _seed(db, root)
    other = db.add_collection(["other"], "Other")
    sources_before = [tuple(row) for row in db.collection_sources(1)]
    memberships_before = [
        tuple(row)
        for row in db.connection.execute(
            "SELECT collection_id,post_id,first_seen_at FROM collection_posts ORDER BY post_id"
        )
    ]
    path = journal_path(tmp_path)

    assert rename_collection_folder(db, root, path, 1, "A A'") == ("A", "A A'")

    assert not (root / "A").exists()
    assert (root / "A A'" / "100.jpg").read_bytes() == b"first-image"
    assert (root / "A A'" / "sub" / "101.png").read_bytes() == b"second-image"
    assert db.get_collection(1)["folder_name"] == "A A'"
    assert db.get_collection(int(other["collection_id"]))["folder_name"] == "Other"
    assert [tuple(row) for row in db.collection_sources(1)] == sources_before
    assert [
        tuple(row)
        for row in db.connection.execute(
            "SELECT collection_id,post_id,first_seen_at FROM collection_posts ORDER BY post_id"
        )
    ] == memberships_before
    assert [
        row[0]
        for row in db.connection.execute(
            "SELECT relative_path FROM collection_posts WHERE collection_id=1 ORDER BY post_id"
        )
    ] == [str(Path("A A'") / "100.jpg"), str(Path("A A'") / "sub" / "101.png")]
    assert not path.exists()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "..\\ABC",
        "ABC\\DEF",
        "ABC/DEF",
        "C:\\ABC",
        "\\\\server\\share",
        "bad:name",
        "CON",
        "nul",
        "COM1",
        "ABC.",
        "ABC ",
    ],
)
def test_invalid_explicit_folder_names_are_rejected(name):
    with pytest.raises(SecurityError):
        validate_collection_folder_name(name)


def test_reparse_directory_decision_is_rejected_deterministically():
    class ReparseDirectory:
        def lstat(self):
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )

    with pytest.raises(CollectionRenameError, match="reparse point"):
        rename_module._directory(ReparseDirectory(), "old collection")


@pytest.mark.parametrize("new_name", ["a", "A"])
def test_case_only_or_identical_rename_is_rejected_without_mutation(db, tmp_path, new_name):
    root = tmp_path / "downloads"
    _seed(db, root)
    with pytest.raises(ValueError, match="case-only"):
        rename_collection_folder(db, root, journal_path(tmp_path), 1, new_name)
    assert (root / "A").is_dir()
    assert db.get_collection(1)["folder_name"] == "A"


def test_existing_destination_and_missing_old_fail_preflight(db, tmp_path):
    root = tmp_path / "downloads"
    _seed(db, root)
    (root / "New").mkdir()
    path = journal_path(tmp_path)
    with pytest.raises(ValueError, match="destination already exists"):
        rename_collection_folder(db, root, path, 1, "New")
    assert not path.exists() and (root / "A").is_dir()
    (root / "A").rename(root / "Gone")
    with pytest.raises(CollectionRenameError, match="missing"):
        rename_collection_folder(db, root, path, 1, "Another")
    assert not path.exists() and db.get_collection(1)["folder_name"] == "A"


def test_inconsistent_persisted_path_fails_before_mutation(db, tmp_path):
    root = tmp_path / "downloads"
    _seed(db, root)
    db.connection.execute(
        "UPDATE collection_posts SET relative_path=? WHERE collection_id=1 AND post_id=100",
        (str(Path("Other") / "100.jpg"),),
    )
    db.connection.commit()
    path = journal_path(tmp_path)
    with pytest.raises(CollectionRenameError, match="inconsistent"):
        rename_collection_folder(db, root, path, 1, "New")
    assert (root / "A").is_dir() and not (root / "New").exists() and not path.exists()


def test_database_failure_renames_filesystem_back_and_clears_journal(db, tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    _seed(db, root)
    path = journal_path(tmp_path)
    monkeypatch.setattr(
        rename_module,
        "_commit_database_rename",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("db failed")),
    )
    with pytest.raises(RuntimeError, match="db failed"):
        rename_collection_folder(db, root, path, 1, "New")
    assert (root / "A").is_dir() and not (root / "New").exists()
    assert db.get_collection(1)["folder_name"] == "A" and not path.exists()


def test_rollback_failure_keeps_journal(db, tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    _seed(db, root)
    path = journal_path(tmp_path)
    monkeypatch.setattr(
        rename_module,
        "_commit_database_rename",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("db failed")),
    )
    real_replace = rename_module.os.replace
    calls = 0

    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 4:  # prepared journal, folder rename, phase journal, then rollback
            raise OSError("rollback failed")
        return real_replace(source, destination)

    monkeypatch.setattr(rename_module.os, "replace", replace)
    with pytest.raises(CollectionRenameError, match="rollback failed"):
        rename_collection_folder(db, root, path, 1, "New")
    assert path.exists() and (root / "New").is_dir()


@pytest.mark.parametrize("state", ["old", "filesystem", "committed"])
def test_recovery_safe_states(db, tmp_path, state):
    root = tmp_path / "downloads"
    _seed(db, root)
    path = journal_path(tmp_path)
    _journal(path)
    if state == "filesystem":
        (root / "A").rename(root / "New")
    elif state == "committed":
        (root / "A").rename(root / "New")
        db.connection.execute("UPDATE collections SET folder_name='New' WHERE collection_id=1")
        db.connection.execute(
            "UPDATE collection_posts SET relative_path=? WHERE collection_id=1 AND post_id=100",
            (str(Path("New") / "100.jpg"),),
        )
        db.connection.execute(
            "UPDATE collection_posts SET relative_path=? WHERE collection_id=1 AND post_id=101",
            (str(Path("New") / "sub" / "101.png"),),
        )
        db.connection.commit()
    recover_collection_rename(db, root, path)
    assert not path.exists()
    expected = "New" if state == "committed" else "A"
    assert (root / expected).is_dir() and db.get_collection(1)["folder_name"] == expected


def test_recovery_ambiguous_fails_closed_and_keeps_journal(db, tmp_path):
    root = tmp_path / "downloads"
    _seed(db, root)
    (root / "New").mkdir()
    path = journal_path(tmp_path)
    _journal(path)
    with pytest.raises(CollectionRenameError, match="ambiguous"):
        recover_collection_rename(db, root, path)
    assert path.exists() and (root / "A").is_dir() and (root / "New").is_dir()
