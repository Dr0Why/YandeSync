from __future__ import annotations

import hashlib

import pytest

import yande_sync.cli as cli_module
from yande_sync.cli import do_sync, main, prepare_query_folder
from yande_sync.compare import CheckResult
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import Database
from yande_sync.downloader import download_post
from yande_sync.logger import EventLogger
from yande_sync.models import Post
from yande_sync.security import SecurityError, safe_folder_name


class Response:
    def __init__(self, data: bytes):
        self.data = data

    def iter_content(self, chunk_size):
        yield self.data

    def close(self):
        pass


class Client:
    def __init__(self, data: bytes):
        self.data = data

    def get(self, *_args, **_kwargs):
        return Response(self.data)


def config_at(tmp_path):
    path = tmp_path / "app" / "config.toml"
    bootstrap_config(path)
    return path


def test_artist_name_persistence_update_list_unset_and_reopen(tmp_path):
    path = tmp_path / "state.db"
    with Database(path) as database:
        stored = database.set_artist_name("korie_riko", "梱枝りこ")
        assert stored["display_name"] == "梱枝りこ"
        assert database.get_artist_name("korie_riko")["display_name"] == "梱枝りこ"
        database.set_artist_name("korie_riko", "梱枝 りこ")
        assert [tuple(row)[:2] for row in database.list_artist_names()] == [
            ("korie_riko", "梱枝 りこ")
        ]

    with Database(path) as database:
        assert database.get_artist_name("korie_riko")["display_name"] == "梱枝 りこ"
        assert database.unset_artist_name("korie_riko") is True
        assert database.get_artist_name("korie_riko") is None
        assert database.unset_artist_name("korie_riko") is False


def test_local_mapping_uses_first_tag_and_complete_original_query(db):
    db.set_artist_name("korie_riko", "梱枝りこ")
    expected = {
        "korie_riko": "梱枝りこ korie_riko",
        "korie_riko seifuku": "梱枝りこ korie_riko seifuku",
        "korie_riko original long_hair": "梱枝りこ korie_riko original long_hair",
        "seifuku korie_riko": "seifuku korie_riko",
    }
    for tag_query, folder_name in expected.items():
        query = db.add_query(tag_query, safe_folder_name(tag_query))
        assert prepare_query_folder(db, query)["folder_name"] == folder_name


