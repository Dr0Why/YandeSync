from __future__ import annotations

import sqlite3

import pytest

import yande_sync.cli as cli_module
from yande_sync.cli import do_sync, ensure_collection, main, prepare_collection_folder
from yande_sync.compare import CheckResult, incremental_check
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import SCHEMA_V4, SCHEMA_VERSION, Database
from yande_sync.downloader import download_post
from yande_sync.errors import OperationalError
from yande_sync.logger import EventLogger
from yande_sync.models import Post
from yande_sync.security import canonical_source_signature


def post(post_id: int) -> Post:
    return Post(
        post_id, f"{post_id}.jpg", "jpg", 1, 1, 3,
        "900150983cd24fb0d6963f7d28e17f72",
        f"https://files.yande.re/{post_id}.jpg", "tag", "", None,
    )


def config_at(tmp_path):
    path = tmp_path / "app" / "config.toml"
    bootstrap_config(path)
    write_download_dir(path, tmp_path / "downloads")
    return path


def test_multi_source_union_keeps_membership_separate_from_materialization(db):
    collection = db.add_collection(["karory", "karomix"], "karory + karomix")
    first, second = db.collection_sources(collection["collection_id"])

    assert [item.post_id for item in db.store_source_posts(
        first["source_id"], collection["collection_id"], [post(100), post(101), post(102)]
    )] == [100, 101, 102]
    assert [item.post_id for item in db.store_source_posts(
        second["source_id"], collection["collection_id"], [post(101), post(102), post(103)]
    )] == [103]

    assert db.connection.execute("SELECT COUNT(*) FROM source_posts").fetchone()[0] == 6
    assert [row[0] for row in db.connection.execute(
        "SELECT post_id FROM collection_posts ORDER BY post_id"
    )] == [100, 101, 102, 103]
    assert len(db.posts_to_download_for_collections([collection["collection_id"]])) == 4


def test_same_post_from_two_sources_is_downloaded_once_in_one_folder(db, tmp_path):
    collection = db.add_collection(["one", "two"], "one + two")
    first, second = db.collection_sources(collection["collection_id"])
    db.store_source_posts(first["source_id"], collection["collection_id"], [post(1)])
    db.store_source_posts(second["source_id"], collection["collection_id"], [post(1)])
    rows = db.posts_to_download_for_collections([collection["collection_id"]])

    class Response:
        def iter_content(self, chunk_size):
            yield b"abc"

        def close(self):
            pass

    class Client:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    client = Client()
    result = download_post(
        db, client, rows[0], tmp_path / "downloads", EventLogger(tmp_path / "logs")
    )
    assert client.calls == 1
    assert result.local_path == tmp_path / "downloads" / "one + two" / "1.jpg"
    assert list((tmp_path / "downloads" / "one + two").glob("*.jpg")) == [
        result.local_path
    ]
    finalized = db.get_collection(collection["collection_id"])
    assert finalized["folder_finalized"] == 1
    assert finalized["folder_name"] == "one + two"


def test_collection_add_preserves_order_deduplicates_and_is_idempotent(db):
    first = ensure_collection(db, ["karory", "karomix", "karory"])
    second = ensure_collection(db, ["karory", "karomix"])
    reversed_collection = ensure_collection(db, ["karomix", "karory"])

    assert first["collection_id"] == second["collection_id"]
    assert reversed_collection["collection_id"] != first["collection_id"]
    assert [row["tag_query"] for row in db.collection_sources(first["collection_id"])] == [
        "karory", "karomix",
    ]
    assert db.collection_summary(first["collection_id"]) == "karory + karomix"
    assert canonical_source_signature(["ab", "c"]) != canonical_source_signature(["a", "bc"])


def test_invalid_collection_batch_is_prevalidated_without_mutation(db):
    with pytest.raises(ValueError):
        ensure_collection(db, ["valid", "bad\x1bsource"])
    assert db.list_collections() == []


def test_complete_filtered_sources_are_sent_as_independent_unchanged_queries(db):
    collection = db.add_collection(
        ["karory rating:safe", "karomix rating:safe"], "filtered"
    )
    calls = []

    class Api:
        page_size = 100

        def page(self, tags, _page, _limit):
            calls.append(tags)
            return []

    for source in db.collection_sources(collection["collection_id"]):
        incremental_check(db, Api(), source, limit=10, known_stop_count=2)

    assert calls == ["karory rating:safe", "karomix rating:safe"]


