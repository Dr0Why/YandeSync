import hashlib
import os
from types import SimpleNamespace

import yande_sync.downloader as downloader_module
from yande_sync.cli import (
    reconcile_missing_downloads,
    resolve_download_dir,
    reusable_local_source,
    verify_new_downloads,
)
from yande_sync.config import StorageConfig
from yande_sync.downloader import download_post
from yande_sync.logger import EventLogger
from yande_sync.models import Post


class Response:
    def __init__(self, data):
        self.chunks = data if isinstance(data, list) else [data]
        self.yielded = 0

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def close(self):
        pass


class Client:
    def __init__(self, data):
        self.data = data
        self.urls = []
        self.response = None

    def get(self, url, **_kwargs):
        self.urls.append(url)
        self.response = Response(self.data)
        return self.response


def setup_row(db, data):
    query = db.add_query("tag", "tag")
    item = Post(1, "1.jpg", "jpg", 1, 1, len(data), hashlib.md5(data, usedforsecurity=False).hexdigest(),
                "https://files.yande.re/1.jpg", "tag", "", None)
    db.store_posts(query["query_id"], [item])
    return db.query_posts(query["query_id"])[0]


def test_download_uses_part_then_atomic_rename(db, tmp_path):
    row = setup_row(db, b"abc")
    client = Client(b"abc")
    result = download_post(db, client, row, tmp_path / "downloads", EventLogger(tmp_path / "logs"))
    assert result.result == "downloaded"
    assert result.local_path.read_bytes() == b"abc"
    assert result.local_path.parent == tmp_path / "downloads" / "tag"
    assert not list((tmp_path / "downloads" / "tag").glob("*.part"))
    assert client.urls == ["https://files.yande.re/1.jpg"]


def test_bad_md5_never_creates_final_file(db, tmp_path):
    row = setup_row(db, b"abc")
    result = download_post(db, Client(b"abd"), row, tmp_path / "downloads", EventLogger(tmp_path / "logs"))
    assert result.result == "corrupt"
    assert not (tmp_path / "downloads" / "tag" / "1.jpg").exists()
    assert not list((tmp_path / "downloads" / "tag").glob("*.part"))


def test_download_directory_override_is_current_operation_only(db, tmp_path):
    config = SimpleNamespace(
        storage=StorageConfig(tmp_path / "archive", tmp_path / "archive" / "downloads")
    )
    selected = resolve_download_dir(config, db, tmp_path / "pictures")
    assert selected == (tmp_path / "pictures").resolve()
    assert resolve_download_dir(config, db) == config.storage.downloads.resolve()


def test_missing_downloaded_file_is_returned_to_download_queue(db, tmp_path):
    row = setup_row(db, b"abc")
    db.set_materialization_status(
        row["query_id"], row["post_id"], "downloaded",
        local_path=tmp_path / "downloads" / row["file_name"],
    )
    recorded = db.query_posts(db.get_query("tag")["query_id"])

    assert reconcile_missing_downloads(db, recorded) == 1
    queued = db.posts_to_download(db.get_query("tag")["query_id"])
    assert [item["post_id"] for item in queued] == [row["post_id"]]
    assert queued[0]["status"] == "missing"


def test_new_download_is_rechecked_and_corruption_updates_status(db, tmp_path, capsys):
    row = setup_row(db, b"abc")
    path = tmp_path / "1.jpg"
    path.write_bytes(b"abd")
    ok, bad = verify_new_downloads(db, [(row, path)])
    assert (ok, bad) == (0, 1)
    assert db.connection.execute(
        "SELECT status FROM collection_posts WHERE post_id=1"
    ).fetchone()[0] == "corrupt"
    assert "[VERIFY FAILED] 1.jpg" in capsys.readouterr().out


def test_oversized_response_stops_before_writing_extra_chunks(db, tmp_path):
    row = setup_row(db, b"abc")
    client = Client([b"abcd", b"more-data-that-must-not-be-read"])
    result = download_post(
        db, client, row, tmp_path / "downloads", EventLogger(tmp_path / "logs")
    )
    assert result.result == "corrupt"
    assert client.response.yielded == 1
    assert not (tmp_path / "downloads" / "tag" / "1.jpg").exists()
    assert not list((tmp_path / "downloads" / "tag").glob("*.part"))


def test_part_hardlink_is_rejected_without_touching_victim(db, tmp_path):
    row = setup_row(db, b"abc")
    target = tmp_path / "downloads"
    target.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do-not-touch")
    query_target = target / "tag"
    query_target.mkdir()
    unrelated_part = query_target / "1.jpg.part"
    os.link(victim, unrelated_part)
    result = download_post(db, Client(b"abc"), row, target, EventLogger(tmp_path / "logs"))
    assert result.result == "downloaded"
    assert victim.read_bytes() == b"do-not-touch"
    assert unrelated_part.exists()


def test_existing_final_file_is_never_overwritten(db, tmp_path):
    row = setup_row(db, b"abc")
    target = tmp_path / "downloads"
    target.mkdir()
    query_target = target / "tag"
    query_target.mkdir()
    final = query_target / "1.jpg"
    final.write_bytes(b"user-file")
    result = download_post(db, Client(b"abc"), row, target, EventLogger(tmp_path / "logs"))
    assert result.result == "corrupt"
    assert final.read_bytes() == b"user-file"


