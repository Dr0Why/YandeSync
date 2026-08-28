from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

from .collection_rename import (
    journal_path,
    recover_collection_rename,
    rename_collection_folder,
)
from .compare import incremental_check
from .config import (
    ConfigError,
    assess_download_dir,
    bootstrap_config,
    discover_runtime_root,
    load_config,
    require_download_dir_acceptance,
    write_download_dir,
)
from .database import MIGRATION_ROOT_KEY, SCHEMA_VERSION, Database, now_iso, prepare_database
from .doctor import print_doctor, run_doctor
from .downloader import DownloadError, download_post, hash_safe_file
from .errors import OperationalError, UserError
from .locking import OperationLock
from .logger import EventLogger
from .network import SafeHttpClient
from .reporter import print_download_plan
from .security import (
    safe_collection_folder_name,
    safe_library_path,
    validate_artist_display_name,
    validate_artist_tag,
    validate_download_root,
    validate_tag_query,
)
from .yande_api import YandeApi

PUBLIC_COMMANDS = ("sync", "verify", "status", "config", "query")
DEFAULT_SYNC_LIMIT = 2000
DEFAULT_SYNC_CONCURRENCY = 8
MAX_SYNC_CONCURRENCY = 32


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="yande-sync", description="Reliable, portable yande.re downloader"
    )
    root.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    sub = root.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="check configured queries and recover the library")
    sync.add_argument("--query", help="process one registered query")
    sync.add_argument(
        "--limit", type=positive_int, default=DEFAULT_SYNC_LIMIT,
        help=f"maximum materialization/download work items (default: {DEFAULT_SYNC_LIMIT})",
    )
    sync.add_argument(
        "--concurrency", type=concurrency_int, default=DEFAULT_SYNC_CONCURRENCY,
        help=("maximum simultaneous image materializations "
              f"(default: {DEFAULT_SYNC_CONCURRENCY}; range: 1-{MAX_SYNC_CONCURRENCY})"),
    )
    sync.add_argument("--full-scan", action="store_true", help="scan beyond the normal stop point")

    verify = sub.add_parser("verify", help="verify files without downloading replacements")
    verify.add_argument("--query", help="verify one registered query")

    status = sub.add_parser("status", help="show configuration and library state")
    status.add_argument("--details", action="store_true")
    status.add_argument("--history", type=positive_int, metavar="N")
    status.add_argument("--doctor", action="store_true")

    config = sub.add_parser("config", help="show or change configuration")
    config_sub = config.add_subparsers(dest="config_action")
    config_get = config_sub.add_parser("get", help="get one configuration value")
    config_get.add_argument("key", choices=("download-dir",))
    config_set = config_sub.add_parser("set", help="set one configuration value")
    config_set.add_argument("key", choices=("download-dir",))
    config_set.add_argument("value", type=Path)
    config_set.add_argument(
        "--accept-missing", action="store_true",
        help="accept a location where tracked files are missing",
    )

    query = sub.add_parser("query", help="list or manage saved queries")
    query_sub = query.add_subparsers(dest="query_action")
    query_add = query_sub.add_parser(
        "add", help="add one collection containing one or more source queries"
    )
    query_add.add_argument(
        "--to", type=positive_int, metavar="COLLECTION_ID",
        help="attach source queries to an existing collection",
    )
    query_add.add_argument("tags", nargs="+")
    query_remove = query_sub.add_parser(
        "remove", help="remove source queries from a collection"
    )
    query_remove.add_argument(
        "--from", dest="collection_id", type=positive_int, required=True,
        metavar="COLLECTION_ID", help="collection to remove source queries from",
    )
    query_remove.add_argument("tags", nargs="+")
    query_rename = query_sub.add_parser("rename", help="rename a collection folder")
    query_rename.add_argument("collection", type=positive_int)
    query_rename.add_argument("new_folder_name")
    for action in ("enable", "disable"):
        command = query_sub.add_parser(action, help=f"{action} a collection")
        command.add_argument("collection")
    artist_name = query_sub.add_parser(
        "artist-name", help="manage local Japanese artist names"
    )
    artist_sub = artist_name.add_subparsers(dest="artist_name_action", required=True)
    artist_set = artist_sub.add_parser("set", help="set a local Japanese artist name")
    artist_set.add_argument("artist_tag")
    artist_set.add_argument("display_name")
    artist_unset = artist_sub.add_parser("unset", help="remove a local artist name")
    artist_unset.add_argument("artist_tag")
    artist_sub.add_parser("list", help="list local artist names")
    return root


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def concurrency_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 32") from exc
    if not 1 <= value <= MAX_SYNC_CONCURRENCY:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 32")
    return value