def test_source_cursors_and_memberships_are_independent(db):
    collection = db.add_collection(["one", "two"], "one + two")
    first, second = db.collection_sources(collection["collection_id"])
    db.store_source_posts(first["source_id"], collection["collection_id"], [post(9)])

    first, second = db.collection_sources(collection["collection_id"])
    assert first["highest_seen_post_id"] == 9
    assert first["last_checked_at"] is not None
    assert second["highest_seen_post_id"] is None
    assert second["last_checked_at"] is None
    assert db.source_post_ids(first["source_id"]) == {9}
    assert db.source_post_ids(second["source_id"]) == set()


def test_partial_source_failure_preserves_failed_state_and_successful_union(
    tmp_path, monkeypatch
):
    config = load_config(config_at(tmp_path))
    with Database(config.storage.database, config.storage.downloads) as database:
        collection = database.add_collection(["one", "two"], "one + two")
        _first, second = database.collection_sources(collection["collection_id"])
        database.store_source_posts(
            second["source_id"], collection["collection_id"], [post(1)]
        )
        before = database.collection_sources(collection["collection_id"])[1]

        def fake_incremental(db, _api, source, **_kwargs):
            if source["tag_query"] == "two":
                raise OperationalError("source unavailable")
            new = db.store_source_posts(
                source["source_id"], source["collection_id"], [post(2)]
            )
            return CheckResult([post(2)], new, 1)

        class Client:
            def __init__(self, _config):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        monkeypatch.setattr(cli_module, "require_doctor", lambda _config: None)
        monkeypatch.setattr(cli_module, "SafeHttpClient", Client)
        monkeypatch.setattr(cli_module, "incremental_check", fake_incremental)

        assert do_sync(config, database, str(collection["collection_id"]), 0) == (0, 1)
        after = database.collection_sources(collection["collection_id"])[1]
        assert (after["last_checked_at"], after["highest_seen_post_id"]) == (
            before["last_checked_at"], before["highest_seen_post_id"],
        )
        assert database.source_post_ids(second["source_id"]) == {1}
        assert {row[0] for row in database.connection.execute(
            "SELECT post_id FROM collection_posts"
        )} == {1, 2}
        run = database.last_run("sync")
        assert run["result"] == "partial" and run["failed_count"] == 1


def test_text_selector_rejects_source_used_by_multiple_collections(tmp_path, capsys):
    config_path = config_at(tmp_path)
    assert main(["--config", str(config_path), "query", "add", "karory"]) == 0
    assert main([
        "--config", str(config_path), "query", "add", "karory", "karomix"
    ]) == 0

    assert main(["--config", str(config_path), "query", "disable", "karory"]) == 2
    assert "ambiguous" in capsys.readouterr().err
    assert main(["--config", str(config_path), "query", "disable", "2"]) == 0


def test_limit_counts_distinct_collection_materializations(db):
    collection = db.add_collection(["one", "two"], "one + two")
    first, second = db.collection_sources(collection["collection_id"])
    db.store_source_posts(first["source_id"], collection["collection_id"], [post(1), post(2)])
    db.store_source_posts(second["source_id"], collection["collection_id"], [post(1)])

    queued = db.posts_to_download_for_collections([collection["collection_id"]], limit=1)
    assert len(queued) == 1
    assert db.connection.execute("SELECT COUNT(*) FROM collection_posts").fetchone()[0] == 2


def test_status_counts_sources_separately_from_materializations(db):
    collection = db.add_collection(["one", "two"], "one + two")
    first, second = db.collection_sources(collection["collection_id"])
    db.store_source_posts(first["source_id"], collection["collection_id"], [post(1)])
    db.store_source_posts(second["source_id"], collection["collection_id"], [post(1)])
    counts = db.status_counts()
    assert counts["collections"] == 1
    assert counts["enabled_collections"] == 1
    assert counts["sources"] == 2
    assert counts["total"] == 1
    assert counts["materializations"] == 1