def test_verified_managed_corrupt_file_is_atomically_repaired(db, tmp_path):
    row = setup_row(db, b"abc")
    target = tmp_path / "downloads"
    final = target / "tag" / "1.jpg"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"bad")
    db.set_materialization_status(
        row["collection_id"], row["post_id"], "downloaded", local_path=final
    )
    db.set_materialization_status(row["collection_id"], row["post_id"], "corrupt")
    row = db.collection_posts(row["collection_id"])[0]

    result = download_post(db, Client(b"abc"), row, target, EventLogger(tmp_path / "logs"))

    assert result.result == "downloaded"
    assert final.read_bytes() == b"abc"
    assert not list(final.parent.glob("*.part"))


def test_corrupt_status_without_expected_managed_path_does_not_allow_overwrite(db, tmp_path):
    row = setup_row(db, b"abc")
    target = tmp_path / "downloads"
    final = target / "tag" / "1.jpg"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"user-file")
    db.set_materialization_status(row["collection_id"], row["post_id"], "corrupt")
    row = db.collection_posts(row["collection_id"])[0]

    result = download_post(db, Client(b"abc"), row, target, EventLogger(tmp_path / "logs"))

    assert result.result == "corrupt"
    assert final.read_bytes() == b"user-file"


def test_verified_corrupt_file_changed_before_replace_is_preserved(db, tmp_path, monkeypatch):
    row = setup_row(db, b"abc")
    target = tmp_path / "downloads"
    final = target / "tag" / "1.jpg"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"bad")
    db.set_materialization_status(
        row["collection_id"], row["post_id"], "downloaded", local_path=final
    )
    db.set_materialization_status(row["collection_id"], row["post_id"], "corrupt")
    row = db.collection_posts(row["collection_id"])[0]
    original = downloader_module._atomic_replace_corrupt

    def change_then_replace(*args):
        final.write_bytes(b"changed")
        return original(*args)

    monkeypatch.setattr(downloader_module, "_atomic_replace_corrupt", change_then_replace)

    result = download_post(db, Client(b"abc"), row, target, EventLogger(tmp_path / "logs"))

    assert result.result == "corrupt"
    assert final.read_bytes() == b"changed"


def test_same_post_materializes_as_two_independent_query_files(db, tmp_path):
    data = b"abc"
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    item = Post(
        1, "1.jpg", "jpg", 1, 1, len(data),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(first["query_id"], [item])
    db.store_posts(second["query_id"], [item])
    rows = db.posts_to_download_for_queries([first["query_id"], second["query_id"]])
    root = tmp_path / "downloads"

    first_result = download_post(db, Client(data), rows[0], root, EventLogger(tmp_path / "logs"))

    class NoNetwork:
        def get(self, *_args, **_kwargs):
            raise AssertionError("verified local reuse should avoid a second network request")

    second_source = reusable_local_source(db, rows[1], db.expected_path_for(rows[1]))
    second_result = download_post(
        db, NoNetwork(), rows[1], root, EventLogger(tmp_path / "logs"),
        local_source=second_source,
    )
    assert first_result.local_path == root / "one" / "1.jpg"
    assert second_result.local_path == root / "two" / "1.jpg"
    assert first_result.local_path.read_bytes() == second_result.local_path.read_bytes() == data
    assert not os.path.samefile(first_result.local_path, second_result.local_path)
    assert first_result.local_path.stat().st_nlink == second_result.local_path.stat().st_nlink == 1


def test_legacy_flat_file_is_planned_and_lazily_copied(db, tmp_path):
    row = setup_row(db, b"abc")
    root = tmp_path / "downloads"
    root.mkdir()
    legacy = root / "1.jpg"
    legacy.write_bytes(b"abc")
    db.set_materialization_status(
        row["query_id"], row["post_id"], "downloaded", relative_path="1.jpg"
    )
    planned = db.posts_to_download(row["query_id"])
    assert len(planned) == 1
    source = reusable_local_source(db, planned[0], db.expected_path_for(planned[0]))
    result = download_post(
        db, Client(b"must not be used"), planned[0], root, EventLogger(tmp_path / "logs"),
        local_source=source,
    )
    assert result.result == "downloaded"
    assert result.local_path == root / "tag" / "1.jpg"
    assert legacy.read_bytes() == b"abc"


def test_missing_reconciliation_is_isolated_per_query(db, tmp_path):
    data = b"abc"
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    item = Post(
        1, "1.jpg", "jpg", 1, 1, len(data),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(first["query_id"], [item])
    db.store_posts(second["query_id"], [item])
    valid = tmp_path / "downloads" / "two" / "1.jpg"
    valid.parent.mkdir(parents=True)
    valid.write_bytes(data)
    db.set_materialization_status(first["query_id"], 1, "downloaded", relative_path="one/1.jpg")
    db.set_materialization_status(second["query_id"], 1, "downloaded", local_path=valid)
    assert reconcile_missing_downloads(
        db, db.posts_for_queries([first["query_id"], second["query_id"]])
    ) == 1
    states = db.connection.execute(
        """SELECT collection_id,status FROM collection_posts
        ORDER BY collection_id"""
    ).fetchall()
    assert [(row[0], row[1]) for row in states] == [
        (first["query_id"], "missing"), (second["query_id"], "downloaded")
    ]
