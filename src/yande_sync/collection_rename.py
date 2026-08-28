from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .database import Database
from .errors import OperationalError
from .security import (
    SecurityError,
    folder_collision_key,
    safe_library_path,
    validate_collection_folder_name,
    validate_download_root,
    validate_relative_path,
)

OPERATION = "collection_folder_rename"
JOURNAL_NAME = "collection-folder-rename.json"


class CollectionRenameError(OperationalError):
    pass


def journal_path(runtime_root: Path) -> Path:
    return runtime_root / "data" / JOURNAL_NAME


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_journal(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _load_journal(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectionRenameError("rename interrupted and recovery journal is invalid") from exc
    if not isinstance(payload, dict) or payload.get("operation") != OPERATION:
        raise CollectionRenameError("rename interrupted and recovery journal is invalid")
    return payload


def _directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CollectionRenameError(f"{label} folder is missing: {path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or attributes & reparse:
        raise CollectionRenameError(f"{label} folder is a symlink or reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise CollectionRenameError(f"{label} folder is not a directory: {path}")


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _paths(db: Database, collection_id: int) -> list[str]:
    rows = db.connection.execute(
        "SELECT relative_path FROM collection_posts WHERE collection_id=? "
        "AND relative_path IS NOT NULL ORDER BY post_id",
        (collection_id,),
    )
    return [str(row[0]) for row in rows]


def _split_path(value: str, expected_folder: str) -> str:
    relative = validate_relative_path(value)
    if not relative.parts or relative.parts[0] != expected_folder:
        raise CollectionRenameError("database/path state inconsistent with the collection folder")
    # Stored paths are native Path strings. Rebuilding only replaces component one.
    return str(Path(*relative.parts[1:])) if len(relative.parts) > 1 else ""


def _paths_match(db: Database, collection_id: int, folder: str) -> bool:
    try:
        return all(_split_path(value, folder) or True for value in _paths(db, collection_id))
    except (SecurityError, CollectionRenameError):
        return False


def _safe_endpoints(root: Path, old_name: str, new_name: str) -> tuple[Path, Path]:
    resolved = validate_download_root(root)
    old = safe_library_path(resolved, validate_collection_folder_name(old_name))
    new = safe_library_path(resolved, validate_collection_folder_name(new_name))
    if old.parent != resolved or new.parent != resolved:
        raise CollectionRenameError("collection folder must be directly under download_dir")
    return old, new


def _commit_database_rename(
    db: Database, collection_id: int, new_name: str, values: list[str], rewritten: list[str]
) -> None:
    try:
        db.connection.execute("BEGIN IMMEDIATE")
        db.connection.execute(
            "UPDATE collections SET folder_name=?,folder_finalized=1 WHERE collection_id=?",
            (new_name, collection_id),
        )
        for original, replacement in zip(values, rewritten, strict=True):
            cursor = db.connection.execute(
                "UPDATE collection_posts SET relative_path=? "
                "WHERE collection_id=? AND relative_path=?",
                (replacement, collection_id, original),
            )
            if cursor.rowcount != 1:
                raise CollectionRenameError("database/path state changed during rename")
        db.connection.commit()
    except Exception:
        db.connection.rollback()
        raise


def recover_collection_rename(db: Database, root: Path, path: Path) -> None:
    payload = _load_journal(path)
    if payload is None:
        return
    try:
        collection_id = int(payload["collection_id"])
        old_name = str(payload["old_folder_name"])
        new_name = str(payload["new_folder_name"])
        phase = str(payload["phase"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectionRenameError("rename interrupted and recovery journal is invalid") from exc
    if phase not in {"prepared", "filesystem_renamed", "database_committed"}:
        raise CollectionRenameError("rename interrupted and recovery journal phase is invalid")
    collection = db.get_collection(collection_id)
    if collection is None:
        raise CollectionRenameError("rename recovery collection no longer exists")
    old, new = _safe_endpoints(root, old_name, new_name)
    old_exists, new_exists = _exists(old), _exists(new)
    db_name = str(collection["folder_name"])
    old_paths = _paths_match(db, collection_id, old_name)
    new_paths = _paths_match(db, collection_id, new_name)
    if db_name == old_name and old_exists and not new_exists and old_paths:
        _directory(old, "old collection")
        _remove_journal(path)
        return
    if db_name == old_name and not old_exists and new_exists and old_paths:
        _directory(new, "renamed collection")
        os.replace(new, old)
        _directory(old, "old collection")
        _remove_journal(path)
        return
    if db_name == new_name and not old_exists and new_exists and new_paths:
        _directory(new, "renamed collection")
        _remove_journal(path)
        return
    raise CollectionRenameError(
        "rename interrupted and recovery required; filesystem/database state is ambiguous"
    )


def rename_collection_folder(
    db: Database, root: Path, path: Path, collection_id: int, new_name: str
) -> tuple[str, str]:
    collection = db.get_collection(collection_id)
    if collection is None:
        raise ValueError(f"collection is not registered: {collection_id}")
    old_name = validate_collection_folder_name(str(collection["folder_name"]))
    new_name = validate_collection_folder_name(new_name)
    if folder_collision_key(old_name) == folder_collision_key(new_name):
        raise ValueError("case-only collection folder rename is unsupported")
    if any(
        folder_collision_key(name) == folder_collision_key(new_name)
        for name in db.folder_names(exclude_collection_id=collection_id)
    ):
        raise ValueError("destination conflicts with another collection folder")
    old, new = _safe_endpoints(root, old_name, new_name)
    _directory(old, "old collection")
    if _exists(new):
        raise ValueError(f"destination already exists: {new}")
    values = _paths(db, collection_id)
    tails = [_split_path(value, old_name) for value in values]
    rewritten = [str(Path(new_name) / tail) if tail else new_name for tail in tails]
    for value in rewritten:
        safe_library_path(root, value)

    payload: dict[str, object] = {
        "operation": OPERATION,
        "collection_id": collection_id,
        "old_folder_name": old_name,
        "new_folder_name": new_name,
        "phase": "prepared",
    }
    _atomic_json(path, payload)
    os.replace(old, new)
    payload["phase"] = "filesystem_renamed"
    _atomic_json(path, payload)
    try:
        _commit_database_rename(db, collection_id, new_name, values, rewritten)
    except Exception:
        try:
            os.replace(new, old)
        except OSError as rollback_exc:
            raise CollectionRenameError(
                "filesystem rollback failed; consistency may require recovery"
            ) from rollback_exc
        _remove_journal(path)
        raise
    payload["phase"] = "database_committed"
    _atomic_json(path, payload)
    current = db.get_collection(collection_id)
    if (
        current is None
        or str(current["folder_name"]) != new_name
        or _exists(old)
        or not _exists(new)
        or not _paths_match(db, collection_id, new_name)
    ):
        raise CollectionRenameError("rename postcondition verification failed")
    _directory(new, "renamed collection")
    _remove_journal(path)
    return old_name, new_name
