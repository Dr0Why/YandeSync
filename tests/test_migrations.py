from __future__ import annotations

import sqlite3
from hashlib import sha256

import pytest

import yande_sync.cli as cli_module
import yande_sync.database as database_module
from yande_sync.cli import open_writable_database
from yande_sync.config import bootstrap_config, load_config
from yande_sync.database import (
    MIGRATION_ROOT_KEY,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_VERSION,
    Database,
    DatabaseError,
    prepare_database,
)

LEGACY_SCHEMA = """
CREATE TABLE posts (
 post_id INTEGER PRIMARY KEY, file_name TEXT NOT NULL, file_ext TEXT NOT NULL,
 width INTEGER NOT NULL, height INTEGER NOT NULL, file_size INTEGER NOT NULL,
 md5 TEXT NOT NULL, file_url TEXT NOT NULL, tags TEXT NOT NULL,
 source TEXT NOT NULL DEFAULT '', remote_created_at TEXT, first_seen_at TEXT NOT NULL,
 downloaded_at TEXT, local_path TEXT,
 status TEXT NOT NULL CHECK(status IN
 ('new','pending','downloading','downloaded','failed','missing','corrupt')));
CREATE TABLE queries (
 query_id INTEGER PRIMARY KEY AUTOINCREMENT, tag_query TEXT NOT NULL UNIQUE,
 folder_name TEXT NOT NULL, created_at TEXT NOT NULL, last_checked_at TEXT,
 highest_seen_post_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE query_posts (
 query_id INTEGER NOT NULL REFERENCES queries(query_id),
 post_id INTEGER NOT NULL REFERENCES posts(post_id), first_seen_at TEXT NOT NULL,
 PRIMARY KEY(query_id,post_id));
CREATE TABLE runs (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, tag_query TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, pages_requested INTEGER NOT NULL DEFAULT 0,
 posts_received INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0,
 downloaded_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
 result TEXT NOT NULL DEFAULT 'running');
CREATE TABLE download_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL REFERENCES posts(post_id),
 attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 bytes_received INTEGER NOT NULL DEFAULT 0, expected_size INTEGER NOT NULL,
 expected_md5 TEXT NOT NULL, actual_md5 TEXT, result TEXT NOT NULL,
 error_type TEXT, error_message TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
"""


def test_v2_materialization_state_expands_to_every_query_without_touching_file(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    flat = root / "1.jpg"
    flat.write_bytes(b"abc")
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V2)
    connection.execute("PRAGMA user_version=2")
    connection.execute(
        """INSERT INTO posts VALUES(
        1,'1.jpg','jpg',1,1,3,'900150983cd24fb0d6963f7d28e17f72',
        'https://files.yande.re/1.jpg','tag','',NULL,'now','now','1.jpg',NULL,'downloaded')"""
    )
    connection.execute("INSERT INTO queries VALUES(1,'one','one','now',NULL,NULL,1)")
    connection.execute("INSERT INTO queries VALUES(2,'two','two','now',NULL,NULL,1)")
    connection.execute("INSERT INTO query_posts VALUES(1,1,'now')")
    connection.execute("INSERT INTO query_posts VALUES(2,1,'now')")
    connection.commit()
    connection.close()
    before = flat.read_bytes()
    backups = tmp_path / "backups"

    with Database(path, root, backup_dir=backups) as database:
        rows = database.connection.execute(
            """SELECT collection_id,status,relative_path,downloaded_at
            FROM collection_posts ORDER BY collection_id"""
        ).fetchall()
        post_columns = {
            row[1] for row in database.connection.execute("PRAGMA table_info(posts)")
        }
        assert database.schema_version() == SCHEMA_VERSION
    assert [tuple(row) for row in rows] == [
        (1, "downloaded", "1.jpg", "now"),
        (2, "downloaded", "1.jpg", "now"),
    ]
    assert "status" not in post_columns and "relative_path" not in post_columns
    assert flat.read_bytes() == before
    assert len(list(backups.glob("yande-sync-v2-*.db"))) == 1


def test_v3_to_v4_adds_local_artist_names_without_rebuilding_state(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    image = root / "tag" / "1.jpg"
    image.parent.mkdir()
    image.write_bytes(b"abc")
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V3)
    connection.execute("PRAGMA user_version=3")
    connection.execute(
        "INSERT INTO queries VALUES(1,'tag','tag',1,'now',NULL,NULL,1)"
    )
    connection.execute(
        """INSERT INTO posts VALUES(
        1,'1.jpg','jpg',1,1,3,'900150983cd24fb0d6963f7d28e17f72',
        'https://files.yande.re/1.jpg','tag','',NULL,'now')"""
    )
    connection.execute(
        "INSERT INTO query_posts VALUES(1,1,'now','now','tag/1.jpg',NULL,'downloaded')"
    )
    connection.commit()
    connection.close()
    before = image.read_bytes()
    backups = tmp_path / "backups"

    with Database(path, root, backup_dir=backups) as database:
        materialization = database.query_posts(1)[0]
        database.set_artist_name("korie_riko", "梱枝りこ")
        assert database.schema_version() == SCHEMA_VERSION
        assert materialization["relative_path"] == "tag/1.jpg"
        assert database.get_artist_name("korie_riko")["display_name"] == "梱枝りこ"

    assert image.read_bytes() == before
    assert len(list(backups.glob("yande-sync-v3-*.db"))) == 1