def test_mapping_before_first_materialization_creates_only_prefixed_folder(db, tmp_path):
    data = b"abc"
    root = tmp_path / "downloads"
    db.download_root = root.resolve()
    query = db.add_query("korie_riko", "korie_riko")
    assert not root.exists()
    db.set_artist_name("korie_riko", "梱枝りこ")
    assert not root.exists()
    query = prepare_query_folder(db, query)
    assert query["folder_name"] == "梱枝りこ korie_riko"
    assert not root.exists()
    item = Post(
        1, "1.jpg", "jpg", 1, 1, len(data),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(query["query_id"], [item])
    row = dict(db.query_posts(query["query_id"])[0])
    row["folder_name"] = query["folder_name"]

    result = download_post(
        db, Client(data), row, root, EventLogger(tmp_path / "logs")
    )

    assert result.result == "downloaded"
    assert (root / "梱枝りこ korie_riko" / "1.jpg").read_bytes() == data
    assert not (root / "korie_riko").exists()
    stored = db.get_query("korie_riko")
    assert stored["folder_finalized"] == 1
    assert stored["folder_name"] == "梱枝りこ korie_riko"


def test_zero_materialization_sync_keeps_folder_eligible_for_later_mapping(
    tmp_path, monkeypatch
):
    data = b"abc"
    root = tmp_path / "downloads"
    config_path = config_at(tmp_path)
    write_download_dir(config_path, root)
    config = load_config(config_path)
    item = Post(
        1, "1.jpg", "jpg", 1, 1, len(data),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    should_materialize = False

    class SyncClient(Client):
        def __init__(self, _network):
            super().__init__(data)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def fake_incremental(database, _api, source, **_kwargs):
        if not should_materialize:
            return CheckResult([], [], 1)
        new_posts = database.store_source_posts(
            int(source["source_id"]), int(source["collection_id"]), [item]
        )
        return CheckResult([item], new_posts, 1)

    monkeypatch.setattr(cli_module, "require_doctor", lambda _config: None)
    monkeypatch.setattr(cli_module, "SafeHttpClient", SyncClient)
    monkeypatch.setattr(cli_module, "incremental_check", fake_incremental)

    with Database(config.storage.database, root) as database:
        database.add_query("korie_riko", "korie_riko")
        assert do_sync(config, database, None, None) == (0, 0)
        after_empty_sync = database.get_query("korie_riko")
        assert after_empty_sync["folder_finalized"] == 0
        assert after_empty_sync["folder_name"] == "korie_riko"
        assert not (root / "korie_riko").exists()

        database.set_artist_name("korie_riko", "梱枝りこ")
        should_materialize = True
        assert do_sync(config, database, None, None) == (1, 0)
        finalized = database.get_query("korie_riko")

    assert finalized["folder_finalized"] == 1
    assert finalized["folder_name"] == "梱枝りこ korie_riko"
    assert (root / "梱枝りこ korie_riko" / "1.jpg").read_bytes() == data
    assert not (root / "korie_riko").exists()


def test_late_update_and_unset_never_rename_finalized_folder(db, tmp_path):
    query = db.add_query("korie_riko", "korie_riko")
    item = Post(
        1, "1.jpg", "jpg", 1, 1, 3,
        hashlib.md5(b"abc", usedforsecurity=False).hexdigest(),
        "https://files.yande.re/1.jpg", "tag", "", None,
    )
    db.store_posts(query["query_id"], [item])
    db.set_materialization_status(
        query["query_id"], 1, "downloaded", relative_path="korie_riko/1.jpg"
    )
    query = db.get_query("korie_riko")
    folder = tmp_path / "korie_riko"
    folder.mkdir()
    image = folder / "1.jpg"
    image.write_bytes(b"abc")

    db.set_artist_name("korie_riko", "梱枝りこ")
    assert prepare_query_folder(db, query)["folder_name"] == "korie_riko"
    db.set_artist_name("korie_riko", "梱枝 りこ")
    assert prepare_query_folder(db, query)["folder_name"] == "korie_riko"
    assert db.unset_artist_name("korie_riko") is True
    assert prepare_query_folder(db, query)["folder_name"] == "korie_riko"
    assert image.read_bytes() == b"abc"


def test_mapping_applies_to_future_unfinalized_queries(db):
    db.set_artist_name("korie_riko", "梱枝りこ")
    first = db.add_query("korie_riko seifuku", "korie_riko seifuku")
    db.set_artist_name("korie_riko", "梱枝 りこ")
    second = db.add_query("korie_riko original", "korie_riko original")
    assert prepare_query_folder(db, first)["folder_name"] == "梱枝 りこ korie_riko seifuku"
    assert prepare_query_folder(db, second)["folder_name"] == "梱枝 りこ korie_riko original"


@pytest.mark.parametrize("name", ["梱枝りこ", "日向悠二", "ひなた ゆうじ", "カタカナ・名前"])
def test_artist_display_name_accepts_japanese_scripts(db, name):
    assert db.set_artist_name("artist", name)["display_name"] == name


@pytest.mark.parametrize("name", ["", "ASCII only", "梱枝\x1bりこ"])
def test_artist_display_name_rejects_invalid_values(db, name):
    with pytest.raises(SecurityError):
        db.set_artist_name("artist", name)


@pytest.mark.parametrize("artist_tag", ["two tags", "../artist", "artist\\name", "tag\x1b"])
def test_artist_tag_rejects_unsafe_mapping_keys(db, artist_tag):
    with pytest.raises(SecurityError):
        db.set_artist_name(artist_tag, "梱枝りこ")


def test_path_like_display_name_still_passes_final_folder_safety(db):
    db.set_artist_name("artist", "梱枝/りこ")
    query = db.add_query("artist", "artist")
    folder = prepare_query_folder(db, query)["folder_name"]
    assert "/" not in folder and "\\" not in folder and "--" in folder


def test_artist_name_cli_set_list_unset_without_network(tmp_path, monkeypatch, capsys):
    config_path = config_at(tmp_path)

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("artist-name commands must not create an HTTP client")

    monkeypatch.setattr(cli_module, "SafeHttpClient", ForbiddenClient)
    prefix = ["--config", str(config_path), "query", "artist-name"]
    assert main([*prefix, "set", "korie_riko", "梱枝りこ"]) == 0
    assert main([*prefix, "list"]) == 0
    output = capsys.readouterr().out
    assert "korie_riko -> 梱枝りこ" in output
    assert main([*prefix, "unset", "korie_riko"]) == 0

    config = load_config(config_path)
    with Database(config.storage.database, read_only=True) as database:
        assert database.list_artist_names() == []