def test_first_source_alone_controls_artist_mapping_and_folder_display(db):
    db.set_artist_name("karomix", "梱枝りこ")
    collection = db.add_collection(["karory", "karomix"], "placeholder")
    assert prepare_collection_folder(db, collection)["folder_name"] == "karory + karomix"

    db.set_artist_name("karory", "梱枝りこ")
    assert prepare_collection_folder(db, collection)["folder_name"] == (
        "梱枝りこ karory + karomix"
    )


def test_attempt_numbers_are_scoped_to_collection_materialization(db):
    collection = db.add_collection(["one", "two"], "one + two")
    first, second = db.collection_sources(collection["collection_id"])
    db.store_source_posts(first["source_id"], collection["collection_id"], [post(1)])
    db.store_source_posts(second["source_id"], collection["collection_id"], [post(1)])

    assert db.next_attempt(collection["collection_id"], 1) == 1
    db.start_download_event(
        1, 1, 3, "900150983cd24fb0d6963f7d28e17f72",
        collection_id=collection["collection_id"],
    )
    assert db.next_attempt(collection["collection_id"], 1) == 2
    assert db.connection.execute("SELECT COUNT(*) FROM collection_posts").fetchone()[0] == 1


def test_v4_to_v5_preserves_single_query_state_and_never_touches_image(tmp_path):
    root = tmp_path / "pictures"
    image = root / "existing-folder" / "1.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"abc")
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V4)
    connection.execute("PRAGMA user_version=4")
    connection.execute(
        """INSERT INTO queries VALUES(
        7,'karory','existing-folder',1,'created','checked',123,0)"""
    )
    connection.execute(
        """INSERT INTO posts VALUES(
        1,'1.jpg','jpg',1,1,3,'900150983cd24fb0d6963f7d28e17f72',
        'https://files.yande.re/1.jpg','tag','',NULL,'seen')"""
    )
    connection.execute(
        """INSERT INTO query_posts VALUES(
        7,1,'seen','downloaded-at','existing-folder/1.jpg',NULL,'downloaded')"""
    )
    connection.execute(
        "INSERT INTO artist_names VALUES('karory','梱枝りこ','updated')"
    )
    connection.execute(
        """INSERT INTO download_events(
        post_id,query_id,attempt,started_at,expected_size,expected_md5,result)
        VALUES(1,7,1,'started',3,'900150983cd24fb0d6963f7d28e17f72','downloaded')"""
    )
    connection.commit()
    connection.close()
    before = image.read_bytes()
    backups = tmp_path / "backups"

    with Database(path, root, backup_dir=backups) as database:
        assert database.schema_version() == SCHEMA_VERSION == 5
        collection = database.get_collection(7)
        source = database.collection_sources(7)[0]
        materialization = database.collection_posts(7)[0]
        assert (collection["folder_name"], collection["folder_finalized"],
                collection["enabled"]) == ("existing-folder", 1, 0)
        assert (source["tag_query"], source["position"], source["last_checked_at"],
                source["highest_seen_post_id"]) == ("karory", 0, "checked", 123)
        assert database.source_post_ids(source["source_id"]) == {1}
        assert (materialization["status"], materialization["relative_path"]) == (
            "downloaded", "existing-folder/1.jpg",
        )
        assert database.get_artist_name("karory")["display_name"] == "梱枝りこ"
        assert database.connection.execute(
            "SELECT collection_id FROM download_events"
        ).fetchone()[0] == 7
        assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert image.read_bytes() == before
    assert len(list(backups.glob("yande-sync-v4-*.db"))) == 1


def test_v4_migration_does_not_group_existing_queries(tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V4)
    connection.execute("PRAGMA user_version=4")
    connection.execute("INSERT INTO queries VALUES(1,'one','one',0,'now',NULL,NULL,1)")
    connection.execute("INSERT INTO queries VALUES(2,'two','two',0,'now',NULL,NULL,1)")
    connection.commit()
    connection.close()

    with Database(path, tmp_path / "pictures") as database:
        assert len(database.list_collections()) == 2
        assert [database.collection_summary(item["collection_id"])
                for item in database.list_collections()] == ["one", "two"]