def validated_unique_sources(tags: list[str]) -> list[str]:
    sources = []
    seen = set()
    for tag in tags:
        validate_tag_query(tag)
        if tag not in seen:
            seen.add(tag)
            sources.append(tag)
    return sources


def ensure_collection(db: Database, tags: list[str]):
    sources = validated_unique_sources(tags)
    folder = safe_collection_folder_name(sources, existing_names=db.folder_names())
    return db.add_collection(sources, folder)


def add_collection_sources(db: Database, collection_id: int, tags: list[str]):
    sources = validated_unique_sources(tags)
    return db.add_sources_to_collection(collection_id, sources)


def remove_collection_sources(db: Database, collection_id: int, tags: list[str]):
    sources = validated_unique_sources(tags)
    return db.remove_sources_from_collection(collection_id, sources)


def prepare_collection_folder(db: Database, collection, *, reserved_names=()):
    """Return the folder candidate without persisting filesystem identity."""
    if bool(collection["folder_finalized"]):
        return collection
    sources = db.collection_sources(int(collection["collection_id"]))
    first_tag = str(sources[0]["tag_query"]).split(maxsplit=1)[0]
    try:
        mapping = db.get_artist_name(first_tag)
    except ValueError:
        mapping = None
    japanese_name = str(mapping["display_name"]) if mapping is not None else None
    folder = safe_collection_folder_name(
        [str(source["tag_query"]) for source in sources], japanese_name,
        existing_names=[
            *db.folder_names(exclude_collection_id=int(collection["collection_id"])),
            *reserved_names,
        ],
    )
    prepared = dict(collection)
    prepared["folder_name"] = folder
    return prepared


def prepare_query_folder(db: Database, query, *, reserved_names=()):
    """Compatibility wrapper for one-source collection callers."""
    return prepare_collection_folder(db, query, reserved_names=reserved_names)


