from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .errors import OperationalError
from .models import Post
from .security import (
    canonical_source_signature,
    safe_library_path,
    validate_artist_display_name,
    validate_artist_tag,
    validate_download_root,
    validate_relative_path,
)

SCHEMA_VERSION = 5
MIGRATION_ROOT_KEY = "_portable_migration_download_root"
REQUIRED_TABLES = frozenset(
    {"posts", "collections", "collection_sources", "source_posts", "collection_posts",
     "runs", "download_events", "settings", "artist_names"}
)
LEGACY_REQUIRED_TABLES = frozenset(
    {"posts", "queries", "query_posts", "runs", "download_events", "settings"}
)


class DatabaseError(OperationalError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


POSTS_V1 = """
CREATE TABLE {table_name} (
    post_id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_ext TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    md5 TEXT NOT NULL,
    file_url TEXT NOT NULL,
    tags TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    remote_created_at TEXT,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT,
    relative_path TEXT,
    download_started_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('new','pending','downloading','downloaded','failed','missing','corrupt'))
);
"""


SCHEMA_V2 = POSTS_V1.format(table_name="IF NOT EXISTS posts") + """
CREATE TABLE IF NOT EXISTS queries (
    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_query TEXT NOT NULL UNIQUE,
    folder_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    highest_seen_post_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS query_posts (
    query_id INTEGER NOT NULL REFERENCES queries(query_id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(query_id, post_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    tag_query TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages_requested INTEGER NOT NULL DEFAULT 0,
    posts_received INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS download_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(post_id),
    run_id INTEGER REFERENCES runs(run_id),
    query_id INTEGER REFERENCES queries(query_id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    expected_size INTEGER NOT NULL,
    expected_md5 TEXT NOT NULL,
    actual_md5 TEXT,
    result TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_query_posts_post ON query_posts(post_id);
"""

POSTS_V3 = """
CREATE TABLE {table_name} (
    post_id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,
    file_ext TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    md5 TEXT NOT NULL,
    file_url TEXT NOT NULL,
    tags TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    remote_created_at TEXT,
    first_seen_at TEXT NOT NULL
);
"""

QUERY_POSTS_V3 = """
CREATE TABLE {table_name} (
    query_id INTEGER NOT NULL REFERENCES queries(query_id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT,
    relative_path TEXT,
    download_started_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('new','pending','downloading','downloaded','failed','missing','corrupt')),
    PRIMARY KEY(query_id, post_id)
);
"""

SCHEMA_V3 = POSTS_V3.format(table_name="IF NOT EXISTS posts") + """
CREATE TABLE IF NOT EXISTS queries (
    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_query TEXT NOT NULL UNIQUE,
    folder_name TEXT NOT NULL,
    folder_finalized INTEGER NOT NULL DEFAULT 0 CHECK(folder_finalized IN (0,1)),
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    highest_seen_post_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
""" + QUERY_POSTS_V3.format(table_name="IF NOT EXISTS query_posts") + """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    tag_query TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages_requested INTEGER NOT NULL DEFAULT 0,
    posts_received INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS download_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(post_id),
    run_id INTEGER REFERENCES runs(run_id),
    query_id INTEGER REFERENCES queries(query_id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    expected_size INTEGER NOT NULL,
    expected_md5 TEXT NOT NULL,
    actual_md5 TEXT,
    result TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_posts_post ON query_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_query_posts_status ON query_posts(status);
"""

ARTIST_NAMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_names (
    artist_tag TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_V4 = SCHEMA_V3 + ARTIST_NAMES_SCHEMA

COLLECTIONS_V5 = """
CREATE TABLE {table_name} (
    collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_signature TEXT NOT NULL UNIQUE,
    folder_name TEXT NOT NULL,
    folder_finalized INTEGER NOT NULL DEFAULT 0 CHECK(folder_finalized IN (0,1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL
);
"""

COLLECTION_SOURCES_V5 = """
CREATE TABLE {table_name} (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    tag_query TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    highest_seen_post_id INTEGER,
    UNIQUE(collection_id, position),
    UNIQUE(collection_id, tag_query)
);
"""

SOURCE_POSTS_V5 = """
CREATE TABLE {table_name} (
    source_id INTEGER NOT NULL REFERENCES collection_sources(source_id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(source_id, post_id)
);
"""

COLLECTION_POSTS_V5 = """
CREATE TABLE {table_name} (
    collection_id INTEGER NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    post_id INTEGER NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT,
    relative_path TEXT,
    download_started_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('new','pending','downloading','downloaded','failed','missing','corrupt')),
    PRIMARY KEY(collection_id, post_id)
);
"""

DOWNLOAD_EVENTS_V5 = """
CREATE TABLE {table_name} (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(post_id),
    run_id INTEGER REFERENCES runs(run_id),
    collection_id INTEGER REFERENCES collections(collection_id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    expected_size INTEGER NOT NULL,
    expected_md5 TEXT NOT NULL,
    actual_md5 TEXT,
    result TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT
);
"""

SCHEMA_V5 = POSTS_V3.format(table_name="IF NOT EXISTS posts") + \
    COLLECTIONS_V5.format(table_name="IF NOT EXISTS collections") + \
    COLLECTION_SOURCES_V5.format(table_name="IF NOT EXISTS collection_sources") + \
    SOURCE_POSTS_V5.format(table_name="IF NOT EXISTS source_posts") + \
    COLLECTION_POSTS_V5.format(table_name="IF NOT EXISTS collection_posts") + """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    tag_query TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages_requested INTEGER NOT NULL DEFAULT 0,
    posts_received INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT 'running'
);
""" + DOWNLOAD_EVENTS_V5.format(table_name="IF NOT EXISTS download_events") + """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_posts_post ON source_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_collection_posts_post ON collection_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_collection_posts_status ON collection_posts(status);
""" + ARTIST_NAMES_SCHEMA

MATERIALIZATION_COLUMNS = """p.*,c.collection_id,c.collection_id AS query_id,
c.folder_name,c.enabled,
cp.first_seen_at AS collection_first_seen_at,cp.downloaded_at,cp.relative_path,
cp.download_started_at,cp.status"""


def materialization_needs_work(row) -> bool:
    if row["status"] != "downloaded" or not row["relative_path"]:
        return True
    expected = Path(str(row["folder_name"])) / str(row["file_name"])
    return Path(str(row["relative_path"])) != expected


class Database:
    def __init__(self, path: Path, download_root: Path | None = None, *,
                 read_only: bool = False, backup_dir: Path | None = None):
        self.path = path
        self.download_root = (
            validate_download_root(download_root) if download_root is not None else None
        )
        self.read_only = read_only
        if read_only:
            if not path.is_file():
                raise DatabaseError(f"database does not exist: {path}")
            uri = "file:" + path.resolve().as_posix() + "?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True)
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA busy_timeout=3000")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            if not is_new and path.stat().st_size == 0:
                raise DatabaseError(f"database is empty or incomplete: {path}")
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            if is_new:
                self.connection.executescript(SCHEMA_V5)
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.connection.commit()
            else:
                version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if version == 0:
                    self._migrate_v0(backup_dir or path.parent / "backups")
                    version = 1
                if version == 1:
                    self._migrate_v1(backup_dir or path.parent / "backups")
                    version = 2
                if version == 2:
                    self._migrate_v2(backup_dir or path.parent / "backups")
                    version = 3
                if version == 3:
                    self._migrate_v3(backup_dir or path.parent / "backups")
                    version = 4
                if version == 4:
                    self._migrate_v4(backup_dir or path.parent / "backups")
                    version = 5
                if version > SCHEMA_VERSION:
                    raise DatabaseError(
                        f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                    )
                self.connection.executescript(SCHEMA_V5)
        self.connection.row_factory = sqlite3.Row

    def _migrate_v0(self, backup_dir: Path) -> None:
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(posts)")
        }
        if "local_path" not in columns:
            raise DatabaseError("unversioned database is not the recognized legacy schema")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"yande-sync-v0-{stamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()

        converted: dict[int, str | None] = {}
        for post_id, file_name, local_path in self.connection.execute(
            "SELECT post_id,file_name,local_path FROM posts"
        ):
            if local_path is None:
                converted[int(post_id)] = None
                continue
            if self.download_root is None:
                raise DatabaseError("legacy path migration requires the existing download root")
            legacy_path = Path(str(local_path))
            if not legacy_path.is_absolute():
                raise DatabaseError(f"legacy local_path is not absolute for post {post_id}")
            if "\x00" in str(legacy_path):
                raise DatabaseError(f"legacy local_path is unsafe for post {post_id}")
            try:
                relative = legacy_path.resolve().relative_to(self.download_root.resolve())
            except ValueError as exc:
                raise DatabaseError(
                    f"legacy local_path is outside download_dir for post {post_id}"
                ) from exc
            try:
                relative = validate_relative_path(relative)
            except ValueError as exc:
                raise DatabaseError(
                    f"legacy local_path is unsafe for post {post_id}"
                ) from exc
            if relative.name != str(file_name):
                raise DatabaseError(
                    f"legacy local_path filename does not match post {post_id}"
                )
            converted[int(post_id)] = str(relative)

        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(POSTS_V1.format(table_name="posts_new"))
            legacy_rows = self.connection.execute("SELECT * FROM posts").fetchall()
            for row in legacy_rows:
                self.connection.execute(
                    """INSERT INTO posts_new(
                    post_id,file_name,file_ext,width,height,file_size,md5,file_url,tags,source,
                    remote_created_at,first_seen_at,downloaded_at,relative_path,
                    download_started_at,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (*row[:13], converted[int(row[0])], row[14]),
                )
            self.connection.execute("DROP TABLE posts")
            self.connection.execute("ALTER TABLE posts_new RENAME TO posts")
            self.connection.execute("CREATE INDEX idx_posts_status ON posts(status)")
            self.connection.execute("PRAGMA user_version=1")
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DatabaseError("foreign-key validation failed during migration")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def _migrate_v1(self, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"yande-sync-v1-{stamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "ALTER TABLE download_events ADD COLUMN run_id INTEGER REFERENCES runs(run_id)"
            )
            self.connection.execute(
                """ALTER TABLE download_events ADD COLUMN query_id INTEGER
                REFERENCES queries(query_id)"""
            )
            self.connection.execute("PRAGMA user_version=2")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _migrate_v2(self, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"yande-sync-v2-{stamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()

        post_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(posts)")
        }
        query_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(queries)")
        }
        membership_columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(query_posts)")
        }
        if (
            "status" not in post_columns
            and "folder_finalized" in query_columns
            and "status" in membership_columns
        ):
            self.connection.execute("PRAGMA user_version=3")
            self.connection.commit()
            return

        associations = self.connection.execute(
            """SELECT qp.query_id,qp.post_id,qp.first_seen_at,p.downloaded_at,
            p.relative_path,p.download_started_at,p.status FROM query_posts qp
            JOIN posts p ON p.post_id=qp.post_id"""
        ).fetchall()
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(POSTS_V3.format(table_name="posts_new"))
            self.connection.execute(
                """INSERT INTO posts_new(post_id,file_name,file_ext,width,height,file_size,md5,
                file_url,tags,source,remote_created_at,first_seen_at)
                SELECT post_id,file_name,file_ext,width,height,file_size,md5,file_url,tags,source,
                remote_created_at,first_seen_at FROM posts"""
            )
            self.connection.execute("DROP TABLE query_posts")
            self.connection.execute("DROP TABLE posts")
            self.connection.execute("ALTER TABLE posts_new RENAME TO posts")
            self.connection.execute(QUERY_POSTS_V3.format(table_name="query_posts"))
            self.connection.executemany(
                """INSERT INTO query_posts(query_id,post_id,first_seen_at,downloaded_at,
                relative_path,download_started_at,status) VALUES(?,?,?,?,?,?,?)""",
                [tuple(row) for row in associations],
            )
            self.connection.execute(
                """ALTER TABLE queries ADD COLUMN folder_finalized INTEGER NOT NULL DEFAULT 0
                CHECK(folder_finalized IN (0,1))"""
            )
            self.connection.execute(
                "CREATE INDEX idx_query_posts_post ON query_posts(post_id)"
            )
            self.connection.execute(
                "CREATE INDEX idx_query_posts_status ON query_posts(status)"
            )
            self.connection.execute("PRAGMA user_version=3")
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DatabaseError("foreign-key validation failed during v2 migration")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def _migrate_v3(self, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"yande-sync-v3-{stamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS artist_names (
                artist_tag TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL)"""
            )
            self.connection.execute("PRAGMA user_version=4")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _migrate_v4(self, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"yande-sync-v4-{stamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()

        queries = self.connection.execute(
            """SELECT query_id,tag_query,folder_name,folder_finalized,created_at,
            last_checked_at,highest_seen_post_id,enabled FROM queries ORDER BY query_id"""
        ).fetchall()
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                COLLECTIONS_V5.format(table_name="collections")
            )
            self.connection.execute(
                COLLECTION_SOURCES_V5.format(table_name="collection_sources")
            )
            self.connection.execute(SOURCE_POSTS_V5.format(table_name="source_posts"))
            self.connection.execute(
                COLLECTION_POSTS_V5.format(table_name="collection_posts")
            )
            for query in queries:
                query_id = int(query["query_id"] if isinstance(query, sqlite3.Row) else query[0])
                tag_query = str(query["tag_query"] if isinstance(query, sqlite3.Row) else query[1])
                folder_name = str(query["folder_name"] if isinstance(query, sqlite3.Row) else query[2])
                folder_finalized = int(
                    query["folder_finalized"] if isinstance(query, sqlite3.Row) else query[3]
                )
                created_at = str(query["created_at"] if isinstance(query, sqlite3.Row) else query[4])
                last_checked = query["last_checked_at"] if isinstance(query, sqlite3.Row) else query[5]
                highest = query["highest_seen_post_id"] if isinstance(query, sqlite3.Row) else query[6]
                enabled = int(query["enabled"] if isinstance(query, sqlite3.Row) else query[7])
                self.connection.execute(
                    """INSERT INTO collections(collection_id,source_signature,folder_name,
                    folder_finalized,enabled,created_at) VALUES(?,?,?,?,?,?)""",
                    (query_id, canonical_source_signature([tag_query]), folder_name,
                     folder_finalized, enabled, created_at),
                )
                self.connection.execute(
                    """INSERT INTO collection_sources(source_id,collection_id,position,
                    tag_query,created_at,last_checked_at,highest_seen_post_id)
                    VALUES(?,?,0,?,?,?,?)""",
                    (query_id, query_id, tag_query, created_at, last_checked, highest),
                )
            self.connection.execute(
                """INSERT INTO source_posts(source_id,post_id,first_seen_at)
                SELECT query_id,post_id,first_seen_at FROM query_posts"""
            )
            self.connection.execute(
                """INSERT INTO collection_posts(collection_id,post_id,first_seen_at,
                downloaded_at,relative_path,download_started_at,status)
                SELECT query_id,post_id,first_seen_at,downloaded_at,relative_path,
                download_started_at,status FROM query_posts"""
            )
            self.connection.execute(
                DOWNLOAD_EVENTS_V5.format(table_name="download_events_new")
            )
            self.connection.execute(
                """INSERT INTO download_events_new(event_id,post_id,run_id,collection_id,
                attempt,started_at,finished_at,bytes_received,expected_size,expected_md5,
                actual_md5,result,error_type,error_message)
                SELECT event_id,post_id,run_id,query_id,attempt,started_at,finished_at,
                bytes_received,expected_size,expected_md5,actual_md5,result,error_type,
                error_message FROM download_events"""
            )
            self.connection.execute("DROP TABLE download_events")
            self.connection.execute("DROP TABLE query_posts")
            self.connection.execute("DROP TABLE queries")
            self.connection.execute("ALTER TABLE download_events_new RENAME TO download_events")
            self.connection.execute("CREATE INDEX idx_source_posts_post ON source_posts(post_id)")
            self.connection.execute(
                "CREATE INDEX idx_collection_posts_post ON collection_posts(post_id)"
            )
            self.connection.execute(
                "CREATE INDEX idx_collection_posts_status ON collection_posts(status)"
            )
            self.connection.execute("PRAGMA user_version=5")
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise DatabaseError("foreign-key validation failed during v4 migration")
            integrity = self.connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise DatabaseError("integrity validation failed during v4 migration")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        if self.read_only:
            raise DatabaseError("database is read-only")
        try:
            if immediate:
                self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def path_for(self, row) -> Path:
        if self.download_root is None:
            raise DatabaseError("download_dir is not configured")
        relative = row["relative_path"]
        if not relative:
            relative = row["file_name"]
        path = safe_library_path(self.download_root, relative)
        if path.name != row["file_name"]:
            raise DatabaseError("stored relative_path filename does not match post metadata")
        return path

    def expected_path_for(self, row) -> Path:
        if self.download_root is None:
            raise DatabaseError("download_dir is not configured")
        return safe_library_path(
            self.download_root, Path(str(row["folder_name"])) / str(row["file_name"])
        )

    def add_collection(self, sources: list[str], folder_name: str) -> sqlite3.Row:
        signature = canonical_source_signature(sources)
        timestamp = now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM collections WHERE source_signature=?", (signature,)
            ).fetchone()
            if existing is not None:
                return existing
            cursor = conn.execute(
                """INSERT INTO collections(source_signature,folder_name,created_at)
                VALUES(?,?,?)""",
                (signature, folder_name, timestamp),
            )
            collection_id = int(cursor.lastrowid)
            conn.executemany(
                """INSERT INTO collection_sources(collection_id,position,tag_query,created_at)
                VALUES(?,?,?,?)""",
                [
                    (collection_id, position, source, timestamp)
                    for position, source in enumerate(sources)
                ],
            )
            row = conn.execute(
                "SELECT * FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError("collection was not stored")
            return row

    def add_sources_to_collection(self, collection_id: int,
                                  sources: list[str]) -> tuple[list[str], list[str]]:
        """Attach validated sources atomically without changing collection/file state."""
        timestamp = now_iso()
        with self.transaction() as conn:
            collection = conn.execute(
                "SELECT * FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone()
            if collection is None:
                raise ValueError(f"collection is not registered: {collection_id}")
            existing_rows = conn.execute(
                """SELECT position,tag_query FROM collection_sources WHERE collection_id=?
                ORDER BY position""", (collection_id,),
            ).fetchall()
            existing = [str(row["tag_query"]) for row in existing_rows]
            existing_set = set(existing)
            present = [source for source in sources if source in existing_set]
            added = [source for source in sources if source not in existing_set]
            combined = [*existing, *added]
            # Preflight the unique collection identity before inserting any membership.
            signature = canonical_source_signature(combined)
            collision = conn.execute(
                """SELECT collection_id FROM collections
                WHERE source_signature=? AND collection_id<>?""",
                (signature, collection_id),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    f"the resulting source list already belongs to collection {collision[0]}"
                )
            next_position = max(
                (int(row["position"]) for row in existing_rows), default=-1
            ) + 1
            conn.executemany(
                """INSERT INTO collection_sources(
                collection_id,position,tag_query,created_at) VALUES(?,?,?,?)""",
                [
                    (collection_id, next_position + offset, source, timestamp)
                    for offset, source in enumerate(added)
                ],
            )
            if added:
                conn.execute(
                    "UPDATE collections SET source_signature=? WHERE collection_id=?",
                    (signature, collection_id),
                )
        return added, present

    def remove_sources_from_collection(self, collection_id: int,
                                       sources: list[str]) -> list[str]:
        """Remove collection-local subscriptions without touching materializations."""
        with self.transaction(immediate=True) as conn:
            collection = conn.execute(
                "SELECT * FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone()
            if collection is None:
                raise ValueError(f"collection is not registered: {collection_id}")
            existing_rows = conn.execute(
                """SELECT * FROM collection_sources WHERE collection_id=?
                ORDER BY position""", (collection_id,),
            ).fetchall()
            existing = [str(row["tag_query"]) for row in existing_rows]
            existing_set = set(existing)
            missing = [source for source in sources if source not in existing_set]
            if missing:
                raise ValueError(
                    "sources are not present in collection "
                    f"{collection_id}: {', '.join(missing)}"
                )
            removing = set(sources)
            remaining = [source for source in existing if source not in removing]
            if not remaining:
                raise ValueError(
                    f"cannot remove the last source from collection {collection_id}"
                )
            signature = canonical_source_signature(remaining)
            collision = conn.execute(
                """SELECT collection_id FROM collections
                WHERE source_signature=? AND collection_id<>?""",
                (signature, collection_id),
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    f"the remaining source list already belongs to collection {collision[0]}"
                )
            placeholders = ",".join("?" for _ in sources)
            cursor = conn.execute(
                f"""DELETE FROM collection_sources WHERE collection_id=?
                AND tag_query IN ({placeholders})""",
                (collection_id, *sources),
            )
            if cursor.rowcount != len(sources):
                raise DatabaseError("source membership changed during removal")
            conn.execute(
                "UPDATE collections SET source_signature=? WHERE collection_id=?",
                (signature, collection_id),
            )
        return sources

    # One-source compatibility helpers keep older internal callers readable while all
    # persisted state uses the v5 collection/source relations.
    def add_query(self, tag_query: str, folder_name: str):
        row = dict(self.add_collection([tag_query], folder_name))
        row.update(query_id=row["collection_id"], tag_query=tag_query)
        return row

    def get_query(self, tag_query: str):
        matches = self.find_source_collections(tag_query)
        if len(matches) != 1:
            return None
        row = dict(matches[0])
        row.update(query_id=row["collection_id"], tag_query=tag_query)
        return row

    def list_queries(self, *, enabled_only: bool = False):
        rows = []
        for collection in self.list_collections(enabled_only=enabled_only):
            sources = self.collection_sources(int(collection["collection_id"]))
            if len(sources) == 1:
                row = dict(collection)
                row.update(
                    query_id=row["collection_id"], tag_query=sources[0]["tag_query"],
                    last_checked_at=sources[0]["last_checked_at"],
                    highest_seen_post_id=sources[0]["highest_seen_post_id"],
                )
                rows.append(row)
        return rows

    def get_collection(self, collection_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM collections WHERE collection_id=?", (collection_id,)
        ).fetchone()

    def find_source_collections(self, tag_query: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT c.* FROM collections c
            JOIN collection_sources cs ON cs.collection_id=c.collection_id
            WHERE cs.tag_query=?
            ORDER BY c.collection_id""",
            (tag_query,),
        ))

    def list_collections(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM collections"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY collection_id"
        return list(self.connection.execute(sql))

    def collection_sources(self, collection_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT * FROM collection_sources WHERE collection_id=?
            ORDER BY position""",
            (collection_id,),
        ))

    def collection_summary(self, collection_id: int) -> str:
        return " + ".join(
            str(row["tag_query"]) for row in self.collection_sources(collection_id)
        )

    def folder_names(self, *, exclude_collection_id: int | None = None) -> list[str]:
        sql = "SELECT folder_name FROM collections"
        params: tuple[object, ...] = ()
        if exclude_collection_id is not None:
            sql += " WHERE collection_id<>?"
            params = (exclude_collection_id,)
        return [str(row[0]) for row in self.connection.execute(sql, params)]

    def set_artist_name(self, artist_tag: str, display_name: str) -> sqlite3.Row:
        key = validate_artist_tag(artist_tag)
        value = validate_artist_display_name(display_name)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO artist_names(artist_tag,display_name,updated_at)
                VALUES(?,?,?) ON CONFLICT(artist_tag) DO UPDATE SET
                display_name=excluded.display_name,updated_at=excluded.updated_at""",
                (key, value, now_iso()),
            )
        row = self.get_artist_name(key)
        if row is None:
            raise DatabaseError(f"artist name was not stored: {key}")
        return row

    def get_artist_name(self, artist_tag: str) -> sqlite3.Row | None:
        key = validate_artist_tag(artist_tag)
        return self.connection.execute(
            "SELECT * FROM artist_names WHERE artist_tag=?", (key,)
        ).fetchone()

    def list_artist_names(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM artist_names ORDER BY artist_tag COLLATE NOCASE,artist_tag"
        ))

    def unset_artist_name(self, artist_tag: str) -> bool:
        key = validate_artist_tag(artist_tag)
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM artist_names WHERE artist_tag=?", (key,))
        return cursor.rowcount > 0

    def set_collection_enabled(self, collection_id: int, enabled: bool) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE collections SET enabled=? WHERE collection_id=?",
                (int(enabled), collection_id),
            )
        return cursor.rowcount > 0

    def source_post_ids(self, source_id: int) -> set[int]:
        rows = self.connection.execute(
            "SELECT post_id FROM source_posts WHERE source_id=?", (source_id,)
        )
        return {int(row[0]) for row in rows}

    def store_source_posts(self, source_id: int, collection_id: int,
                           posts: list[Post]) -> list[Post]:
        timestamp = now_iso()
        new_for_collection: list[Post] = []
        with self.transaction() as conn:
            source = conn.execute(
                "SELECT collection_id FROM collection_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if source is None or int(source[0]) != collection_id:
                raise DatabaseError("source does not belong to the selected collection")
            for post in posts:
                conn.execute(
                    """INSERT INTO posts(post_id,file_name,file_ext,width,height,file_size,md5,
                    file_url,tags,source,remote_created_at,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(post_id) DO UPDATE SET
                    file_name=excluded.file_name,file_ext=excluded.file_ext,width=excluded.width,
                    height=excluded.height,file_size=excluded.file_size,md5=excluded.md5,
                    file_url=excluded.file_url,tags=excluded.tags,source=excluded.source,
                    remote_created_at=excluded.remote_created_at""",
                    (
                        post.post_id, post.file_name, post.file_ext, post.width, post.height,
                        post.file_size, post.md5, post.file_url, post.tags, post.source,
                        post.remote_created_at, timestamp,
                    ),
                )
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO source_posts(source_id,post_id,first_seen_at)
                    VALUES(?,?,?)""",
                    (source_id, post.post_id, timestamp),
                )
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO collection_posts(
                    collection_id,post_id,first_seen_at,status) VALUES(?,?,?,'new')""",
                    (collection_id, post.post_id, timestamp),
                )
                if cursor.rowcount:
                    new_for_collection.append(post)
            highest = max((post.post_id for post in posts), default=None)
            conn.execute(
                """UPDATE collection_sources SET last_checked_at=?, highest_seen_post_id=
                CASE WHEN ? IS NULL THEN highest_seen_post_id
                     WHEN highest_seen_post_id IS NULL OR ? > highest_seen_post_id THEN ?
                     ELSE highest_seen_post_id END WHERE source_id=?""",
                (timestamp, highest, highest, highest, source_id),
            )
        return new_for_collection

    def store_posts(self, collection_id: int, posts: list[Post]) -> list[Post]:
        sources = self.collection_sources(collection_id)
        if len(sources) != 1:
            raise DatabaseError("store_posts compatibility requires a one-source collection")
        return self.store_source_posts(
            int(sources[0]["source_id"]), collection_id, posts
        )

    def posts_to_download(self, collection_id: int,
                          limit: int | None = None) -> list[sqlite3.Row]:
        sql = f"""SELECT {MATERIALIZATION_COLUMNS} FROM posts p
        JOIN collection_posts cp ON cp.post_id=p.post_id
        JOIN collections c ON c.collection_id=cp.collection_id
        WHERE cp.collection_id=? AND cp.status<>'downloading' ORDER BY p.post_id"""
        rows = [row for row in self.connection.execute(sql, (collection_id,))
                if materialization_needs_work(row)]
        return rows[:limit] if limit is not None else rows

    def collection_posts(self, collection_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            f"""SELECT {MATERIALIZATION_COLUMNS} FROM posts p
            JOIN collection_posts cp ON cp.post_id=p.post_id
            JOIN collections c ON c.collection_id=cp.collection_id
            WHERE cp.collection_id=? ORDER BY p.post_id DESC""", (collection_id,)
        ))

    def query_posts(self, query_id: int) -> list[sqlite3.Row]:
        return self.collection_posts(query_id)

    def posts_for_collections(self, collection_ids: list[int]) -> list[sqlite3.Row]:
        if not collection_ids:
            return []
        placeholders = ",".join("?" for _ in collection_ids)
        return list(self.connection.execute(
            f"""SELECT {MATERIALIZATION_COLUMNS} FROM posts p
            JOIN collection_posts cp ON cp.post_id=p.post_id
            JOIN collections c ON c.collection_id=cp.collection_id
            WHERE cp.collection_id IN ({placeholders})
            ORDER BY cp.collection_id,p.post_id""",
            collection_ids,
        ))

    def posts_for_queries(self, query_ids: list[int]) -> list[sqlite3.Row]:
        return self.posts_for_collections(query_ids)

    def posts_to_download_for_collections(self, collection_ids: list[int],
                                          limit: int | None = None) -> list[sqlite3.Row]:
        if not collection_ids:
            return []
        placeholders = ",".join("?" for _ in collection_ids)
        sql = f"""SELECT {MATERIALIZATION_COLUMNS} FROM posts p
        JOIN collection_posts cp ON cp.post_id=p.post_id
        JOIN collections c ON c.collection_id=cp.collection_id
        WHERE cp.collection_id IN ({placeholders})
        AND cp.status<>'downloading'
        ORDER BY cp.collection_id,p.post_id"""
        rows = [row for row in self.connection.execute(sql, collection_ids)
                if materialization_needs_work(row)]
        return rows[:limit] if limit is not None else rows

    def posts_to_download_for_queries(self, query_ids: list[int],
                                      limit: int | None = None) -> list[sqlite3.Row]:
        return self.posts_to_download_for_collections(query_ids, limit)

    def status_counts(self) -> dict[str, int]:
        counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM collection_posts GROUP BY status"
            )
        }
        counts["materializations"] = sum(counts.values())
        counts["total"] = int(self.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
        counts["collections"] = int(
            self.connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        )
        counts["enabled_collections"] = int(
            self.connection.execute("SELECT COUNT(*) FROM collections WHERE enabled=1").fetchone()[0]
        )
        counts["sources"] = int(
            self.connection.execute("SELECT COUNT(*) FROM collection_sources").fetchone()[0]
        )
        return counts

    def last_run(self, command: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM runs WHERE command=? AND finished_at IS NOT NULL
            ORDER BY run_id DESC LIMIT 1""",
            (command,),
        ).fetchone()

    def set_materialization_status(self, collection_id: int, post_id: int, status: str, *,
                                   local_path=None, relative_path=None) -> None:
        timestamp = now_iso()
        with self.transaction() as conn:
            if status == "downloaded":
                if relative_path is None:
                    if local_path is None or self.download_root is None:
                        raise DatabaseError("downloaded status requires a path and download_dir")
                    try:
                        relative_path = Path(local_path).resolve().relative_to(
                            self.download_root.resolve()
                        )
                    except ValueError as exc:
                        raise DatabaseError("downloaded path is outside download_dir") from exc
                relative = str(validate_relative_path(relative_path))
                relative_parts = Path(relative).parts
                collection = conn.execute(
                    """SELECT folder_name,folder_finalized FROM collections
                    WHERE collection_id=?""",
                    (collection_id,),
                ).fetchone()
                if collection is None:
                    raise DatabaseError(f"collection does not exist: {collection_id}")
                if len(relative_parts) == 2:
                    materialized_folder = relative_parts[0]
                    if bool(collection["folder_finalized"]):
                        if str(collection["folder_name"]) != materialized_folder:
                            raise DatabaseError(
                                "downloaded path conflicts with finalized collection folder"
                            )
                    else:
                        conn.execute(
                            """UPDATE collections SET folder_name=?,folder_finalized=1
                            WHERE collection_id=?""",
                            (materialized_folder, collection_id),
                        )
                conn.execute(
                    """UPDATE collection_posts SET status=?,relative_path=?,downloaded_at=?,
                    download_started_at=NULL WHERE collection_id=? AND post_id=?""",
                    (status, relative, timestamp, collection_id, post_id),
                )
            elif status == "downloading":
                conn.execute(
                    """UPDATE collection_posts SET status=?,download_started_at=?
                    WHERE collection_id=? AND post_id=?""",
                    (status, timestamp, collection_id, post_id),
                )
            else:
                conn.execute(
                    """UPDATE collection_posts SET status=?,download_started_at=NULL
                    WHERE collection_id=? AND post_id=?""",
                    (status, collection_id, post_id),
                )

    def recover_abandoned_downloads(self) -> int:
        timestamp = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE runs SET result='interrupted',finished_at=?
                WHERE result='running' AND finished_at IS NULL""",
                (timestamp,),
            )
            conn.execute(
                """UPDATE download_events SET result='interrupted',finished_at=?,
                error_type='ProcessInterrupted',error_message='abandoned by a terminated process'
                WHERE result='running' AND finished_at IS NULL""",
                (timestamp,),
            )
            cursor = conn.execute(
                """UPDATE collection_posts SET status='pending',download_started_at=NULL
                WHERE status='downloading'"""
            )
        return cursor.rowcount

    def reusable_materializations(self, post_id: int, *,
                                  exclude_collection_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            f"""SELECT {MATERIALIZATION_COLUMNS} FROM posts p
            JOIN collection_posts cp ON cp.post_id=p.post_id
            JOIN collections c ON c.collection_id=cp.collection_id
            WHERE p.post_id=? AND cp.collection_id<>? AND cp.status='downloaded'
            AND cp.relative_path IS NOT NULL ORDER BY cp.collection_id""",
            (post_id, exclude_collection_id),
        ))

    def start_run(self, command: str, tag_query: str | None) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO runs(command,tag_query,started_at) VALUES(?,?,?)",
                (command, tag_query, now_iso()),
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, **values) -> None:
        allowed = {
            "pages_requested", "posts_received", "new_count", "downloaded_count",
            "failed_count", "result",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        values["finished_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id=?", (*values.values(), run_id)
            )

    @contextmanager
    def run(self, command: str, tag_query: str | None):
        lifecycle = RunLifecycle(self, self.start_run(command, tag_query))
        try:
            yield lifecycle
        except KeyboardInterrupt:
            lifecycle.values["result"] = "interrupted"
            raise
        except BaseException:
            lifecycle.values["result"] = "failed"
            raise
        finally:
            lifecycle.finish()

    def next_attempt(self, collection_id: int, post_id: int) -> int:
        row = self.connection.execute(
            """SELECT COALESCE(MAX(attempt),0)+1 FROM download_events
            WHERE collection_id=? AND post_id=?""",
            (collection_id, post_id),
        ).fetchone()
        return int(row[0])

    def start_download_event(self, post_id: int, attempt: int, expected_size: int,
                             expected_md5: str, *, started_at: str | None = None,
                             run_id: int | None = None,
                             collection_id: int | None = None) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO download_events(post_id,run_id,collection_id,attempt,started_at,
                finished_at,bytes_received,expected_size,expected_md5,result)
                VALUES(?,?,?,?,?,NULL,0,?,?,'running')""",
                (
                    post_id, run_id, collection_id, attempt, started_at or now_iso(),
                    expected_size, expected_md5,
                ),
            )
        return int(cursor.lastrowid)

    def finish_download_event(self, event_id: int, **values) -> None:
        allowed = {
            "bytes_received", "actual_md5", "result", "error_type", "error_message"
        }
        values = {key: value for key, value in values.items() if key in allowed}
        values["finished_at"] = now_iso()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE download_events SET {assignments} WHERE event_id=?",
                (*values.values(), event_id),
            )

    def recent_events(self, limit: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """SELECT e.finished_at,e.collection_id,p.post_id,p.file_name,p.file_ext,p.width,
            p.height,p.file_size,e.result,e.error_type FROM download_events e
            JOIN posts p ON p.post_id=e.post_id
            ORDER BY e.event_id DESC LIMIT ?""", (limit,)
        ))

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def delete_setting(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))


