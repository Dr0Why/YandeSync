from __future__ import annotations

from pathlib import Path

import pytest

from yande_sync.cli import (
    add_collection_sources,
    ensure_collection,
    main,
    parser,
    remove_collection_sources,
)
from yande_sync.config import bootstrap_config, load_config, write_download_dir
from yande_sync.database import Database
from yande_sync.models import Post


def _post(post_id: int) -> Post:
    return Post(
        post_id, f"{post_id}.jpg", "jpg", 1, 1, 3,
        "900150983cd24fb0d6963f7d28e17f72",
        f"https://files.yande.re/{post_id}.jpg", "tag", "", None,
    )


def _seed(db: Database, root: Path, sources=("A", "B", "C")):
    collection = db.add_collection(list(sources), "Custom Folder")
    first = db.collection_sources(int(collection["collection_id"]))[0]
    db.store_source_posts(int(first["source_id"]), int(collection["collection_id"]), [
        _post(100), _post(101),
    ])
    post_ids = [100, 101]
    source_rows = db.collection_sources(int(collection["collection_id"]))
    if len(source_rows) > 1:
        db.store_source_posts(
            int(source_rows[1]["source_id"]), int(collection["collection_id"]), [_post(102)]
        )
        post_ids.append(102)
    folder = root / "Custom Folder"
    folder.mkdir(parents=True)
    for post_id in post_ids:
        path = folder / f"{post_id}.jpg"
        path.write_bytes(f"image-{post_id}".encode())
        db.set_materialization_status(
            int(collection["collection_id"]), post_id, "downloaded", local_path=path
        )
    return collection


def test_parser_preserves_remove_query_boundaries_and_existing_commands():
    args = parser().parse_args([
        "query", "remove", "--from", "7", "artist rating:safe", "alias",
    ])
    assert (args.collection_id, args.tags) == (7, ["artist rating:safe", "alias"])
    assert parser().parse_args(["query", "add", "A", "B"]).tags == ["A", "B"]
    assert parser().parse_args(["query", "add", "--to", "1", "B"]).to == 1
    assert parser().parse_args(["query", "rename", "1", "Folder"]).query_action == "rename"


def test_remove_preserves_collection_materializations_files_and_remaining_state(db, tmp_path):
    root = tmp_path / "downloads"
    collection = _seed(db, root)
    collection_id = int(collection["collection_id"])
    db.connection.execute(
        "UPDATE collection_sources SET last_checked_at='kept',highest_seen_post_id=999 "
        "WHERE collection_id=? AND tag_query='A'", (collection_id,),
    )
    db.connection.commit()
    remaining_before = tuple(db.collection_sources(collection_id)[0])
    memberships_before = [tuple(row) for row in db.connection.execute(
        "SELECT * FROM collection_posts WHERE collection_id=? ORDER BY post_id",
        (collection_id,),
    )]
    files_before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }

    assert remove_collection_sources(db, collection_id, ["B"]) == ["B"]

    assert db.get_collection(collection_id)["folder_name"] == "Custom Folder"
    assert [row["tag_query"] for row in db.collection_sources(collection_id)] == ["A", "C"]
    assert tuple(db.collection_sources(collection_id)[0]) == remaining_before
    assert [tuple(row) for row in db.connection.execute(
        "SELECT * FROM collection_posts WHERE collection_id=? ORDER BY post_id",
        (collection_id,),
    )] == memberships_before
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    } == files_before


def test_multi_remove_deduplicates_and_future_add_appends_after_position_gap(db, tmp_path):
    root = tmp_path / "downloads"
    collection = _seed(db, root, ("A", "B", "C", "D"))
    collection_id = int(collection["collection_id"])
    assert remove_collection_sources(db, collection_id, ["B", "B", "C"]) == ["B", "C"]
    assert [row["tag_query"] for row in db.collection_sources(collection_id)] == ["A", "D"]
    assert add_collection_sources(db, collection_id, ["E"]) == (["E"], [])
    assert [row["tag_query"] for row in db.collection_sources(collection_id)] == ["A", "D", "E"]


@pytest.mark.parametrize("sources", [("A",), ("A", "B", "C")])
def test_removing_every_source_is_atomic(db, tmp_path, sources):
    root = tmp_path / "downloads"
    collection = _seed(db, root, sources)
    collection_id = int(collection["collection_id"])
    before = [tuple(row) for row in db.collection_sources(collection_id)]
    with pytest.raises(ValueError, match="last source"):
        remove_collection_sources(db, collection_id, list(sources))
    assert [tuple(row) for row in db.collection_sources(collection_id)] == before


def test_missing_source_and_collection_are_strict_and_atomic(db, tmp_path):
    root = tmp_path / "downloads"
    collection = _seed(db, root)
    collection_id = int(collection["collection_id"])
    before = [tuple(row) for row in db.collection_sources(collection_id)]
    with pytest.raises(ValueError, match="not present"):
        remove_collection_sources(db, collection_id, ["B", "Missing"])
    assert [tuple(row) for row in db.collection_sources(collection_id)] == before
    with pytest.raises(ValueError, match="not registered"):
        remove_collection_sources(db, 999, ["A"])


def test_same_query_in_other_collection_is_untouched(db, tmp_path):
    root = tmp_path / "downloads"
    first = _seed(db, root)
    second = db.add_collection(["B", "Other"], "Other Folder")
    second_id = int(second["collection_id"])
    db.connection.execute(
        "UPDATE collection_sources SET last_checked_at='kept',highest_seen_post_id=123 "
        "WHERE collection_id=? AND tag_query='B'", (second_id,),
    )
    db.connection.commit()
    other_before = [tuple(row) for row in db.collection_sources(second_id)]
    remove_collection_sources(db, int(first["collection_id"]), ["B"])
    assert [tuple(row) for row in db.collection_sources(second_id)] == other_before


def test_cli_remove_uses_isolated_state_and_does_not_rename_folder(tmp_path, capsys):
    config_path = tmp_path / "app" / "config.toml"
    root = tmp_path / "downloads"
    bootstrap_config(config_path)
    write_download_dir(config_path, root)
    config = load_config(config_path)
    config.storage.create_directories()
    with Database(config.storage.database, root) as database:
        _seed(database, root, ("A", "B"))
    assert main([
        "--config", str(config_path), "query", "remove", "--from", "1", "B",
    ]) == 0
    assert "Removed sources:" in capsys.readouterr().out
    with Database(config.storage.database, root, read_only=True) as database:
        assert [row["tag_query"] for row in database.collection_sources(1)] == ["A"]
        assert database.get_collection(1)["folder_name"] == "Custom Folder"
    assert (root / "Custom Folder" / "100.jpg").read_bytes() == b"image-100"


def test_collection_creation_and_add_to_regression(db):
    first = ensure_collection(db, ["one"])
    second = ensure_collection(db, ["two", "three"])
    assert add_collection_sources(db, int(first["collection_id"]), ["alias"]) == (["alias"], [])
    assert [row["tag_query"] for row in db.collection_sources(int(second["collection_id"]))] == [
        "two", "three",
    ]