def resolve_download_dir(config, db: Database, override: Path | None = None) -> Path:
    if override is not None:
        path = validate_download_root(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = config.storage.downloads
    path.mkdir(parents=True, exist_ok=True)
    return validate_download_root(path)


def reconcile_missing_downloads(db: Database, rows) -> int:
    missing = 0
    for row in rows:
        if row["status"] != "downloaded":
            continue
        try:
            present = bool(row["relative_path"]) and db.path_for(row).is_file()
        except (OSError, ValueError):
            present = False
        if present:
            continue
        db.set_materialization_status(
            row["collection_id"], row["post_id"], "missing"
        )
        missing += 1
    return missing


def verify_new_downloads(db: Database, items: list[tuple[object, Path]]) -> tuple[int, int]:
    ok = bad = 0
    for row, path in items:
        try:
            size, digest = hash_safe_file(path)
            valid = size == row["file_size"] and digest == row["md5"]
        except (OSError, DownloadError):
            valid = False
        if valid:
            ok += 1
            print(f"[VERIFY OK] {row['file_name']}")
        else:
            bad += 1
            db.set_materialization_status(
                row["collection_id"], row["post_id"],
                "corrupt" if path.exists() else "missing",
            )
            print(f"[VERIFY FAILED] {row['file_name']}")
    return ok, bad


def require_doctor(config) -> None:
    result = run_doctor(config)
    print_doctor(result)
    if not result.ok:
        raise OperationalError("environment checks failed")


def reusable_local_source(db: Database, row, expected_path: Path) -> Path | None:
    if row["relative_path"]:
        try:
            current = db.path_for(row)
            if current != expected_path:
                return current
        except (OSError, ValueError):
            pass
    for candidate in db.reusable_materializations(
        int(row["post_id"]), exclude_collection_id=int(row["collection_id"])
    ):
        try:
            path = db.path_for(candidate)
        except (OSError, ValueError):
            continue
        if path != expected_path:
            return path
    return None


def resolve_collection(db: Database, requested: str):
    if requested.isdecimal():
        collection = db.get_collection(int(requested))
        if collection is None:
            raise ValueError(f"collection is not registered: {requested}")
        return collection
    matches = db.find_source_collections(requested)
    if not matches:
        raise ValueError(f"collection is not registered: {requested}")
    if len(matches) > 1:
        raise ValueError(
            f"source expression is ambiguous: {requested}; use a collection ID"
        )
    return matches[0]


def selected_collections(db: Database, requested: str | None):
    if requested:
        return [resolve_collection(db, requested)]
    return db.list_collections(enabled_only=True)


class _WorkerDatabase:
    """Filesystem boundary used by workers; it deliberately owns no SQLite handle."""

    def __init__(self, download_root: Path):
        self.download_root = download_root

    def next_attempt(self, _collection_id: int, _post_id: int) -> int:
        raise AssertionError("worker attempts must be allocated by the database owner")

    def set_materialization_status(self, *_args, **_kwargs) -> None:
        pass

    def start_download_event(self, *_args, **_kwargs) -> int:
        return 0

    def finish_download_event(self, *_args, **_kwargs) -> None:
        pass


class _WorkerLogger:
    def event(self, _event: str, **_fields) -> None:
        pass


def materialize_downloads(config, db: Database, rows, target: Path, logger: EventLogger,
                          *, run_id: int, concurrency: int):
    """Materialize a bounded plan while keeping all SQLite and reporting on this thread."""
    clients: list[SafeHttpClient] = []
    clients_lock = threading.Lock()
    worker_state = threading.local()
    worker_logger = _WorkerLogger()

    def initialize_worker() -> None:
        client = SafeHttpClient(config.network)
        worker_state.client = client
        with clients_lock:
            clients.append(client)

    def transfer(work):
        row, attempt, local_source = work
        target_dir = safe_library_path(target, row["folder_name"])
        return download_post(
            _WorkerDatabase(target), worker_state.client, row, target_dir, worker_logger,
            attempt=attempt, run_id=run_id, collection_id=int(row["collection_id"]),
            local_source=local_source,
            max_file_size_bytes=config.download.max_file_size_bytes,
        )

    def begin(row):
        collection_id = int(row["collection_id"])
        post_id = int(row["post_id"])
        attempt = db.next_attempt(collection_id, post_id)
        db.set_materialization_status(collection_id, post_id, "downloading")
        event_id = db.start_download_event(
            post_id, attempt, int(row["file_size"]), str(row["md5"]),
            started_at=now_iso(), run_id=run_id, collection_id=collection_id,
        )
        expected_path = db.expected_path_for(row)
        local_source = reusable_local_source(db, row, expected_path)
        logger.event("download_start", id=post_id, file=row["file_name"])
        print(f"[DOWNLOAD] {row['file_name']}")
        return (row, attempt, local_source), event_id

    downloaded = []
    failures = 0
    row_iter = iter(rows)
    pending: dict[Future, tuple[object, int]] = {}
    executor = ThreadPoolExecutor(max_workers=concurrency, initializer=initialize_worker)
    interrupted = False
    try:
        for row in row_iter:
            work, event_id = begin(row)
            pending[executor.submit(transfer, work)] = (row, event_id)
            if len(pending) >= concurrency:
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                row, event_id = pending.pop(future)
                result = future.result()
                collection_id = int(row["collection_id"])
                if result.result == "downloaded":
                    db.set_materialization_status(
                        collection_id, result.post_id, "downloaded",
                        local_path=result.local_path,
                    )
                    downloaded.append((row, result.local_path))
                    logger.event(
                        "download_ok", id=result.post_id, file=row["file_name"],
                        size_bytes=result.bytes_received, md5_verified=True,
                    )
                    print(f"[SAVED] {result.local_path}")
                else:
                    failures += 1
                    db.set_materialization_status(collection_id, result.post_id, result.result)
                    logger.event(
                        "download_failed", id=result.post_id,
                        error_type=result.error_type, error_message=result.error_message,
                    )
                    print(
                        f"[FAILED] ID={result.post_id} "
                        f"{result.error_type}: {result.error_message}"
                    )
                db.finish_download_event(
                    event_id, bytes_received=result.bytes_received,
                    actual_md5=result.actual_md5, result=result.result,
                    error_type=result.error_type, error_message=result.error_message,
                )
                try:
                    next_row = next(row_iter)
                except StopIteration:
                    continue
                work, next_event_id = begin(next_row)
                pending[executor.submit(transfer, work)] = (next_row, next_event_id)
    except KeyboardInterrupt:
        interrupted = True
        for future, (row, event_id) in pending.items():
            if future.cancel():
                db.set_materialization_status(
                    int(row["collection_id"]), int(row["post_id"]), "pending"
                )
                db.finish_download_event(
                    event_id, result="interrupted", error_type="KeyboardInterrupt",
                    error_message="interrupted before transfer started",
                )
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=interrupted)
        for client in clients:
            close = getattr(client, "close", None)
            if close is not None:
                close()
    return downloaded, failures