@dataclass(frozen=True, slots=True)
class PreparedDatabase:
    download_root: Path | None


def _database_metadata(path: Path) -> tuple[int, str | None]:
    if not path.is_file() or path.stat().st_size == 0:
        raise DatabaseError(f"database is empty or incomplete: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise DatabaseError(f"database integrity check failed: {path}")
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not (REQUIRED_TABLES <= tables or LEGACY_REQUIRED_TABLES <= tables):
            raise DatabaseError(f"database does not have the required yande-sync tables: {path}")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        post_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(posts)")
        }
        event_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(download_events)")
        }
        query_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(queries)")
        } if "queries" in tables else set()
        membership_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(query_posts)")
        } if "query_posts" in tables else set()
        if version == 0 and "local_path" not in post_columns:
            raise DatabaseError(f"database is not the recognized v0 schema: {path}")
        if version in {1, 2} and "relative_path" not in post_columns:
            raise DatabaseError(f"database is not the recognized v{version} schema: {path}")
        if version == 2 and not {"run_id", "query_id"} <= event_columns:
            raise DatabaseError(f"database is not the recognized v2 schema: {path}")
        if version == 3 and not (
            {"folder_finalized"} <= query_columns
            and {"status", "relative_path", "download_started_at"} <= membership_columns
            and "status" not in post_columns
        ):
            raise DatabaseError(f"database is not the recognized v3 schema: {path}")
        if version == 4 and not (
            {"folder_finalized"} <= query_columns
            and {"status", "relative_path", "download_started_at"} <= membership_columns
            and "status" not in post_columns
            and "artist_names" in tables
        ):
            raise DatabaseError(f"database is not the recognized v4 schema: {path}")
        if version == 5:
            required = {
                "posts", "collections", "collection_sources", "source_posts",
                "collection_posts", "runs", "download_events", "settings", "artist_names",
            }
            event_v5_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(download_events)")
            }
            if not required <= tables or "collection_id" not in event_v5_columns:
                raise DatabaseError(f"database is not the recognized v5 schema: {path}")
        if version not in {0, 1, 2, 3, 4, 5}:
            raise DatabaseError(f"unsupported database schema {version}: {path}")
        marker = connection.execute(
            "SELECT value FROM settings WHERE key=?", (MIGRATION_ROOT_KEY,)
        ).fetchone()
        if marker is None and version < SCHEMA_VERSION:
            marker = connection.execute(
                "SELECT value FROM settings WHERE key='download_dir'"
            ).fetchone()
        return version, str(marker[0]) if marker and marker[0] else None
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot validate database {path}: {exc}") from exc
    finally:
        connection.close()


