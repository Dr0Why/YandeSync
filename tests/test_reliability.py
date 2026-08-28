from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import yande_sync.downloader as downloader_module
from yande_sync.database import Database
from yande_sync.downloader import (
    _atomic_no_replace,
    _owned_identity,
    _remove_owned_part,
    download_post,
)
from yande_sync.locking import OperationLock, OperationLockError
from yande_sync.logger import EventLogger
from yande_sync.models import Post


class Response:
    def __init__(self, chunks, *, interrupt=False):
        self.chunks = chunks
        self.interrupt = interrupt

    def iter_content(self, chunk_size):
        yield from self.chunks
        if self.interrupt:
            raise KeyboardInterrupt

    def close(self):
        pass


class Client:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


def add_post(db: Database, *, data=b"abc"):
    query = db.add_query("tag", "tag")
    item = Post(
        1, "1.jpg", "jpg", 1, 1, len(data),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(query["query_id"], [item])
    return query, db.query_posts(query["query_id"])[0]


def test_corrupt_and_abandoned_downloads_are_recoverable(db):
    query, row = add_post(db)
    db.set_materialization_status(query["query_id"], row["post_id"], "corrupt")
    assert [item["post_id"] for item in db.posts_to_download(query["query_id"])] == [1]
    db.set_materialization_status(query["query_id"], row["post_id"], "downloading")
    event_id = db.start_download_event(
        row["post_id"], 1, row["file_size"], row["md5"],
        collection_id=query["collection_id"]
    )
    assert db.posts_to_download(query["query_id"]) == []
    assert db.recover_abandoned_downloads() == 1
    assert db.posts_to_download(query["query_id"])[0]["status"] == "pending"
    event = db.connection.execute(
        "SELECT result,finished_at,error_type FROM download_events WHERE event_id=?", (event_id,)
    ).fetchone()
    assert (event["result"], event["error_type"]) == ("interrupted", "ProcessInterrupted")
    assert event["finished_at"]


def test_reopen_recovery_finalizes_run_event_and_post(tmp_path):
    root = tmp_path / "pictures"
    path = tmp_path / "state.db"
    with Database(path, root) as database:
        query, row = add_post(database)
        run_id = database.start_run("sync", query["tag_query"])
        database.set_materialization_status(
            query["query_id"], row["post_id"], "downloading"
        )
        event_id = database.start_download_event(
            row["post_id"], 1, row["file_size"], row["md5"],
            run_id=run_id, collection_id=query["collection_id"],
        )

    with Database(path, root) as database:
        assert database.recover_abandoned_downloads() == 1
        run = database.connection.execute(
            "SELECT result,finished_at FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        event = database.connection.execute(
            "SELECT result,finished_at FROM download_events WHERE event_id=?", (event_id,)
        ).fetchone()
        post = database.connection.execute(
            """SELECT status,download_started_at FROM collection_posts
            WHERE post_id=1"""
        ).fetchone()

    assert run["result"] == "interrupted" and run["finished_at"]
    assert event["result"] == "interrupted" and event["finished_at"]
    assert post["status"] == "pending" and post["download_started_at"] is None


def test_operation_lock_is_exclusive_and_reusable(tmp_path):
    path = tmp_path / "operation.lock"
    with OperationLock(path), pytest.raises(OperationLockError), OperationLock(path):
        pass
    with OperationLock(path):
        pass


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows named mutex")
def test_operation_lock_is_exclusive_across_processes_without_creating_file(tmp_path):
    path = tmp_path / "operation.lock"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from yande_sync.locking import OperationLock\n"
        "with OperationLock(Path(sys.argv[1])):\n"
        " print('READY', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(OperationLockError), OperationLock(path):
            pass
        assert not path.exists()
    finally:
        if process.stdin is not None:
            process.stdin.write("\n")
            process.stdin.flush()
        process.wait(timeout=10)


def test_run_lifecycle_records_success_failure_and_interrupt(tmp_path):
    with Database(tmp_path / "state.db", tmp_path / "downloads") as database:
        with database.run("sync", "tag") as run:
            run.update(downloaded_count=2)
        with pytest.raises(ValueError), database.run("sync", "tag"):
            raise ValueError("failure")
        with pytest.raises(KeyboardInterrupt), database.run("sync", "tag"):
            raise KeyboardInterrupt
        rows = database.connection.execute(
            "SELECT result,finished_at FROM runs ORDER BY run_id"
        ).fetchall()
    assert [row["result"] for row in rows] == ["ok", "failed", "interrupted"]
    assert all(row["finished_at"] for row in rows)


def test_keyboard_interrupt_returns_post_to_pending_and_finishes_event(db, tmp_path):
    _query, row = add_post(db, data=b"ab")
    target = tmp_path / "downloads"
    with pytest.raises(KeyboardInterrupt):
        download_post(
            db, Client(Response([b"a"], interrupt=True)), row, target,
            EventLogger(tmp_path / "logs"),
        )
    post = db.connection.execute(
        "SELECT status FROM collection_posts WHERE post_id=1"
    ).fetchone()
    event = db.connection.execute(
        "SELECT result,finished_at,error_type FROM download_events WHERE post_id=1"
    ).fetchone()
    assert post["status"] == "pending"
    assert (event["result"], event["error_type"]) == ("interrupted", "KeyboardInterrupt")
    assert event["finished_at"]
    assert not list((target / "tag").glob("*.part"))


def test_interrupted_local_copy_is_recoverable(db, tmp_path, monkeypatch):
    _query, row = add_post(db, data=b"abc")
    target = tmp_path / "downloads"
    source = tmp_path / "legacy" / "1.jpg"
    source.parent.mkdir()
    source.write_bytes(b"abc")

    def interrupt_copy(self, chunk_size):
        yield b"a"
        raise KeyboardInterrupt

    monkeypatch.setattr(downloader_module._LocalResponse, "iter_content", interrupt_copy)
    with pytest.raises(KeyboardInterrupt):
        download_post(
            db, Client(Response([b"network must not be used"])), row, target,
            EventLogger(tmp_path / "logs"), local_source=source,
        )

    state = db.connection.execute(
        "SELECT status FROM collection_posts WHERE post_id=1"
    ).fetchone()[0]
    assert state == "pending"
    assert source.read_bytes() == b"abc"
    assert not list((target / "tag").glob("*.part"))


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows rename semantics")
def test_finalize_race_preserves_independent_file(db, tmp_path, monkeypatch):
    _query, row = add_post(db)
    target = tmp_path / "downloads"

    def collide(_handle: int, destination: Path):
        destination.write_bytes(b"independent")
        raise FileExistsError

    monkeypatch.setattr("yande_sync.downloader._rename_open_windows_file", collide)
    result = download_post(
        db, Client(Response([b"abc"])), row, target, EventLogger(tmp_path / "logs")
    )
    assert result.result == "corrupt"
    assert (target / "tag" / "1.jpg").read_bytes() == b"independent"
    assert not list((target / "tag").glob("*.part"))


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows rename semantics")
def test_handle_bound_finalize_is_no_replace(tmp_path):
    source = tmp_path / ".private.part"
    destination = tmp_path / "final.jpg"
    with source.open("xb") as handle:
        handle.write(b"owned")
        identity = _owned_identity(handle)
    destination.write_bytes(b"independent")

    with pytest.raises(FileExistsError):
        _atomic_no_replace(
            source, destination, identity, len(b"owned"),
            hashlib.md5(b"owned", usedforsecurity=False).hexdigest(),
        )

    assert source.read_bytes() == b"owned"
    assert destination.read_bytes() == b"independent"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows handle finalization")
def test_finalization_rejects_part_mutated_after_stream_validation(db, tmp_path, monkeypatch):
    _query, row = add_post(db)
    target = tmp_path / "downloads"
    original = downloader_module._atomic_no_replace

    def mutate_then_finalize(source, destination, identity, expected_size, expected_md5):
        source.write_bytes(b"abd")
        return original(source, destination, identity, expected_size, expected_md5)

    monkeypatch.setattr(downloader_module, "_atomic_no_replace", mutate_then_finalize)
    result = download_post(
        db, Client(Response([b"abc"])), row, target, EventLogger(tmp_path / "logs")
    )

    assert result.result == "corrupt"
    assert not (target / "tag" / "1.jpg").exists()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows handle finalization")
def test_finalization_denies_concurrent_part_writer(db, tmp_path, monkeypatch):
    _query, row = add_post(db)
    target = tmp_path / "downloads"
    original = downloader_module._rename_open_windows_file

    def assert_write_denied(handle, destination):
        part = next((target / "tag").glob("*.part"))
        with pytest.raises(PermissionError):
            part.open("r+b")
        original(handle, destination)

    monkeypatch.setattr(downloader_module, "_rename_open_windows_file", assert_write_denied)
    result = download_post(
        db, Client(Response([b"abc"])), row, target, EventLogger(tmp_path / "logs")
    )
    assert result.result == "downloaded"


def test_cleanup_failure_cannot_leave_download_event_running(db, tmp_path, monkeypatch):
    _query, row = add_post(db)
    target = tmp_path / "downloads"
    monkeypatch.setattr(
        downloader_module, "_remove_owned_part",
        lambda *_args: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )

    result = download_post(
        db, Client(Response([b"abd"])), row, target, EventLogger(tmp_path / "logs")
    )
    event = db.connection.execute(
        "SELECT result,finished_at,error_message FROM download_events WHERE post_id=1"
    ).fetchone()
    assert result.result == "corrupt"
    assert event["result"] == "corrupt"
    assert event["finished_at"]
    assert "cleanup_error=cleanup denied" in event["error_message"]
    assert len(list((target / "tag").glob("*.part"))) == 1


def test_initial_logger_failure_cannot_strand_post_or_event(db, tmp_path):
    _query, row = add_post(db)

    class FailsOnceLogger:
        calls = 0

        def event(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("log unavailable")

    result = download_post(
        db, Client(Response([b"abc"])), row, tmp_path / "downloads", FailsOnceLogger()
    )
    post = db.connection.execute(
        "SELECT status FROM collection_posts WHERE post_id=1"
    ).fetchone()
    event = db.connection.execute(
        "SELECT result,finished_at,error_type FROM download_events WHERE post_id=1"
    ).fetchone()
    assert result.result == "failed"
    assert post["status"] == "failed"
    assert (event["result"], event["error_type"]) == ("failed", "OSError")
    assert event["finished_at"]


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows handle deletion")
def test_owned_cleanup_does_not_delete_path_replacement(tmp_path, monkeypatch):
    part = tmp_path / ".1.jpg.private.part"
    with part.open("xb") as handle:
        handle.write(b"owned")
        identity = _owned_identity(handle)
    displaced = tmp_path / "owned-displaced.part"
    original_delete = downloader_module._delete_open_windows_file

    def replace_then_delete(handle):
        part.rename(displaced)
        part.write_bytes(b"replacement")
        original_delete(handle)

    monkeypatch.setattr(downloader_module, "_delete_open_windows_file", replace_then_delete)
    _remove_owned_part(part, identity)

    assert part.read_bytes() == b"replacement"
    assert not displaced.exists()
