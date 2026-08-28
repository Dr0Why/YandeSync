from __future__ import annotations

import json
import ntpath
import re
import stat
import unicodedata
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"yande.re", "files.yande.re"})
API_HOST = "yande.re"
FILE_HOST = "files.yande.re"
UNSAFE_FOLDER_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
SAFE_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,8}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
JAPANESE_SCRIPT_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
ALLOWED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif"})
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)


class SecurityError(ValueError):
    pass


def validate_url(url: str, *, purpose: str | None = None) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SecurityError("URL 格式无效") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise SecurityError("仅允许 HTTPS URL")
    if parsed.username or parsed.password:
        raise SecurityError("URL 不得包含凭据")
    if port not in (None, 443):
        raise SecurityError("仅允许 HTTPS 默认端口 443")
    if host not in ALLOWED_HOSTS:
        raise SecurityError(f"域名不在白名单中: {host or '<empty>'}")
    if purpose not in {None, "api", "file"}:
        raise SecurityError(f"unsupported URL purpose: {purpose}")
    if purpose == "api" and (host != API_HOST or parsed.path != "/post.json"):
        raise SecurityError("元数据查询仅允许 https://yande.re/post.json")
    if purpose == "file" and host != FILE_HOST:
        raise SecurityError("文件下载仅允许 files.yande.re")
    return url


def folder_collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold().rstrip(" .")


def safe_folder_name(tag_query: str, japanese_name: str | None = None, *,
                     existing_names=()) -> str:
    """Build one stable, readable and collision-safe Windows directory component."""
    validate_tag_query(tag_query)
    original = tag_query.strip()
    preferred = f"{japanese_name.strip()} {original}" if japanese_name else original
    normalized = unicodedata.normalize("NFC", preferred)
    rendered = UNSAFE_FOLDER_RE.sub("_", normalized).rstrip(" .")
    changed = rendered != preferred or original != tag_query
    if not rendered or rendered in {".", ".."} or ntpath.isreserved(rendered):
        rendered = f"query_{rendered.strip(' ._') or 'unnamed'}"
        changed = True

    existing_keys = {folder_collision_key(str(item)) for item in existing_names}
    digest = sha256(tag_query.encode("utf-8")).hexdigest()
    suffix = f"--{digest[:12]}"
    if len(rendered) > 120:
        rendered = rendered[: 120 - len(suffix)].rstrip(" .")
        changed = True
    if changed or folder_collision_key(rendered) in existing_keys:
        base = rendered[: 120 - len(suffix)].rstrip(" .") or "query"
        rendered = base + suffix
    if folder_collision_key(rendered) in existing_keys:
        for width in range(16, 65, 4):
            suffix = f"--{digest[:width]}"
            base = rendered.split("--", 1)[0][: 120 - len(suffix)].rstrip(" .") or "query"
            candidate = base + suffix
            if folder_collision_key(candidate) not in existing_keys:
                rendered = candidate
                break
        else:
            raise SecurityError("unable to build a unique query directory name")
    relative = validate_relative_path(rendered)
    if len(relative.parts) != 1:
        raise SecurityError("query directory must be one path component")
    return rendered


def canonical_source_signature(sources: list[str] | tuple[str, ...]) -> str:
    """Return an unambiguous, order-sensitive identity for a source list."""
    if not sources:
        raise SecurityError("a collection requires at least one source query")
    for source in sources:
        validate_tag_query(source)
    encoded = json.dumps(list(sources), ensure_ascii=False, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def safe_collection_folder_name(sources: list[str] | tuple[str, ...],
                                japanese_name: str | None = None, *,
                                existing_names=()) -> str:
    """Build a readable Windows-safe folder from an ordered collection source list."""
    signature = canonical_source_signature(sources)
    display = " + ".join(source.strip() for source in sources)
    preferred = f"{japanese_name.strip()} {display}" if japanese_name else display
    normalized = unicodedata.normalize("NFC", preferred)
    rendered = UNSAFE_FOLDER_RE.sub("_", normalized).rstrip(" .")
    changed = rendered != preferred or any(source != source.strip() for source in sources)
    if not rendered or rendered in {".", ".."} or ntpath.isreserved(rendered):
        rendered = f"collection_{rendered.strip(' ._') or 'unnamed'}"
        changed = True

    existing_keys = {folder_collision_key(str(item)) for item in existing_names}
    suffix = f"--{signature[:12]}"
    if len(rendered) > 120:
        rendered = rendered[: 120 - len(suffix)].rstrip(" .")
        changed = True
    if changed or folder_collision_key(rendered) in existing_keys:
        base = rendered[: 120 - len(suffix)].rstrip(" .") or "collection"
        rendered = base + suffix
    if folder_collision_key(rendered) in existing_keys:
        for width in range(16, 65, 4):
            suffix = f"--{signature[:width]}"
            base = rendered.split("--", 1)[0][: 120 - len(suffix)].rstrip(" .")
            candidate = (base or "collection") + suffix
            if folder_collision_key(candidate) not in existing_keys:
                rendered = candidate
                break
        else:
            raise SecurityError("unable to build a unique collection directory name")
    relative = validate_relative_path(rendered)
    if len(relative.parts) != 1:
        raise SecurityError("collection directory must be one path component")
    return rendered


def validate_tag_query(tag_query: str) -> str:
    if not tag_query.strip() or len(tag_query) > 500:
        raise SecurityError("TAG 不能为空且长度不得超过 500 个字符")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in tag_query):
        raise SecurityError("TAG 不得包含控制字符")
    return tag_query


