from __future__ import annotations

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from .database import Database, now_iso
from .logger import EventLogger
from .models import DownloadResult
from .network import SafeHttpClient
from .security import (
    safe_child,
    safe_library_path,
    validate_download_root,
    validate_file_extension,
    validate_file_metadata,
    validate_url,
)


class DownloadError(RuntimeError):
    pass


DEFAULT_MAX_FILE_SIZE_BYTES = 2_147_483_648


def _windows_file_information(handle: int):
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = ByHandleFileInformation()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    if not kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle), ctypes.byref(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return information


def _owned_identity(handle) -> tuple[int, int, int]:
    if os.name == "nt":
        import msvcrt

        information = _windows_file_information(msvcrt.get_osfhandle(handle.fileno()))
        return (
            int(information.dwVolumeSerialNumber),
            (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
            int(information.nNumberOfLinks),
        )
    information = os.fstat(handle.fileno())  # pragma: no cover - Windows supported
    return int(information.st_dev), int(information.st_ino), int(information.st_nlink)


def _delete_open_windows_file(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    disposition = FileDispositionInfo(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    if not kernel32.SetFileInformationByHandle(
        wintypes.HANDLE(handle), 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _remove_owned_part(part_path: Path, identity: tuple[int, int, int]) -> None:
    if os.name != "nt":  # pragma: no cover - Windows is the supported runtime
        with part_path.open("rb") as handle:
            if _owned_identity(handle) != identity:
                raise DownloadError("temporary file ownership changed; refusing cleanup")
        return

    import ctypes
    from ctypes import wintypes

    delete = 0x00010000
    read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse_point = 0x00200000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    raw_handle = kernel32.CreateFileW(
        str(part_path), delete | read_attributes, share_all, None,
        open_existing, open_reparse_point, None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _windows_file_information(raw_handle)
        attributes = int(information.dwFileAttributes)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        directory_flag = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
        current = (
            int(information.dwVolumeSerialNumber),
            (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
            int(information.nNumberOfLinks),
        )
        if attributes & (reparse_flag | directory_flag) or current[2] != 1:
            raise DownloadError("temporary file is not a private regular file")
        if current != identity:
            raise DownloadError("temporary file ownership changed; refusing cleanup")
        _delete_open_windows_file(raw_handle)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(raw_handle))


def hash_safe_file(path: Path) -> tuple[int, str]:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if path.is_symlink() or attributes & reparse_flag:
        raise DownloadError("existing final file is a symbolic link or reparse point")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DownloadError("existing final file is not a private regular file")
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise DownloadError("existing final file changed during inspection")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise DownloadError("existing final file changed during inspection")
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class _LocalResponse:
    def __init__(self, source: Path):
        self.source = source

    def iter_content(self, chunk_size: int):
        info = self.source.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if self.source.is_symlink() or attributes & reparse_flag:
            raise DownloadError("local reuse source is a symbolic link or reparse point")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadError("local reuse source is not a private regular file")
        with self.source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise DownloadError("local reuse source changed during inspection")
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise DownloadError("local reuse source changed during inspection")
            yield from iter(lambda: handle.read(chunk_size), b"")

    def close(self) -> None:
        pass


def _set_open_windows_file_name(handle: int, final_path: Path, flags: int) -> None:
    import ctypes
    from ctypes import wintypes

    name = str(final_path)

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(name) + 1)),
        ]

    information = FileRenameInfo(flags, None, len(name.encode("utf-16-le")), name)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    if not kernel32.SetFileInformationByHandle(
        wintypes.HANDLE(handle), 22, ctypes.byref(information), ctypes.sizeof(information)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _rename_open_windows_file(handle: int, final_path: Path) -> None:
    try:
        final_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"destination already exists: {final_path}")
    _set_open_windows_file_name(handle, final_path, 0)


def _replace_open_windows_file(handle: int, final_path: Path) -> None:
    _set_open_windows_file_name(handle, final_path, 1)


def _atomic_finalize(part_path: Path, final_path: Path,
                     identity: tuple[int, int, int], expected_size: int,
                     expected_md5: str,
                     corrupt_fingerprint: tuple[int, str] | None) -> None:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        delete = 0x00010000
        generic_read = 0x80000000
        read_attributes = 0x00000080
        share_read = 0x00000001
        open_existing = 3
        open_reparse_point = 0x00200000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        raw_handle = kernel32.CreateFileW(
            str(part_path), generic_read | delete | read_attributes, share_read, None,
            open_existing, open_reparse_point, None,
        )
        if raw_handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = False
        try:
            information = _windows_file_information(raw_handle)
            attributes = int(information.dwFileAttributes)
            current = (
                int(information.dwVolumeSerialNumber),
                (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
                int(information.nNumberOfLinks),
            )
            if attributes & 0x410 or current[2] != 1 or current != identity:
                raise DownloadError("temporary file ownership changed; refusing finalization")
            descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDONLY | os.O_BINARY)
            transferred = True
            with os.fdopen(descriptor, "rb") as handle:
                digest = hashlib.md5(usedforsecurity=False)
                size = 0
                for chunk in iter(lambda: handle.read(1024 * 256), b""):
                    size += len(chunk)
                    digest.update(chunk)
                actual_md5 = digest.hexdigest()
                if size != expected_size or actual_md5 != expected_md5:
                    raise DownloadError("temporary file changed after download validation")
                if (
                    corrupt_fingerprint is not None
                    and hash_safe_file(final_path) != corrupt_fingerprint
                ):
                    raise DownloadError("corrupt final file changed before replacement")
                if corrupt_fingerprint is None:
                    _rename_open_windows_file(raw_handle, final_path)
                else:
                    _replace_open_windows_file(raw_handle, final_path)
        finally:
            if not transferred:
                kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
        return
    size, actual_md5 = hash_safe_file(part_path)  # pragma: no cover
    if size != expected_size or actual_md5 != expected_md5:  # pragma: no cover
        raise DownloadError("temporary file changed after download validation")
    if corrupt_fingerprint is not None:  # pragma: no cover
        if hash_safe_file(final_path) != corrupt_fingerprint:
            raise DownloadError("corrupt final file changed before replacement")
        os.replace(part_path, final_path)
        return
    os.link(part_path, final_path)  # pragma: no cover - Windows supported
    part_path.unlink()  # pragma: no cover


def _atomic_no_replace(part_path: Path, final_path: Path,
                       identity: tuple[int, int, int], expected_size: int,
                       expected_md5: str) -> None:
    _atomic_finalize(part_path, final_path, identity, expected_size, expected_md5, None)


def _atomic_replace_corrupt(part_path: Path, final_path: Path,
                            identity: tuple[int, int, int], expected_size: int,
                            expected_md5: str,
                            corrupt_fingerprint: tuple[int, str]) -> None:
    _atomic_finalize(
        part_path, final_path, identity, expected_size, expected_md5, corrupt_fingerprint
    )


def download_post(db: Database, client: SafeHttpClient, row, target_dir: Path,
                  logger: EventLogger, attempt: int | None = None,
                  run_id: int | None = None, collection_id: int | None = None,
                  query_id: int | None = None,
                  local_source: Path | None = None,
                  max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES) -> DownloadResult:
    post_id = int(row["post_id"])
    expected_size = int(row["file_size"])
    expected_md5 = str(row["md5"]).lower()
    filename = str(row["file_name"])
    started_at = now_iso()
    received = 0
    digest = hashlib.md5(usedforsecurity=False)
    part_path: Path | None = None
    part_identity: tuple[int, int, int] | None = None
    part_created = False
    corrupt_fingerprint: tuple[int, str] | None = None
    collection_id = int(
        collection_id if collection_id is not None
        else query_id if query_id is not None
        else row["collection_id"]
    )
    attempt = attempt or db.next_attempt(collection_id, post_id)
    event_id: int | None = None
    try:
        db.set_materialization_status(collection_id, post_id, "downloading")
        event_id = db.start_download_event(
            post_id, attempt, expected_size, expected_md5, started_at=started_at,
            run_id=run_id, collection_id=collection_id,
        )
        logger.event("download_start", id=post_id, file=filename)
        validate_file_metadata(expected_size, expected_md5)
        if expected_size > max_file_size_bytes:
            raise DownloadError(
                f"文件超过安全上限: expected={expected_size} limit={max_file_size_bytes}"
            )
        extension = validate_file_extension(str(row["file_ext"]))
        if filename != f"{post_id}.{extension}":
            raise DownloadError("数据库文件名与 Post ID 或扩展名不一致")
        validate_url(row["file_url"], purpose="file")
        if db.download_root is None:
            raise DownloadError("download_dir is not configured")
        db.download_root.mkdir(parents=True, exist_ok=True)
        db.download_root = validate_download_root(db.download_root)
        if target_dir == db.download_root:
            target_dir = safe_library_path(db.download_root, str(row["folder_name"]))
        try:
            relative_dir = target_dir.relative_to(db.download_root)
        except ValueError as exc:
            raise DownloadError("query directory is outside download_dir") from exc
        target_dir = safe_library_path(db.download_root, relative_dir)
        target_dir.mkdir(parents=False, exist_ok=True)
        target_dir = safe_library_path(db.download_root, relative_dir)
        if not target_dir.is_dir():
            raise DownloadError("query destination is not a directory")
        final_path = safe_child(target_dir, filename)
        part_path = safe_child(target_dir, f".{filename}.{uuid4().hex}.part")
        if final_path.exists():
            existing_size, existing_md5 = hash_safe_file(final_path)
            if existing_size == expected_size and existing_md5 == expected_md5:
                db.set_materialization_status(
                    collection_id, post_id, "downloaded", local_path=final_path
                )
                db.finish_download_event(
                    event_id, bytes_received=existing_size, actual_md5=existing_md5,
                    result="downloaded",
                )
                logger.event("existing_file_ok", id=post_id, file=filename, md5_verified=True)
                return DownloadResult(
                    post_id, final_path, existing_size, existing_md5, "downloaded"
                )
            expected_relative = Path(relative_dir) / filename
            managed_corrupt = (
                row["status"] == "corrupt"
                and bool(row["relative_path"])
                and Path(str(row["relative_path"])) == expected_relative
            )
            if not managed_corrupt:
                raise DownloadError("目标文件已存在但校验失败，拒绝覆盖")
            corrupt_fingerprint = existing_size, existing_md5
        if local_source is not None:
            try:
                source_size, source_md5 = hash_safe_file(local_source)
                if source_size != expected_size or source_md5 != expected_md5:
                    local_source = None
            except (OSError, DownloadError):
                local_source = None
        response = (
            _LocalResponse(local_source) if local_source is not None
            else client.get(row["file_url"], purpose="file", stream=True)
        )
        try:
            with part_path.open("xb") as handle:
                part_created = True
                part_identity = _owned_identity(handle)
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        next_size = received + len(chunk)
                        if next_size > expected_size or next_size > max_file_size_bytes:
                            raise DownloadError(
                                f"响应超过允许大小: expected={expected_size} actual>{received}"
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        received = next_size
                        if received == expected_size:
                            break
        finally:
            response.close()
        actual_md5 = digest.hexdigest()
        if received != expected_size:
            raise DownloadError(f"文件大小不符: expected={expected_size} actual={received}")
        if actual_md5 != expected_md5:
            raise DownloadError(f"MD5 不符: expected={expected_md5} actual={actual_md5}")
        try:
            if corrupt_fingerprint is None:
                _atomic_no_replace(
                    part_path, final_path, part_identity, expected_size, expected_md5
                )
            else:
                _atomic_replace_corrupt(
                    part_path, final_path, part_identity, expected_size, expected_md5,
                    corrupt_fingerprint,
                )
            part_created = False
        except FileExistsError:
            existing_size, existing_md5 = hash_safe_file(final_path)
            if existing_size != expected_size or existing_md5 != expected_md5:
                raise DownloadError("a different final file appeared during download")
            _remove_owned_part(part_path, part_identity)
            part_created = False
        db.set_materialization_status(
            collection_id, post_id, "downloaded", local_path=final_path
        )
        db.finish_download_event(event_id, bytes_received=received,
                                 actual_md5=actual_md5, result="downloaded")
        logger.event("download_ok", id=post_id, file=filename, format=row["file_ext"],
                     width=row["width"], height=row["height"], size_bytes=received,
                     md5_verified=True)
        return DownloadResult(post_id, final_path, received, actual_md5, "downloaded")
    except KeyboardInterrupt:
        db.set_materialization_status(collection_id, post_id, "pending")
        cleanup_error = None
        if part_created and part_path is not None and part_identity is not None:
            try:
                _remove_owned_part(part_path, part_identity)
            except Exception as exc:  # noqa: BLE001 - preserve the interrupt
                cleanup_error = str(exc)
        if event_id is not None:
            db.finish_download_event(
                event_id, bytes_received=received,
                actual_md5=digest.hexdigest() if received else "", result="interrupted",
                error_type="KeyboardInterrupt", error_message=cleanup_error or "interrupted",
            )
        with suppress(Exception):  # Never replace KeyboardInterrupt with a logging failure.
            logger.event("download_interrupted", id=post_id, cleanup_error=cleanup_error or "")
        raise
    except Exception as exc:  # noqa: BLE001 - every failure must clean up the partial file
        actual_md5 = digest.hexdigest() if received else ""
        status = "corrupt" if isinstance(exc, DownloadError) else "failed"
        db.set_materialization_status(collection_id, post_id, status)
        cleanup_error = None
        if part_created and part_path is not None and part_identity is not None:
            try:
                _remove_owned_part(part_path, part_identity)
            except Exception as cleanup_exc:  # noqa: BLE001 - retain the original outcome
                cleanup_error = str(cleanup_exc)
        error_message = str(exc)
        if cleanup_error:
            error_message = f"{error_message}; cleanup_error={cleanup_error}"
        if event_id is not None:
            db.finish_download_event(event_id, bytes_received=received,
                                     actual_md5=actual_md5, result=status,
                                     error_type=type(exc).__name__, error_message=error_message)
        logger.event("download_failed", id=post_id, error_type=type(exc).__name__,
                     error_message=error_message)
        return DownloadResult(post_id, None, received, actual_md5, status,
                              type(exc).__name__, error_message)