def do_sync(config, db: Database, requested: str | None, limit: int | None,
            *, full_scan: bool = False,
            concurrency: int = DEFAULT_SYNC_CONCURRENCY) -> tuple[int, int]:
    collections = selected_collections(db, requested)
    if not collections:
        raise ValueError("no enabled collections; run 'yande-sync query add TAG'")
    discovery_limit = config.sync.initial_limit
    collection_ids = [int(item["collection_id"]) for item in collections]
    ok = failed = 0
    pages = received = new_count = 0
    with db.run("sync", requested) as run, EventLogger(config.storage.logs) as logger:
        require_doctor(config)
        with SafeHttpClient(config.network) as client:
            api = YandeApi(client, config.sync.page_size)
            for collection in collections:
                for source in db.collection_sources(int(collection["collection_id"])):
                    try:
                        result = incremental_check(
                            db, api, source, limit=discovery_limit,
                            known_stop_count=(
                                discovery_limit + 1
                                if full_scan else config.sync.known_stop_count
                            ),
                        )
                        pages += result.pages_requested
                        received += len(result.received)
                        new_count += len(result.new_posts)
                        print(
                            f"[CHECK] {source['tag_query']}: received={len(result.received)} "
                            f"new={len(result.new_posts)}"
                        )
                    except (OperationalError, ValueError) as exc:
                        failed += 1
                        logger.event(
                            "source_failed", source=source["tag_query"],
                            collection_id=collection["collection_id"],
                            error_type=type(exc).__name__, error_message=str(exc),
                        )
                        print(f"[SOURCE FAILED] {source['tag_query']}: {exc}")

            recorded = db.posts_for_collections(collection_ids)
            reconcile_missing_downloads(db, recorded)
            rows = db.posts_to_download_for_collections(collection_ids, limit)
            collections_by_id = {
                int(item["collection_id"]): item for item in collections
            }
            prepared_collections = {}
            reserved_names = []
            prepared_rows = []
            for row in rows:
                collection_id = int(row["collection_id"])
                if collection_id not in prepared_collections:
                    prepared = prepare_collection_folder(
                        db, collections_by_id[collection_id], reserved_names=reserved_names
                    )
                    prepared_collections[collection_id] = prepared
                    reserved_names.append(str(prepared["folder_name"]))
                prepared_row = dict(row)
                prepared_row["folder_name"] = prepared_collections[collection_id]["folder_name"]
                prepared_rows.append(prepared_row)
            rows = prepared_rows
            print_download_plan(db.posts_for_collections(collection_ids), rows)
            target = resolve_download_dir(config, db)
            downloaded, materialize_failed = materialize_downloads(
                config, db, rows, target, logger,
                run_id=run.run_id, concurrency=concurrency,
            )
            ok += len(downloaded)
            failed += materialize_failed
        verified, verify_failed = verify_new_downloads(db, downloaded)
        failed += verify_failed
        run.update(
            pages_requested=pages, posts_received=received, new_count=new_count,
            downloaded_count=ok, failed_count=failed,
            result="ok" if failed == 0 else "partial",
        )
    print(f"Sync complete: downloaded={ok} failed={failed} verified={verified}")
    return ok, failed


