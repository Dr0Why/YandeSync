from __future__ import annotations

import hashlib
import threading
from collections import Counter

import pytest

import yande_sync.cli as cli_module
from yande_sync.cli import DEFAULT_SYNC_CONCURRENCY, materialize_downloads
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import Database
from yande_sync.logger import EventLogger
from yande_sync.models import Post


class TransferTracker:
    def __init__(self, release_at: int):
        self.release_at = release_at
        self.active = 0
        self.maximum = 0
        self.urls = []
        self.part_paths = set()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def enter(self, url, target_root):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.urls.append(url)
            self.part_paths.update(target_root.rglob("*.part"))
            if self.active >= self.release_at:
                self.release.set()
        assert self.release.wait(timeout=5), "expected transfers did not become active"

    def leave(self):
        with self.lock:
            self.active -= 1


class ControlledResponse:
    def __init__(self, tracker, url, data, target_root):
        self.tracker = tracker
        self.url = url
        self.data = data
        self.target_root = target_root

    def iter_content(self, chunk_size):
        del chunk_size
        self.tracker.enter(self.url, self.target_root)
        try:
            yield self.data
        finally:
            self.tracker.leave()

    def close(self):
        pass


def configured(tmp_path):
    path = tmp_path / "runtime" / "config.toml"
    bootstrap_config(path)
    write_download_dir(path, tmp_path / "downloads")
    return load_config(path)


def seed(database, count):
    collection = database.add_collection(["tag", "tag rating:safe"], "tag")
    first, second = database.collection_sources(collection["collection_id"])
    posts = []
    for post_id in range(1, count + 1):
        data = f"image-{post_id}".encode()
        posts.append(Post(
            post_id, f"{post_id}.jpg", "jpg", 1, 1, len(data),
            hashlib.md5(data, usedforsecurity=False).hexdigest(),
            f"https://files.yande.re/{post_id}.jpg", "tag", "", None,
        ))
    database.store_source_posts(
        first["source_id"], collection["collection_id"], posts
    )
    # Exercise overlapping-source deduplication without creating more materializations.
    database.store_source_posts(
        second["source_id"], collection["collection_id"], posts[:2]
    )
    return database.posts_to_download_for_collections([collection["collection_id"]])


def run_controlled(tmp_path, monkeypatch, *, count, concurrency, bad_post=None):
    config = configured(tmp_path)
    tracker = TransferTracker(min(count, concurrency))

    class Client:
        def __init__(self, _network):
            pass

        def get(self, url, **_kwargs):
            post_id = int(url.rsplit("/", 1)[1].split(".", 1)[0])
            data = b"bad" if post_id == bad_post else f"image-{post_id}".encode()
            return ControlledResponse(tracker, url, data, config.storage.downloads)

        def close(self):
            pass

    monkeypatch.setattr(cli_module, "SafeHttpClient", Client)
    with Database(config.storage.database, config.storage.downloads) as database:
        rows = seed(database, count)
        with database.run("sync", None) as run, EventLogger(config.storage.logs) as logger:
            downloaded, failed = materialize_downloads(
                config, database, rows, config.storage.downloads, logger,
                run_id=run.run_id, concurrency=concurrency,
            )
        states = database.connection.execute(
            "SELECT status,COUNT(*) FROM collection_posts GROUP BY status"
        ).fetchall()
        events = database.connection.execute(
            "SELECT post_id,attempt,result FROM download_events ORDER BY post_id"
        ).fetchall()
        collection = database.list_collections()[0]
    return tracker, downloaded, failed, dict(states), events, collection, config


@pytest.mark.parametrize(("concurrency", "expected_max"), [(1, 1), (2, 2), (8, 8)])
def test_bounded_concurrency_and_exactly_once_completion(
    tmp_path, monkeypatch, concurrency, expected_max
):
    tracker, downloaded, failed, states, events, collection, config = run_controlled(
        tmp_path, monkeypatch, count=8, concurrency=concurrency
    )
    assert tracker.maximum == expected_max
    assert len(downloaded) == 8 and failed == 0 and states == {"downloaded": 8}
    assert Counter(tracker.urls).most_common(1)[0][1] == 1
    assert len(events) == 8 and all(row[1:] == (1, "downloaded") for row in events)
    assert len(tracker.part_paths) == 8
    assert not list(config.storage.downloads.rglob("*.part"))
    assert collection["folder_finalized"] == 1 and collection["folder_name"] == "tag"
    assert {path.parent.name for _row, path in downloaded} == {"tag"}


def test_default_concurrency_constant_permits_multiple_but_never_more_than_eight(
    tmp_path, monkeypatch
):
    tracker, downloaded, failed, *_ = run_controlled(
        tmp_path, monkeypatch, count=10, concurrency=DEFAULT_SYNC_CONCURRENCY
    )
    assert tracker.maximum == 8
    assert len(downloaded) == 10 and failed == 0


def test_one_failure_does_not_cancel_unrelated_downloads(tmp_path, monkeypatch):
    tracker, downloaded, failed, states, events, *_ = run_controlled(
        tmp_path, monkeypatch, count=8, concurrency=8, bad_post=4
    )
    assert tracker.maximum == 8
    assert len(downloaded) == 7 and failed == 1
    assert states == {"corrupt": 1, "downloaded": 7}
    assert Counter(row[2] for row in events) == {"downloaded": 7, "corrupt": 1}