def create_legacy(path, local_path):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        """INSERT INTO posts VALUES(
        1,'1.jpg','jpg',1,1,3,'900150983cd24fb0d6963f7d28e17f72',
        'https://files.yande.re/1.jpg','tag','',NULL,'now','now',?,'downloaded')""",
        (str(local_path),),
    )
    connection.execute(
        "INSERT INTO queries VALUES(1,'tag','tag','now',NULL,NULL,1)"
    )
    connection.execute(
        "INSERT INTO query_posts VALUES(1,1,'now')"
    )
    connection.commit()
    connection.close()


def test_v0_absolute_paths_migrate_to_relative_paths(tmp_path):
    root = tmp_path / "pictures"
    path = root / "tag" / "1.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"abc")
    database_path = tmp_path / "state.db"
    create_legacy(database_path, path)
    backups = tmp_path / "backups"

    with Database(database_path, root, backup_dir=backups) as database:
        row = database.connection.execute("SELECT * FROM collection_posts").fetchone()
        assert database.schema_version() == SCHEMA_VERSION
        assert row["relative_path"] == str(path.relative_to(root))
        assert row["download_started_at"] is None
        materialization = database.query_posts(1)[0]
        assert database.path_for(materialization) == path.resolve()
    assert len(list(backups.glob("yande-sync-v0-*.db"))) == 1


def test_unsafe_legacy_path_rolls_back_and_preserves_backup(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    database_path = tmp_path / "state.db"
    create_legacy(database_path, tmp_path / "outside.jpg")
    backups = tmp_path / "backups"

    with pytest.raises(DatabaseError, match="outside download_dir"):
        Database(database_path, root, backup_dir=backups)
    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "local_path" in {
        row[1] for row in connection.execute("PRAGMA table_info(posts)")
    }
    connection.close()
    assert len(list(backups.glob("yande-sync-v0-*.db"))) == 1


@pytest.mark.parametrize(
    "unsafe_name",
    ["1.jpg:evil", "CON", "CON .txt", "COM1 .foo", "aux .txt", "name.", "name ",
     "bad\x00.jpg"],
)
def test_windows_unsafe_legacy_paths_roll_back(tmp_path, unsafe_name):
    root = tmp_path / "pictures"
    root.mkdir()
    database_path = tmp_path / "state.db"
    create_legacy(database_path, root / unsafe_name)

    with pytest.raises(DatabaseError, match="unsafe"):
        Database(database_path, root, backup_dir=tmp_path / "backups")

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "local_path" in {
            row[1] for row in connection.execute("PRAGMA table_info(posts)")
        }
    finally:
        connection.close()


def test_prepare_database_copies_legacy_without_deleting_it(tmp_path):
    legacy_root = tmp_path / "legacy"
    legacy_path = legacy_root / "data" / "state.db"
    legacy_path.parent.mkdir(parents=True)
    download_root = tmp_path / "pictures"
    download_root.mkdir()
    create_legacy(legacy_path, download_root / "1.jpg")
    storage = type("Storage", (), {
        "database": tmp_path / "portable" / "data" / "yande-sync.db",
        "download_dir": download_root,
        "legacy_database": legacy_path,
    })()

    assert prepare_database(storage).download_root == download_root.resolve()
    assert legacy_path.is_file()
    assert storage.database.is_file()
    assert sqlite3.connect(legacy_path).execute("PRAGMA user_version").fetchone()[0] == 0


def test_v1_event_schema_migrates_sequentially(tmp_path):
    root = tmp_path / "pictures"
    root.mkdir()
    path = tmp_path / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V2)
    connection.execute("ALTER TABLE download_events DROP COLUMN query_id")
    connection.execute("ALTER TABLE download_events DROP COLUMN run_id")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    backups = tmp_path / "backups"
    with Database(path, root, backup_dir=backups) as database:
        assert database.schema_version() == SCHEMA_VERSION
        columns = {
            row[1] for row in database.connection.execute("PRAGMA table_info(download_events)")
        }
    assert {"run_id", "collection_id"} <= columns
    assert len(list(backups.glob("yande-sync-v1-*.db"))) == 1


