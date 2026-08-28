from __future__ import annotations

import hashlib

from yande_sync.downloader import download_post
from yande_sync.logger import EventLogger
from yande_sync.models import Post


class Response:
    def iter_content(self, chunk_size):
        yield b"abc"

    def close(self):
        pass


class Client:
    def get(self, *_args, **_kwargs):
        return Response()


def test_multi_query_download_event_has_direct_execution_context(db, tmp_path):
    first = db.add_query("one", "one")
    second = db.add_query("two", "two")
    item = Post(
        1, "1.jpg", "jpg", 1, 1, 3,
        hashlib.md5(b"abc", usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(first["query_id"], [item])
    db.store_posts(second["query_id"], [item])
    first_row = db.query_posts(first["query_id"])[0]
    second_row = db.query_posts(second["query_id"])[0]

    with db.run("sync", None) as run:
        result = download_post(
            db, Client(), second_row, tmp_path / "downloads", EventLogger(tmp_path / "logs"),
            run_id=run.run_id, collection_id=second["collection_id"],
        )
    assert result.result == "downloaded"
    event = db.connection.execute(
        "SELECT run_id,collection_id,attempt FROM download_events"
    ).fetchone()
    assert (event["run_id"], event["collection_id"], event["attempt"]) == (
        run.run_id, second["collection_id"], 1,
    )
    assert db.recent_events(1)[0]["collection_id"] == second["collection_id"]

    download_post(
        db, Client(), first_row, tmp_path / "downloads", EventLogger(tmp_path / "logs"),
        collection_id=first["collection_id"],
    )
    attempts = db.connection.execute(
        "SELECT attempt FROM download_events ORDER BY event_id"
    ).fetchall()
    assert [attempt[0] for attempt in attempts] == [1, 1]