def _flush_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _promote_no_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(source, target)
        return
    os.link(source, target)  # pragma: no cover - Windows is the supported runtime
    source.unlink()  # pragma: no cover


def _migration_candidates(target: Path) -> list[Path]:
    return sorted(
        target.parent.glob(f".{target.name}.migration-*.tmp"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )


def _promote_recoverable_temporary(target: Path) -> bool:
    for candidate in _migration_candidates(target):
        try:
            _database_metadata(candidate)
        except (DatabaseError, OSError):
            continue
        try:
            _promote_no_replace(candidate, target)
        except FileExistsError:
            return False
        return True
    return False


def _copy_legacy_database(source_path: Path, target: Path, download_root: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.migration-{uuid4().hex}.tmp"
    with temporary.open("xb"):
        pass
    source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
        destination.execute(
            """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (MIGRATION_ROOT_KEY, str(download_root), now_iso()),
        )
        destination.commit()
    finally:
        destination.close()
        source.close()
    _database_metadata(temporary)
    _flush_file(temporary)
    _promote_no_replace(temporary, target)


def _set_migration_marker(path: Path, download_root: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (MIGRATION_ROOT_KEY, str(download_root), now_iso()),
        )
        connection.commit()
    finally:
        connection.close()


def prepare_database(storage) -> PreparedDatabase:
    target = storage.database
    download_root = storage.download_dir
    legacy = storage.legacy_database
    if target.exists():
        version, marker = _database_metadata(target)
        if marker:
            root = validate_download_root(Path(marker))
            if version < SCHEMA_VERSION:
                _set_migration_marker(target, root)
            return PreparedDatabase(root)
        return PreparedDatabase(download_root)

    if _promote_recoverable_temporary(target):
        _version, marker = _database_metadata(target)
        root = validate_download_root(Path(marker)) if marker else download_root
        return PreparedDatabase(root)

    if legacy is None or not legacy.is_file():
        return PreparedDatabase(download_root)

    _version, legacy_root = _database_metadata(legacy)
    if legacy_root:
        download_root = validate_download_root(Path(legacy_root))
    if download_root is None:
        raise DatabaseError("legacy database migration requires the existing download root")
    _copy_legacy_database(legacy, target, download_root)
    return PreparedDatabase(download_root)


@dataclass(slots=True)
class RunLifecycle:
    database: Database
    run_id: int
    values: dict[str, object] = field(default_factory=dict)
    _finished: bool = False

    def update(self, **values) -> None:
        self.values.update(values)

    def finish(self) -> None:
        if self._finished:
            return
        self.values.setdefault("result", "ok")
        self.database.finish_run(self.run_id, **self.values)
        self._finished = True
