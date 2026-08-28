from __future__ import annotations

import hashlib
import sqlite3

import pytest

import yande_sync.cli as cli_module
from yande_sync.cli import main
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import SCHEMA_V2, Database
from yande_sync.doctor import DoctorResult
from yande_sync.locking import OperationLock, OperationLockError
from yande_sync.models import Post


def config_at(tmp_path, download_root=None):
    path = tmp_path / "app" / "config.toml"
    bootstrap_config(path)
    if download_root is not None:
        write_download_dir(path, download_root)
    return path


def add_downloaded(config_path, download_root, *, present=True):
    config = load_config(config_path)
    download_root.mkdir(parents=True, exist_ok=True)
    with Database(config.storage.database, download_root) as database:
        query = database.add_query("tag", "tag")
        post = Post(
            1, "1.jpg", "jpg", 1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
            "https://files.yande.re/1.jpg", "tag", "", None,
        )
        database.store_posts(query["query_id"], [post])
        database.set_materialization_status(
            query["query_id"], 1, "downloaded", relative_path="1.jpg"
        )
    if present:
        (download_root / "1.jpg").write_bytes(b"abc")
    return config


def create_v0_database(path, local_path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE posts (
         post_id INTEGER PRIMARY KEY,file_name TEXT NOT NULL,file_ext TEXT NOT NULL,
         width INTEGER NOT NULL,height INTEGER NOT NULL,file_size INTEGER NOT NULL,
         md5 TEXT NOT NULL,file_url TEXT NOT NULL,tags TEXT NOT NULL,
         source TEXT NOT NULL DEFAULT '',remote_created_at TEXT,first_seen_at TEXT NOT NULL,
         downloaded_at TEXT,local_path TEXT,status TEXT NOT NULL);
        CREATE TABLE queries (
         query_id INTEGER PRIMARY KEY AUTOINCREMENT,tag_query TEXT NOT NULL UNIQUE,
         folder_name TEXT NOT NULL,created_at TEXT NOT NULL,last_checked_at TEXT,
         highest_seen_post_id INTEGER,enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE query_posts (
         query_id INTEGER NOT NULL REFERENCES queries(query_id),
         post_id INTEGER NOT NULL REFERENCES posts(post_id),first_seen_at TEXT NOT NULL,
         PRIMARY KEY(query_id,post_id));
        CREATE TABLE runs (
         run_id INTEGER PRIMARY KEY AUTOINCREMENT,command TEXT NOT NULL,tag_query TEXT,
         started_at TEXT NOT NULL,finished_at TEXT,pages_requested INTEGER NOT NULL DEFAULT 0,
         posts_received INTEGER NOT NULL DEFAULT 0,new_count INTEGER NOT NULL DEFAULT 0,
         downloaded_count INTEGER NOT NULL DEFAULT 0,failed_count INTEGER NOT NULL DEFAULT 0,
         result TEXT NOT NULL DEFAULT 'running');
        CREATE TABLE download_events (
         event_id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL REFERENCES posts(post_id),
         attempt INTEGER NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
         bytes_received INTEGER NOT NULL DEFAULT 0,expected_size INTEGER NOT NULL,
         expected_md5 TEXT NOT NULL,actual_md5 TEXT,result TEXT NOT NULL,
         error_type TEXT,error_message TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
        """
    )
    connection.execute(
        """INSERT INTO posts VALUES(
        1,'1.jpg','jpg',1,1,3,'900150983cd24fb0d6963f7d28e17f72',
        'https://files.yande.re/1.jpg','tag','',NULL,'now','now',?,'downloaded')""",
        (str(local_path),),
    )
    connection.commit()
    connection.close()


def test_query_management_and_read_only_listing(tmp_path, capsys):
    config_path = config_at(tmp_path)
    assert main(["--config", str(config_path), "query", "add", "tag"]) == 0
    assert main(["--config", str(config_path), "query", "disable", "tag"]) == 0
    assert main(["--config", str(config_path), "query"]) == 0
    output = capsys.readouterr().out
    assert "[1] tag [disabled] Sources: tag" in output


def test_query_listing_shows_folders_and_collection_local_sources(tmp_path, capsys):
    config_path = config_at(tmp_path)
    assert main([
        "--config", str(config_path), "query", "add", "source_a", "source_a_prime"
    ]) == 0
    assert main(["--config", str(config_path), "query", "add", "source_b"]) == 0
    config = load_config(config_path)
    with Database(config.storage.database) as database, database.transaction() as connection:
        connection.execute(
            "UPDATE collections SET folder_name='Custom A' WHERE collection_id=1"
        )
        connection.execute(
            "UPDATE collections SET folder_name='Custom B' WHERE collection_id=2"
        )
    capsys.readouterr()

    assert main(["--config", str(config_path), "query"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "[1] Custom A [enabled] Sources: source_a, source_a_prime",
        "[2] Custom B [enabled] Sources: source_b",
    ]


@pytest.mark.parametrize(
    "tags",
    [
        ["tag_a"],
        ["tag_a", "tag_b"],
        ["tag_a", "tag_b", "tag_c"],
    ],
)
def test_query_add_creates_one_collection_with_requested_sources(tmp_path, capsys, tags):
    config_path = config_at(tmp_path)

    assert main(["--config", str(config_path), "query", "add", *tags]) == 0

    config = load_config(config_path)
    with Database(config.storage.database, read_only=True) as database:
        collections = database.list_collections()
        assert len(collections) == 1
        assert [
            row["tag_query"]
            for row in database.collection_sources(collections[0]["collection_id"])
        ] == tags
    output = capsys.readouterr().out
    assert output.count("Added collection") == 1


def test_query_add_exact_duplicates_are_idempotent(tmp_path, capsys):
    config_path = config_at(tmp_path)

    assert main([
        "--config", str(config_path), "query", "add", "tag_a", "tag_a"
    ]) == 0

    config = load_config(config_path)
    with Database(config.storage.database, read_only=True) as database:
        collections = database.list_collections()
        assert len(collections) == 1
        assert [row["tag_query"] for row in database.collection_sources(1)] == ["tag_a"]
    assert capsys.readouterr().out.count("Added collection") == 1


def test_query_add_exact_ordered_readd_preserves_existing_collection_state(tmp_path, capsys):
    config_path = config_at(tmp_path)
    assert main([
        "--config", str(config_path), "query", "add", "artist_a", "artist_b"
    ]) == 0
    capsys.readouterr()

    config = load_config(config_path)
    expected_state = {
        "enabled": 0,
        "folder_name": "existing-folder",
        "created_at": "2025-01-02T03:04:05+00:00",
    }
    with Database(config.storage.database) as database, database.transaction() as connection:
        connection.execute(
            """UPDATE collections SET enabled=?,folder_name=?,created_at=?
            WHERE collection_id=1""",
            tuple(expected_state.values()),
        )
        connection.execute(
            """UPDATE collection_sources SET last_checked_at=?,highest_seen_post_id=?
            WHERE collection_id=1 AND position=0""",
            ("2025-06-07T08:09:10+00:00", 123456),
        )

    assert main([
        "--config", str(config_path), "query", "add", "artist_a", "artist_b"
    ]) == 0

    with Database(config.storage.database, read_only=True) as database:
        existing = database.get_collection(1)
        assert existing is not None
        assert {field: existing[field] for field in expected_state} == expected_state
        assert len(database.list_collections()) == 1
        sources = database.collection_sources(1)
        assert [row["tag_query"] for row in sources] == ["artist_a", "artist_b"]
        assert (sources[0]["last_checked_at"], sources[0]["highest_seen_post_id"]) == (
            "2025-06-07T08:09:10+00:00", 123456,
        )
    assert capsys.readouterr().out.count("Added collection") == 1


def test_query_add_invalid_batch_creates_no_queries(tmp_path, capsys):
    config_path = config_at(tmp_path)

    assert main([
        "--config", str(config_path), "query", "add", "tag_a", "bad\x1btag", "tag_c"
    ]) == 2

    config = load_config(config_path)
    with Database(config.storage.database, read_only=True) as database:
        assert database.list_collections() == []
    assert "TAG" in capsys.readouterr().err


def test_query_add_to_updates_existing_collection_without_touching_state(tmp_path, capsys):
    download_root = tmp_path / "library"
    config_path = config_at(tmp_path, download_root)
    assert main(["--config", str(config_path), "query", "add", "artist_a"]) == 0
    config = load_config(config_path)
    download_root.mkdir()
    existing_file = download_root / "custom" / "kept.jpg"
    existing_file.parent.mkdir()
    existing_file.write_bytes(b"kept")
    with Database(config.storage.database, download_root) as database:
        post = Post(
            1, "kept.jpg", "jpg", 1, 1, 4, "18ccf61d533b600bbf5a963359223fe4",
            "https://files.yande.re/kept.jpg", "artist_a", "", None,
        )
        database.store_posts(1, [post])
        database.set_materialization_status(1, 1, "downloaded", relative_path="custom/kept.jpg")
        with database.transaction() as connection:
            connection.execute(
                "UPDATE collections SET folder_name='custom',folder_finalized=1 WHERE collection_id=1"
            )
            connection.execute(
                """UPDATE collection_sources SET last_checked_at='checked',
                highest_seen_post_id=99 WHERE collection_id=1"""
            )

    assert main([
        "--config", str(config_path), "query", "add", "--to", "1", "artist_b", "artist_a"
    ]) == 0

    with Database(config.storage.database, download_root, read_only=True) as database:
        assert len(database.list_collections()) == 1
        collection = database.get_collection(1)
        assert collection["folder_name"] == "custom"
        sources = database.collection_sources(1)
        assert [row["tag_query"] for row in sources] == ["artist_a", "artist_b"]
        assert (sources[0]["last_checked_at"], sources[0]["highest_seen_post_id"]) == (
            "checked", 99,
        )
        assert (sources[1]["last_checked_at"], sources[1]["highest_seen_post_id"]) == (
            None, None,
        )
        assert database.collection_posts(1)[0]["relative_path"] == str(
            __import__("pathlib").Path("custom") / "kept.jpg"
        )
    assert existing_file.read_bytes() == b"kept"
    output = capsys.readouterr().out
    assert "Added sources:" in output and "Already present:" in output


def test_query_add_to_invalid_batch_and_missing_collection_are_atomic(tmp_path):
    config_path = config_at(tmp_path)
    assert main(["--config", str(config_path), "query", "add", "base"]) == 0
    assert main([
        "--config", str(config_path), "query", "add", "--to", "1",
        "valid", "bad\x1btag",
    ]) == 2
    assert main([
        "--config", str(config_path), "query", "add", "--to", "99", "other"
    ]) == 2
    config = load_config(config_path)
    with Database(config.storage.database, read_only=True) as database:
        assert [row["tag_query"] for row in database.collection_sources(1)] == ["base"]


def test_status_remains_available_while_mutation_lock_is_held(tmp_path, capsys):
    config_path = config_at(tmp_path)
    assert main(["--config", str(config_path), "query", "add", "tag"]) == 0
    config = load_config(config_path)
    with OperationLock(config.storage.operation_lock):
        assert main(["--config", str(config_path), "status"]) == 0
    assert "Known remote posts: 0" in capsys.readouterr().out


@pytest.mark.parametrize("healthy,exit_code", [(True, 0), (False, 1)])
def test_status_doctor_exit_reflects_health(tmp_path, monkeypatch, capsys,
                                            healthy, exit_code):
    config_path = config_at(tmp_path)
    monkeypatch.setattr(
        cli_module, "run_doctor",
        lambda _config: DoctorResult(healthy, [("check", healthy, "detail")]),
    )

    assert main(["--config", str(config_path), "status", "--doctor"]) == exit_code
    output = capsys.readouterr().out
    assert ("[OK]" if healthy else "[FAIL]") in output


def test_config_set_reports_and_refuses_missing_noninteractive(tmp_path, capsys):
    old_root = tmp_path / "old"
    new_root = tmp_path / "empty"
    config_path = config_at(tmp_path, old_root)
    add_downloaded(config_path, old_root)

    assert main([
        "--config", str(config_path), "config", "set", "download-dir", str(new_root)
    ]) == 2
    captured = capsys.readouterr()
    assert "Tracked files: 1" in captured.out
    assert "Missing files: 1" in captured.out
    assert "unchanged" in captured.err
    assert load_config(config_path).storage.download_dir == old_root.resolve()
    assert not new_root.exists()
    assert not (config_path.parent / "operation.lock").exists()


def test_config_set_accept_missing_is_explicit(tmp_path, capsys):
    old_root = tmp_path / "old"
    new_root = tmp_path / "empty"
    config_path = config_at(tmp_path, old_root)
    add_downloaded(config_path, old_root)

    assert main([
        "--config", str(config_path), "config", "set", "download-dir", str(new_root),
        "--accept-missing",
    ]) == 0
    assert load_config(config_path).storage.download_dir == new_root.resolve()
    assert "Missing files: 1" in capsys.readouterr().out


def test_verify_updates_state_without_downloading(tmp_path, capsys):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    config = add_downloaded(config_path, root, present=False)

    assert main(["--config", str(config_path), "verify"]) == 1
    with Database(config.storage.database, root, read_only=True) as database:
        status = database.connection.execute(
            "SELECT status FROM collection_posts WHERE post_id=1"
        ).fetchone()[0]
    assert status == "missing"
    assert "[MISSING] 1.jpg" in capsys.readouterr().out


def test_repeated_verify_keeps_missing_file_as_problem(tmp_path, capsys):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    config = add_downloaded(config_path, root, present=False)

    assert main(["--config", str(config_path), "verify"]) == 1
    capsys.readouterr()
    assert main(["--config", str(config_path), "verify"]) == 1
    output = capsys.readouterr().out

    with Database(config.storage.database, root, read_only=True) as database:
        status = database.connection.execute(
            "SELECT status FROM collection_posts WHERE post_id=1"
        ).fetchone()[0]
    assert status == "missing"
    assert "[MISSING] 1.jpg" in output
    assert "problems=1" in output
    assert "yande-sync sync" in output


def test_repeated_verify_keeps_corrupt_file_as_problem(tmp_path, capsys):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    config = add_downloaded(config_path, root)
    (root / "1.jpg").write_bytes(b"bad")

    assert main(["--config", str(config_path), "verify"]) == 1
    capsys.readouterr()
    assert main(["--config", str(config_path), "verify"]) == 1
    output = capsys.readouterr().out

    with Database(config.storage.database, root, read_only=True) as database:
        status = database.connection.execute(
            "SELECT status FROM collection_posts WHERE post_id=1"
        ).fetchone()[0]
    assert status == "corrupt"
    assert "[CORRUPT] 1.jpg" in output
    assert "problems=1" in output


@pytest.mark.parametrize("initial_problem", ["missing", "corrupt"])
def test_verify_restored_valid_file_returns_to_downloaded(
    tmp_path, capsys, initial_problem
):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    config = add_downloaded(config_path, root, present=initial_problem == "corrupt")
    if initial_problem == "corrupt":
        (root / "1.jpg").write_bytes(b"bad")

    assert main(["--config", str(config_path), "verify"]) == 1
    (root / "1.jpg").write_bytes(b"abc")
    capsys.readouterr()
    assert main(["--config", str(config_path), "verify"]) == 0

    with Database(config.storage.database, root, read_only=True) as database:
        status = database.connection.execute(
            "SELECT status FROM collection_posts WHERE post_id=1"
        ).fetchone()[0]
    assert status == "downloaded"
    assert "Verify complete: ok=1 problems=0" in capsys.readouterr().out


def test_repeated_healthy_verify_remains_clean(tmp_path, capsys):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    add_downloaded(config_path, root)

    assert main(["--config", str(config_path), "verify"]) == 0
    capsys.readouterr()
    assert main(["--config", str(config_path), "verify"]) == 0
    output = capsys.readouterr().out

    assert "Verify complete: ok=1 problems=0" in output
    assert "yande-sync sync" not in output


def test_verify_isolates_duplicate_post_materializations_and_status_counts(tmp_path, capsys):
    root = tmp_path / "pictures"
    root.mkdir()
    config_path = config_at(tmp_path, root)
    config = load_config(config_path)
    with Database(config.storage.database, root) as database:
        first = database.add_query("one", "one")
        second = database.add_query("two", "two")
        post = Post(
            1, "1.jpg", "jpg", 1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
            "https://files.yande.re/1.jpg", "tag", "", None,
        )
        database.store_posts(first["query_id"], [post])
        database.store_posts(second["query_id"], [post])
        first_path = root / "one" / "1.jpg"
        second_path = root / "two" / "1.jpg"
        first_path.parent.mkdir()
        second_path.parent.mkdir()
        first_path.write_bytes(b"bad")
        second_path.write_bytes(b"abc")
        database.set_materialization_status(
            first["query_id"], 1, "downloaded", local_path=first_path
        )
        database.set_materialization_status(
            second["query_id"], 1, "downloaded", local_path=second_path
        )

        assert cli_module.do_verify(config, database, None) == (1, 1)
        states = database.connection.execute(
            """SELECT collection_id,status FROM collection_posts
            ORDER BY collection_id"""
        ).fetchall()
        counts = database.status_counts()
    assert [tuple(row) for row in states] == [
        (first["query_id"], "corrupt"), (second["query_id"], "downloaded")
    ]
    assert counts["total"] == 1 and counts["materializations"] == 2
    assert "[CORRUPT] 1.jpg" in capsys.readouterr().out


def test_query_scoped_verify_does_not_change_other_materialization(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    config_path = config_at(tmp_path, root)
    config = load_config(config_path)
    with Database(config.storage.database, root) as database:
        first = database.add_query("one", "one")
        second = database.add_query("two", "two")
        item = Post(
            1, "1.jpg", "jpg", 1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
            "https://files.yande.re/1.jpg", "tag", "", None,
        )
        database.store_posts(first["query_id"], [item])
        database.store_posts(second["query_id"], [item])
        first_path = root / "one" / "1.jpg"
        second_path = root / "two" / "1.jpg"
        first_path.parent.mkdir()
        second_path.parent.mkdir()
        first_path.write_bytes(b"bad")
        second_path.write_bytes(b"abc")
        database.set_materialization_status(
            first["query_id"], 1, "downloaded", local_path=first_path
        )
        database.set_materialization_status(
            second["query_id"], 1, "pending", local_path=second_path
        )

        assert cli_module.do_verify(config, database, "one") == (0, 1)
        states = database.connection.execute(
            """SELECT collection_id,status FROM collection_posts
            ORDER BY collection_id"""
        ).fetchall()

    assert [tuple(row) for row in states] == [
        (first["query_id"], "corrupt"), (second["query_id"], "pending")
    ]


def test_verify_reports_initial_count_and_periodic_progress(tmp_path, capsys):
    root = tmp_path / "pictures"
    root.mkdir()
    config_path = config_at(tmp_path, root)
    config = load_config(config_path)
    data = b"a"
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    with Database(config.storage.database, root) as database:
        collection = database.add_query("tag", "tag")
        posts = [
            Post(
                post_id, f"{post_id}.jpg", "jpg", 1, 1, 1, digest,
                f"https://files.yande.re/{post_id}.jpg", "tag", "", None,
            )
            for post_id in range(1, 206)
        ]
        database.store_posts(collection["query_id"], posts)
        folder = root / "tag"
        folder.mkdir()
        for post in posts:
            path = folder / post.file_name
            path.write_bytes(data)
            database.set_materialization_status(
                collection["query_id"], post.post_id, "downloaded", local_path=path
            )

        assert cli_module.do_verify(config, database, None) == (205, 0)

    output = capsys.readouterr().out
    assert "Verifying 205 files..." in output
    assert "Verifying 100/205..." in output
    assert "Verifying 200/205..." in output
    assert "Verifying 1/205..." not in output


def test_disabling_query_preserves_materialization_file(tmp_path):
    root = tmp_path / "pictures"
    config_path = config_at(tmp_path, root)
    assert main(["--config", str(config_path), "query", "add", "tag"]) == 0
    folder = root / "tag"
    folder.mkdir(parents=True)
    image = folder / "1.jpg"
    image.write_bytes(b"abc")
    assert main(["--config", str(config_path), "query", "disable", "tag"]) == 0
    assert image.read_bytes() == b"abc"


def test_invalid_config_set_creates_no_runtime_state(tmp_path, capsys):
    config_path = tmp_path / "app" / "config.toml"
    assert main([
        "--config", str(config_path), "config", "set", "download-dir", "relative"
    ]) == 2
    assert "absolute" in capsys.readouterr().err
    assert not config_path.parent.exists()


def test_failed_mutation_lock_creates_no_runtime_state(tmp_path, monkeypatch, capsys):
    config_path = config_at(tmp_path)
    before = config_path.read_bytes()

    def fail_lock(_self):
        raise OperationLockError("held")

    monkeypatch.setattr(OperationLock, "__enter__", fail_lock)
    assert main(["--config", str(config_path), "query", "add", "tag"]) == 1
    assert "held" in capsys.readouterr().err
    assert config_path.read_bytes() == before
    assert not (config_path.parent / "data").exists()
    assert not (config_path.parent / "logs").exists()
    assert not (config_path.parent / "temp").exists()


def test_declined_config_set_does_not_migrate_v0_database(tmp_path, capsys):
    old_root = tmp_path / "old"
    old_root.mkdir()
    config_path = config_at(tmp_path, old_root)
    database_path = config_path.parent / "data" / "yande-sync.db"
    create_v0_database(database_path, old_root / "1.jpg")
    before_config = config_path.read_bytes()

    assert main([
        "--config", str(config_path), "config", "set", "download-dir", str(tmp_path / "empty")
    ]) == 2
    capsys.readouterr()
    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "local_path" in {
        row[1] for row in connection.execute("PRAGMA table_info(posts)")
    }
    connection.close()
    assert config_path.read_bytes() == before_config
    assert not (config_path.parent / "operation.lock").exists()
    assert not (config_path.parent / "logs").exists()
    assert not (config_path.parent / "temp").exists()


@pytest.mark.parametrize("version", [0, 1])
def test_old_schema_read_only_commands_do_not_migrate_or_write(tmp_path, version, capsys):
    root = tmp_path / "pictures"
    root.mkdir()
    config_path = config_at(tmp_path, root)
    database_path = config_path.parent / "data" / "yande-sync.db"
    if version == 0:
        create_v0_database(database_path, root / "1.jpg")
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.executescript(SCHEMA_V2)
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
    before_config = config_path.read_bytes()
    before_database = database_path.read_bytes()
    lock_path = config_path.parent / "operation.lock"

    with OperationLock(lock_path):
        assert main(["--config", str(config_path), "status"]) == 0
        assert main(["--config", str(config_path), "status", "--history", "5"]) == 0
        assert main(["--config", str(config_path), "query"]) == 0
    output = capsys.readouterr().out
    assert "migration required" in output
    assert config_path.read_bytes() == before_config
    assert database_path.read_bytes() == before_database
    assert not (config_path.parent / "logs").exists()
    assert not (config_path.parent / "temp").exists()