def do_verify(config, db: Database, requested: str | None) -> tuple[int, int]:
    collections = selected_collections(db, requested)
    if not collections:
        raise ValueError("no enabled collections")
    rows = db.posts_for_collections(
        [int(item["collection_id"]) for item in collections]
    )
    verifiable_statuses = {"downloaded", "missing", "corrupt"}
    verification_total = sum(row["status"] in verifiable_statuses for row in rows)
    print(f"Verifying {verification_total} files...")
    verified_count = 0
    ok = bad = 0
    with db.run("verify", requested) as run:
        for row in rows:
            if row["status"] not in verifiable_statuses:
                continue
            verified_count += 1
            path = db.path_for(row)
            try:
                size, digest = hash_safe_file(path)
            except (OSError, DownloadError):
                size, digest = -1, ""
            if not path.is_file():
                db.set_materialization_status(
                    row["collection_id"], row["post_id"], "missing"
                )
                bad += 1
                print(f"[MISSING] {row['file_name']}")
            elif size != row["file_size"] or digest != row["md5"]:
                db.set_materialization_status(
                    row["collection_id"], row["post_id"], "corrupt"
                )
                bad += 1
                print(f"[CORRUPT] {row['file_name']}")
            else:
                if row["status"] != "downloaded":
                    db.set_materialization_status(
                        row["collection_id"], row["post_id"], "downloaded", local_path=path
                    )
                ok += 1
            if verified_count % 100 == 0:
                print(f"Verifying {verified_count}/{verification_total}...")
        parts = []
        for collection in collections:
            folder = safe_library_path(config.storage.downloads, collection["folder_name"])
            if folder.is_dir():
                parts.extend(folder.glob("*.part"))
        for path in parts:
            print(f"[PART] {path}")
        bad += len(parts)
        run.update(failed_count=bad, result="ok" if bad == 0 else "partial")
    print(f"Verify complete: ok={ok} problems={bad}")
    if bad:
        print("Run 'yande-sync sync' to repair missing or corrupt files.")
    return ok, bad


@contextmanager
def open_writable_database(config):
    prepared = prepare_database(config.storage)
    download_root = prepared.download_root
    with Database(
        config.storage.database, download_root, backup_dir=config.storage.backups
    ) as database:
        integrity = database.connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise OperationalError("database integrity check failed after migration")
        pending_root = database.get_setting(MIGRATION_ROOT_KEY)
        if pending_root:
            download_root = validate_download_root(Path(pending_root))
            if download_root != config.storage.download_dir:
                write_download_dir(config.source_path, download_root)
                config = config.with_download_dir(download_root)
            database.delete_setting(MIGRATION_ROOT_KEY)
        yield config, database


def print_config(config, *, key: str | None = None) -> None:
    value = str(config.storage.download_dir) if config.storage.download_dir else "not configured"
    if key == "download-dir":
        print(value)
        return
    print(f"Runtime mode: {config.runtime_mode}")
    print(f"Runtime root: {config.storage.root}")
    print(f"Download directory: {value}")
    print(f"Database: {config.storage.database}")
    print(f"Logs: {config.storage.logs}")


def print_collections(database: Database | None) -> None:
    if database is not None and database.schema_version() != SCHEMA_VERSION:
        print(f"Database schema: {database.schema_version()} (migration required)")
        return
    rows = database.list_collections() if database is not None else []
    if not rows:
        print("No configured collections.")
        return
    for row in rows:
        state = "enabled" if row["enabled"] else "disabled"
        sources = ", ".join(
            str(source["tag_query"])
            for source in database.collection_sources(int(row["collection_id"]))
        )
        print(
            f"[{row['collection_id']}] {row['folder_name']} [{state}] "
            f"Sources: {sources}"
        )


def print_artist_names(database: Database) -> None:
    rows = database.list_artist_names()
    if not rows:
        print("No artist names configured.")
        return
    for row in rows:
        print(f"{row['artist_tag']} -> {row['display_name']}")