def validate_artist_tag(artist_tag: str) -> str:
    validate_tag_query(artist_tag)
    if artist_tag != artist_tag.strip() or len(artist_tag.split()) != 1:
        raise SecurityError("artist tag must be exactly one query token")
    if artist_tag in {".", ".."} or any(char in artist_tag for char in "/\\"):
        raise SecurityError("artist tag is not a safe mapping key")
    return artist_tag


def validate_artist_display_name(display_name: str) -> str:
    value = display_name.strip()
    if not value or len(value) > 200:
        raise SecurityError("artist display name must contain 1 to 200 characters")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise SecurityError("artist display name must not contain control characters")
    if not JAPANESE_SCRIPT_RE.search(value):
        raise SecurityError("artist display name must contain Japanese script")
    return value


def validate_collection_folder_name(folder_name: str) -> str:
    """Validate an explicit Windows folder component without rewriting it."""
    if not isinstance(folder_name, str) or not folder_name.strip():
        raise SecurityError("collection folder name must not be empty")
    if len(folder_name) > 255:
        raise SecurityError("collection folder name is too long")
    if UNSAFE_FOLDER_RE.search(folder_name):
        raise SecurityError("collection folder name contains invalid Windows characters")
    if folder_name in {".", ".."} or folder_name.endswith((" ", ".")):
        raise SecurityError("collection folder name has an invalid trailing dot or space")
    if ntpath.isabs(folder_name) or ntpath.splitdrive(folder_name)[0]:
        raise SecurityError("collection folder name must be one basename")
    if ntpath.isreserved(folder_name):
        raise SecurityError("collection folder name is a reserved Windows device name")
    relative = validate_relative_path(folder_name)
    if len(relative.parts) != 1:
        raise SecurityError("collection folder name must be one basename")
    return folder_name


def validate_file_extension(extension: str) -> str:
    normalized = extension.lower()
    if not SAFE_EXTENSION_RE.fullmatch(normalized) or normalized not in ALLOWED_EXTENSIONS:
        raise SecurityError(f"不支持或不安全的文件扩展名: {extension!r}")
    return normalized


def validate_file_metadata(file_size: int, md5: str) -> None:
    if file_size <= 0:
        raise SecurityError("远端文件大小必须为正整数")
    if not MD5_RE.fullmatch(md5.lower()):
        raise SecurityError("远端 MD5 格式无效")


def safe_child(root: Path, name: str) -> Path:
    resolved_root = validate_download_root(root)
    relative = validate_relative_path(name)
    if len(relative.parts) != 1:
        raise SecurityError("路径越界")
    child = resolved_root / relative
    _reject_linked_descendants(resolved_root, relative)
    return child


def validate_download_root(path: Path) -> Path:
    if not path.is_absolute():
        raise SecurityError("download_dir must be an absolute path")
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.exists():
        for component in (candidate, *candidate.parents):
            try:
                info = component.lstat()
            except OSError as exc:
                raise SecurityError(f"cannot inspect download_dir: {component}") from exc
            attributes = getattr(info, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if component.is_symlink() or attributes & reparse_flag:
                raise SecurityError("download_dir must not contain links or reparse points")
    return path.resolve()


def validate_relative_path(value: str | Path) -> Path:
    relative = Path(value)
    if relative.is_absolute() or relative.drive or relative.anchor or not relative.parts:
        raise SecurityError("stored file path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise SecurityError("stored file path escapes download_dir")
    for part in relative.parts:
        if "\x00" in part or ":" in part or part.endswith((" ", ".")):
            raise SecurityError("stored file path is unsafe on Windows")
        if ntpath.isreserved(part):
            raise SecurityError("stored file path uses a reserved Windows device name")
    return relative


def safe_library_path(root: Path, relative_path: str | Path) -> Path:
    resolved_root = validate_download_root(root)
    relative = validate_relative_path(relative_path)
    _reject_linked_descendants(resolved_root, relative)
    return resolved_root / relative


def _reject_linked_descendants(root: Path, relative: Path) -> None:
    current = root
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SecurityError(f"cannot inspect library path: {current}") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise SecurityError("library path must not contain links or reparse points")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise SecurityError("stored file path has a non-directory parent")
