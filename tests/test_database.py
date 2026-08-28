import sqlite3

import pytest

from yande_sync.models import Post
from yande_sync.security import canonical_source_signature


def post(post_id=1):
    return Post(post_id, f"{post_id}.jpg", "jpg", 100, 200, 3, "900150983cd24fb0d6963f7d28e17f72",
                f"https://files.yande.re/a/{post_id}.jpg", "tag", "https://source.invalid", None)


def test_same_post_not_duplicated(db):
    query = db.add_query("tag", "tag")
    assert len(db.store_posts(query["query_id"], [post()])) == 1
    assert len(db.store_posts(query["query_id"], [post()])) == 0
    assert len(db.query_posts(query["query_id"])) == 1


def test_post_can_belong_to_two_collections_with_independent_materializations(db):
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    db.store_posts(first["query_id"], [post()])
    db.store_posts(second["query_id"], [post()])
    assert db.connection.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM collection_posts").fetchone()[0] == 2
    queued = db.posts_to_download_for_queries([first["query_id"], second["query_id"]])
    assert [row["post_id"] for row in queued] == [1, 1]
    assert [row["collection_id"] for row in queued] == [
        first["collection_id"], second["collection_id"]
    ]


def test_download_limit_counts_materializations_not_unique_posts(db):
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    db.store_posts(first["query_id"], [post()])
    db.store_posts(second["query_id"], [post()])

    queued = db.posts_to_download_for_queries(
        [first["query_id"], second["query_id"]], limit=1
    )

    assert len(queued) == 1
    assert queued[0]["collection_id"] == first["collection_id"]


def test_collection_source_add_rolls_back_on_database_error(db):
    db.connection.execute(
        """CREATE TRIGGER reject_second_source BEFORE INSERT ON collection_sources
        WHEN NEW.tag_query='two'
        BEGIN SELECT RAISE(ABORT, 'rejected for test'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="rejected for test"):
        db.add_collection(["one", "two"], "one + two")

    assert db.list_collections() == []


def test_attach_sources_rolls_back_as_one_transaction(db):
    collection = db.add_collection(["base"], "base")
    db.connection.execute(
        """CREATE TRIGGER reject_attached_source BEFORE INSERT ON collection_sources
        WHEN NEW.tag_query='rejected'
        BEGIN SELECT RAISE(ABORT, 'rejected for test'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="rejected for test"):
        db.add_sources_to_collection(collection["collection_id"], ["accepted", "rejected"])

    assert [row["tag_query"] for row in db.collection_sources(1)] == ["base"]
    assert db.get_collection(1)["source_signature"] == canonical_source_signature(["base"])


def test_same_query_has_independent_collection_local_source_state(db):
    first = db.add_collection(["shared"], "first")
    second = db.add_collection(["shared", "other"], "second")
    first_source = db.collection_sources(first["collection_id"])[0]
    second_source = db.collection_sources(second["collection_id"])[0]

    db.store_source_posts(first_source["source_id"], first["collection_id"], [post(50)])

    first_source = db.collection_sources(first["collection_id"])[0]
    second_source = db.collection_sources(second["collection_id"])[0]
    assert first_source["source_id"] != second_source["source_id"]
    assert first_source["highest_seen_post_id"] == 50
    assert second_source["highest_seen_post_id"] is None


def test_downloaded_post_is_requeued_for_another_query(db, tmp_path):
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    db.store_posts(first["query_id"], [post()])
    db.set_materialization_status(
        first["query_id"], 1, "downloaded", local_path=tmp_path / "downloads" / "one" / "1.jpg"
    )
    db.store_posts(second["query_id"], [post()])

    queued = db.posts_to_download_for_queries([first["query_id"], second["query_id"]])
    assert len(queued) == 1
    assert queued[0]["collection_id"] == second["collection_id"]


def test_status_updates_only_on_request(db, tmp_path):
    query = db.add_query("tag", "tag")
    db.store_posts(query["query_id"], [post()])
    db.set_materialization_status(
        query["query_id"], 1, "downloaded", local_path=tmp_path / "downloads" / "tag" / "1.jpg"
    )
    row = db.query_posts(query["query_id"])[0]
    assert row["status"] == "downloaded"
    assert row["downloaded_at"]
    assert row["relative_path"] == str(__import__("pathlib").Path("tag") / "1.jpg")