def print_status(config, database: Database | None, *, details=False, history=None) -> None:
    print(f"Download directory: {config.storage.download_dir or 'not configured'}")
    if database is None:
        print("Database: not initialized")
        return
    version = database.schema_version()
    if version != SCHEMA_VERSION:
        print(f"Database schema: {version} (migration required)")
        print("Run a mutating command after backing up the portable runtime to migrate it.")
        return
    counts = database.status_counts()
    print(f"Known remote posts: {counts.get('total', 0)}")
    print(f"Collections: {counts.get('collections', 0)}")
    print(f"Enabled collections: {counts.get('enabled_collections', 0)}")
    print(f"Sources: {counts.get('sources', 0)}")
    print(f"Materializations: {counts.get('materializations', 0)}")
    for status in ("downloaded", "pending", "new", "failed", "missing", "corrupt", "downloading"):
        print(f"{status.capitalize()}: {counts.get(status, 0)}")
    print("Collections:")
    print_collections(database)
    for command, label in (("sync", "Last sync"), ("verify", "Last verify")):
        row = database.last_run(command)
        print(f"{label}: {row['finished_at'] if row else 'never'}")
    if details:
        print(f"Database: {config.storage.database}")
        print(f"Logs: {config.storage.logs}")
    if history:
        for row in database.recent_events(history):
            print(
                f"{row['finished_at'] or '-'} [{row['collection_id'] or '-'}] "
                f"{row['file_name']} {row['result']}"
            )


def read_only_database(config) -> Database | None:
    candidate = config.storage.database
    if not candidate.is_file() and config.storage.legacy_database is not None:
        candidate = config.storage.legacy_database
    if not candidate.is_file():
        return None
    return Database(candidate, config.storage.download_dir, read_only=True)


def _config_path(args) -> Path:
    if args.config is not None:
        return args.config.resolve()
    runtime_root, _mode = discover_runtime_root()
    return runtime_root / "config.toml"


def configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        if hasattr(stream, "isatty") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            stream.reconfigure(errors="replace")


def _report_assessment(assessment) -> None:
    print(f"Tracked files: {assessment.tracked}")
    print(f"Found files: {assessment.found}")
    print(f"Missing files: {assessment.missing}")


def _record_exception_safely(config, exc: BaseException) -> bool:
    try:
        with EventLogger(config.storage.logs) as logger:
            logger.record_exception(str(exc))
        return True
    except Exception:  # noqa: BLE001 - diagnostics must not replace the original failure
        return False