def test_custom_legacy_download_root_is_persisted_for_restart(tmp_path):
    app_root = tmp_path / "app"
    config_path = app_root / "config.toml"
    bootstrap_config(config_path)
    legacy_root = app_root / "legacy"
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("[storage]", "[storage]\nroot = \"legacy\""), encoding="utf-8"
    )
    custom_root = tmp_path / "custom-pictures"
    custom_root.mkdir()
    legacy_path = legacy_root / "data" / "state.db"
    legacy_path.parent.mkdir(parents=True)
    create_legacy(legacy_path, custom_root / "1.jpg")
    connection = sqlite3.connect(legacy_path)
    connection.execute(
        "INSERT INTO settings VALUES('download_dir',?,'now')", (str(custom_root),)
    )
    connection.commit()
    connection.close()

    config = load_config(config_path)
    with open_writable_database(config) as (migrated_config, _database):
        assert migrated_config.storage.download_dir == custom_root.resolve()
    assert load_config(config_path).storage.download_dir == custom_root.resolve()


@pytest.mark.parametrize("contents", [b"", b"not a sqlite database"])
def test_invalid_target_is_never_treated_as_completed_legacy_copy(tmp_path, contents):
    legacy_root = tmp_path / "legacy"
    legacy_path = legacy_root / "data" / "state.db"
    legacy_path.parent.mkdir(parents=True)
    download_root = tmp_path / "pictures"
    download_root.mkdir()
    create_legacy(legacy_path, download_root / "1.jpg")
    target = tmp_path / "portable" / "data" / "yande-sync.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(contents)
    storage = type("Storage", (), {
        "database": target,
        "download_dir": download_root,
        "legacy_database": legacy_path,
    })()

    with pytest.raises(DatabaseError):
        prepare_database(storage)
    assert target.read_bytes() == contents
    assert sqlite3.connect(legacy_path).execute("PRAGMA user_version").fetchone()[0] == 0


def test_interrupted_temporary_copy_is_recovered_on_restart(tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy"
    legacy_path = legacy_root / "data" / "state.db"
    legacy_path.parent.mkdir(parents=True)
    download_root = tmp_path / "pictures"
    download_root.mkdir()
    create_legacy(legacy_path, download_root / "1.jpg")
    target = tmp_path / "portable" / "data" / "yande-sync.db"
    storage = type("Storage", (), {
        "database": target,
        "download_dir": download_root,
        "legacy_database": legacy_path,
    })()
    original_promote = database_module._promote_no_replace
    monkeypatch.setattr(
        database_module, "_promote_no_replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("interrupted")),
    )

    with pytest.raises(OSError, match="interrupted"):
        prepare_database(storage)
    candidates = list(target.parent.glob(f".{target.name}.migration-*.tmp"))
    assert len(candidates) == 1
    monkeypatch.setattr(database_module, "_promote_no_replace", original_promote)

    prepared = prepare_database(storage)
    assert prepared.download_root == download_root.resolve()
    assert target.is_file()
    assert not candidates[0].exists()


def test_existing_valid_target_is_never_replaced_by_legacy_source(tmp_path):
    target = tmp_path / "portable" / "data" / "yande-sync.db"
    with Database(target, tmp_path / "pictures"):
        pass
    before = sha256(target.read_bytes()).digest()
    legacy = tmp_path / "legacy" / "data" / "state.db"
    legacy.parent.mkdir(parents=True)
    create_legacy(legacy, tmp_path / "pictures" / "1.jpg")
    storage = type("Storage", (), {
        "database": target,
        "download_dir": tmp_path / "pictures",
        "legacy_database": legacy,
    })()

    prepare_database(storage)
    assert sha256(target.read_bytes()).digest() == before


def test_config_write_failure_after_migration_is_restart_recoverable(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    config_path = app_root / "config.toml"
    bootstrap_config(config_path)
    legacy_root = app_root / "legacy"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[storage]", "[storage]\nroot = \"legacy\""
        ),
        encoding="utf-8",
    )
    custom_root = tmp_path / "custom-pictures"
    custom_root.mkdir()
    legacy_path = legacy_root / "data" / "state.db"
    legacy_path.parent.mkdir(parents=True)
    create_legacy(legacy_path, custom_root / "1.jpg")
    connection = sqlite3.connect(legacy_path)
    connection.execute("INSERT INTO settings VALUES('download_dir',?,'now')", (str(custom_root),))
    connection.commit()
    connection.close()
    original_write = cli_module.write_download_dir
    monkeypatch.setattr(
        cli_module, "write_download_dir",
        lambda *_args: (_ for _ in ()).throw(OSError("config write interrupted")),
    )

    with pytest.raises(OSError, match="config write interrupted"), open_writable_database(
        load_config(config_path)
    ):
        pass
    target = app_root / "data" / "yande-sync.db"
    connection = sqlite3.connect(target)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute(
        "SELECT value FROM settings WHERE key=?", (MIGRATION_ROOT_KEY,)
    ).fetchone()[0] == str(custom_root.resolve())
    connection.close()
    assert load_config(config_path).storage.download_dir != custom_root.resolve()

    monkeypatch.setattr(cli_module, "write_download_dir", original_write)
    with open_writable_database(load_config(config_path)) as (config, database):
        assert config.storage.download_dir == custom_root.resolve()
        assert database.get_setting(MIGRATION_ROOT_KEY) is None
    assert load_config(config_path).storage.download_dir == custom_root.resolve()


def test_v0_migration_failure_restarts_from_promoted_copy(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    config_path = app_root / "config.toml"
    bootstrap_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[storage]", "[storage]\nroot = \"legacy\""
        ),
        encoding="utf-8",
    )
    root = tmp_path / "pictures"
    root.mkdir()
    legacy = app_root / "legacy" / "data" / "state.db"
    legacy.parent.mkdir(parents=True)
    create_legacy(legacy, root / "1.jpg")
    connection = sqlite3.connect(legacy)
    connection.execute("INSERT INTO settings VALUES('download_dir',?,'now')", (str(root),))
    connection.commit()
    connection.close()
    original = Database._migrate_v0
    monkeypatch.setattr(
        Database, "_migrate_v0",
        lambda *_args: (_ for _ in ()).throw(DatabaseError("v0 injection")),
    )

    with pytest.raises(DatabaseError, match="v0 injection"), open_writable_database(
        load_config(config_path)
    ):
        pass
    target = app_root / "data" / "yande-sync.db"
    assert sqlite3.connect(target).execute("PRAGMA user_version").fetchone()[0] == 0
    assert sqlite3.connect(legacy).execute("PRAGMA user_version").fetchone()[0] == 0

    monkeypatch.setattr(Database, "_migrate_v0", original)
    with open_writable_database(load_config(config_path)) as (_config, database):
        assert database.schema_version() == SCHEMA_VERSION


def test_v1_migration_failure_is_transactional_and_restartable(tmp_path, monkeypatch):
    root = tmp_path / "pictures"
    root.mkdir()
    path = tmp_path / "data" / "yande-sync.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V2)
    connection.execute("ALTER TABLE download_events DROP COLUMN query_id")
    connection.execute("ALTER TABLE download_events DROP COLUMN run_id")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()
    original = Database._migrate_v1
    monkeypatch.setattr(
        Database, "_migrate_v1",
        lambda *_args: (_ for _ in ()).throw(DatabaseError("v1 injection")),
    )

    with pytest.raises(DatabaseError, match="v1 injection"):
        Database(path, root)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = {row[1] for row in connection.execute("PRAGMA table_info(download_events)")}
    connection.close()
    assert "run_id" not in columns and "query_id" not in columns

    monkeypatch.setattr(Database, "_migrate_v1", original)
    with Database(path, root) as database:
        assert database.schema_version() == SCHEMA_VERSION


def test_atomic_config_replace_failure_keeps_pending_root_for_restart(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    config_path = app_root / "config.toml"
    bootstrap_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "[storage]", "[storage]\nroot = \"legacy\""
        ),
        encoding="utf-8",
    )
    custom_root = tmp_path / "custom"
    custom_root.mkdir()
    legacy = app_root / "legacy" / "data" / "state.db"
    legacy.parent.mkdir(parents=True)
    create_legacy(legacy, custom_root / "1.jpg")
    connection = sqlite3.connect(legacy)
    connection.execute("INSERT INTO settings VALUES('download_dir',?,'now')", (str(custom_root),))
    connection.commit()
    connection.close()
    original_replace = cli_module.write_download_dir.__globals__["os"].replace
    monkeypatch.setattr(
        cli_module.write_download_dir.__globals__["os"], "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace injection")),
    )

    with pytest.raises(OSError, match="replace injection"), open_writable_database(
        load_config(config_path)
    ):
        pass
    target = app_root / "data" / "yande-sync.db"
    connection = sqlite3.connect(target)
    assert connection.execute(
        "SELECT value FROM settings WHERE key=?", (MIGRATION_ROOT_KEY,)
    ).fetchone()[0] == str(custom_root.resolve())
    connection.close()

    monkeypatch.setattr(cli_module.write_download_dir.__globals__["os"], "replace", original_replace)
    with open_writable_database(load_config(config_path)) as (config, _database):
        assert config.storage.download_dir == custom_root.resolve()