def main(argv: list[str] | None = None) -> int:
    configure_streams()
    args = parser().parse_args(argv)
    config = None
    operation_lock = None
    mutation_lock_acquired = False
    try:
        config_path = _config_path(args)
        is_config_set = args.command == "config" and args.config_action == "set"
        proposed = validate_download_root(args.value) if is_config_set else None

        if args.command == "config" and not is_config_set:
            config = load_config(config_path)
            print_config(config, key=args.key if args.config_action == "get" else None)
            return 0
        if args.command in {"status", "query"} and getattr(args, "query_action", None) is None:
            config = load_config(config_path)
            database = read_only_database(config)
            doctor_ok = True
            try:
                if args.command == "query":
                    print_collections(database)
                else:
                    print_status(
                        config, database, details=args.details, history=args.history
                    )
                    if args.doctor:
                        result = run_doctor(config)
                        print_doctor(result)
                        doctor_ok = result.ok
            finally:
                if database is not None:
                    database.close()
            return 0 if doctor_ok else 1

        lock_path = config_path.parent / "operation.lock"
        operation_lock = OperationLock(lock_path)
        operation_lock.__enter__()
        mutation_lock_acquired = True

        if is_config_set:
            if not config_path.exists():
                existing_database = config_path.parent / "data" / "yande-sync.db"
                if existing_database.exists():
                    raise ConfigError(
                        "configuration is missing beside an existing database; restore config.toml"
                    )
                assessment = type("Assessment", (), {"tracked": 0, "found": 0, "missing": 0})()
                _report_assessment(assessment)
                require_download_dir_acceptance(
                    assessment, accept_missing=args.accept_missing,
                    interactive=sys.stdin.isatty(),
                )
                bootstrap_config(config_path)
                config = load_config(config_path)
            else:
                config = load_config(config_path)
                database = read_only_database(config)
                try:
                    if database is None:
                        assessment = type(
                            "Assessment", (), {"tracked": 0, "found": 0, "missing": 0}
                        )()
                        old_schema = False
                    else:
                        assessment = assess_download_dir(database, proposed)
                        old_schema = database.schema_version() != SCHEMA_VERSION
                finally:
                    if database is not None:
                        database.close()
                _report_assessment(assessment)
                require_download_dir_acceptance(
                    assessment, accept_missing=args.accept_missing,
                    interactive=sys.stdin.isatty(),
                )
                if old_schema:
                    config.storage.create_directories()
                    with open_writable_database(config) as runtime:
                        config, _database = runtime
            operation_lock.materialize()
            proposed.mkdir(parents=True, exist_ok=True)
            write_download_dir(config.source_path, proposed)
            print(f"Download directory changed to: {proposed}")
            return 0

        config = load_config(config_path)
        operation_lock.materialize()
        config.storage.create_directories()
        with open_writable_database(config) as runtime:
            config, database = runtime
            rename_journal = journal_path(config.storage.root)
            if rename_journal.exists():
                recover_collection_rename(database, config.storage.downloads, rename_journal)
            if args.command == "sync":
                database.recover_abandoned_downloads()
                _ok, failed = do_sync(
                    config, database, args.query, args.limit, full_scan=args.full_scan,
                    concurrency=args.concurrency,
                )
                return 0 if failed == 0 else 1
            if args.command == "verify":
                _ok, bad = do_verify(config, database, args.query)
                return 0 if bad == 0 else 1
            if args.command == "query":
                if args.query_action == "add":
                    if args.to is None:
                        collection = ensure_collection(database, args.tags)
                        print(
                            f"Added collection [{collection['collection_id']}]: "
                            f"{database.collection_summary(int(collection['collection_id']))}"
                        )
                    else:
                        added, present = add_collection_sources(database, args.to, args.tags)
                        print(f"Updated collection [{args.to}].")
                        if added:
                            print("Added sources:")
                            for source in added:
                                print(f"  {source}")
                        if present:
                            print("Already present:")
                            for source in present:
                                print(f"  {source}")
                elif args.query_action == "remove":
                    removed = remove_collection_sources(
                        database, args.collection_id, args.tags
                    )
                    print(f"Updated collection [{args.collection_id}].")
                    print("Removed sources:")
                    for source in removed:
                        print(f"  {source}")
                elif args.query_action == "rename":
                    old_name, new_name = rename_collection_folder(
                        database, config.storage.downloads, rename_journal,
                        args.collection, args.new_folder_name,
                    )
                    print(f"Renamed collection {args.collection}:")
                    print(f"  {old_name}")
                    print(f"  -> {new_name}")
                elif args.query_action == "artist-name":
                    if args.artist_name_action == "set":
                        artist_tag = validate_artist_tag(args.artist_tag)
                        display_name = validate_artist_display_name(args.display_name)
                        database.set_artist_name(artist_tag, display_name)
                        print(f"Set artist name: {artist_tag} -> {display_name}")
                    elif args.artist_name_action == "unset":
                        artist_tag = validate_artist_tag(args.artist_tag)
                        removed = database.unset_artist_name(artist_tag)
                        message = (
                            f"Removed artist name: {artist_tag}" if removed
                            else f"No artist name configured: {artist_tag}"
                        )
                        print(message)
                    else:
                        print_artist_names(database)
                else:
                    collection = resolve_collection(database, args.collection)
                    if not database.set_collection_enabled(
                        int(collection["collection_id"]), args.query_action == "enable"
                    ):
                        raise ValueError(
                            f"collection is not registered: {args.collection}"
                        )
                    print(
                        f"{args.query_action.capitalize()}d collection: "
                        f"{collection['collection_id']}"
                    )
                return 0
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (ConfigError, UserError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (OperationalError, OSError, sqlite3.Error) as exc:
        if config is not None and mutation_lock_acquired:
            _record_exception_safely(config, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - final CLI boundary logs unexpected failures
        logged = False
        if config is not None and mutation_lock_acquired:
            logged = _record_exception_safely(config, exc)
        if logged:
            print(
                f"Unexpected internal error. See {config.storage.logs / 'activity.log'}",
                file=sys.stderr,
            )
        else:
            print("Unexpected internal error.", file=sys.stderr)
        return 1
    finally:
        if operation_lock is not None:
            operation_lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
