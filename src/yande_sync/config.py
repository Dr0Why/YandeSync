from __future__ import annotations

import os
import re
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .security import safe_library_path, validate_download_root


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    proxy: str | None
    control_url: str
    require_proxy: bool
    allow_direct: bool
    timeout_seconds: float
    max_retries: int
    max_redirects: int


@dataclass(frozen=True, slots=True)
class StorageConfig:
    root: Path
    download_dir: Path | None = None
    legacy_root: Path | None = None

    @property
    def database(self) -> Path:
        return self.root / "data" / "yande-sync.db"

    @property
    def legacy_database(self) -> Path | None:
        if self.legacy_root is None:
            return None
        return self.legacy_root / "data" / "state.db"

    @property
    def downloads(self) -> Path:
        if self.download_dir is None:
            raise ConfigError(
                "download_dir is not configured; run config set download-dir ABSOLUTE_PATH"
            )
        return self.download_dir

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def backups(self) -> Path:
        return self.root / "data" / "backups"

    @property
    def operation_lock(self) -> Path:
        return self.root / "operation.lock"

    def create_directories(self) -> None:
        for path in (self.database.parent, self.logs, self.temp):
            path.mkdir(parents=True, exist_ok=True)

    def check_access(self) -> None:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.is_dir() or not os.access(probe, os.W_OK):
            raise OSError(f"runtime root is not writable: {self.root}")


@dataclass(frozen=True, slots=True)
class SyncConfig:
    page_size: int
    initial_limit: int
    known_stop_count: int


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    max_file_size_bytes: int


@dataclass(frozen=True, slots=True)
class Config:
    network: NetworkConfig
    storage: StorageConfig
    sync: SyncConfig
    download: DownloadConfig
    source_path: Path
    runtime_mode: str

    def with_download_dir(self, path: Path) -> Config:
        return replace(self, storage=replace(self.storage, download_dir=path))


@dataclass(frozen=True, slots=True)
class DownloadDirAssessment:
    tracked: int
    found: int
    missing: int


def discover_runtime_root() -> tuple[Path, str]:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent, "frozen"
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root, "source"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ConfigError("LOCALAPPDATA is required for an installed wheel runtime")
    return Path(local_app_data).resolve() / "YandeSync", "installed"


def default_config_text() -> str:
    return """[network]
proxy = ""
control_url = "http://127.0.0.1:9790"
require_proxy = false
allow_direct = true
timeout_seconds = 30
max_retries = 3
max_redirects = 3

[download]
max_file_size_bytes = 2147483648

[storage]

[sync]
page_size = 100
initial_limit = 2000
known_stop_count = 50
"""


def bootstrap_config(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(default_config_text(), encoding="utf-8")
    os.replace(temporary, path)


def load_config(path: Path | None = None) -> Config:
    if path is None:
        runtime_root, runtime_mode = discover_runtime_root()
        config_path = runtime_root / "config.toml"
    else:
        config_path = path.resolve()
        runtime_root, runtime_mode = config_path.parent, "explicit"
    if not config_path.is_file():
        raise ConfigError(f"configuration file does not exist: {config_path}")
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    network = raw.get("network", {})
    proxy = str(network.get("proxy", "")).strip() or None
    require_proxy = bool(network.get("require_proxy", False))
    allow_direct = bool(network.get("allow_direct", True))
    if proxy is not None:
        parsed_proxy = urlsplit(proxy)
        try:
            proxy_port = parsed_proxy.port
        except ValueError as exc:
            raise ConfigError("proxy port is invalid") from exc
        if (
            parsed_proxy.scheme not in {"http", "https"}
            or not parsed_proxy.hostname
            or proxy_port is None
            or parsed_proxy.username is not None
            or parsed_proxy.password is not None
            or parsed_proxy.path not in {"", "/"}
            or parsed_proxy.query
            or parsed_proxy.fragment
        ):
            raise ConfigError("proxy must be an http(s) URL with an explicit port")
    if require_proxy and proxy is None:
        raise ConfigError("require_proxy requires a configured proxy")
    if proxy is None and not allow_direct:
        raise ConfigError("direct connections cannot be disabled without a proxy")
    control_url = str(network.get("control_url", "http://127.0.0.1:9790"))
    if control_url != "http://127.0.0.1:9790":
        raise ConfigError("control_url must be http://127.0.0.1:9790")
    timeout_seconds = float(network.get("timeout_seconds", 30))
    max_retries = int(network.get("max_retries", 3))
    max_redirects = int(network.get("max_redirects", 3))
    if not 1 <= timeout_seconds <= 120:
        raise ConfigError("timeout_seconds must be between 1 and 120")
    if not 0 <= max_retries <= 5:
        raise ConfigError("max_retries must be between 0 and 5")
    if not 0 <= max_redirects <= 3:
        raise ConfigError("max_redirects must be between 0 and 3")

    max_file_size = int(raw.get("download", {}).get("max_file_size_bytes", 2_147_483_648))
    if not 1_048_576 <= max_file_size <= 10_737_418_240:
        raise ConfigError("max_file_size_bytes must be between 1 MiB and 10 GiB")

    storage = raw.get("storage", {})
    legacy_root = None
    legacy_value = storage.get("root")
    if legacy_value is not None:
        legacy_root = Path(str(legacy_value))
        if not legacy_root.is_absolute():
            legacy_root = (config_path.parent / legacy_root).resolve()
    configured_download = storage.get("download_dir")
    if configured_download is not None and str(configured_download).strip():
        try:
            download_dir = validate_download_root(Path(str(configured_download)))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    elif legacy_root is not None:
        download_dir = (legacy_root / "downloads").resolve()
    else:
        download_dir = None

    return Config(
        network=NetworkConfig(
            proxy=proxy,
            control_url=control_url,
            require_proxy=require_proxy,
            allow_direct=allow_direct,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            max_redirects=max_redirects,
        ),
        storage=StorageConfig(runtime_root.resolve(), download_dir, legacy_root),
        sync=SyncConfig(
            page_size=min(100, max(1, int(raw.get("sync", {}).get("page_size", 100)))),
            initial_limit=max(1, int(raw.get("sync", {}).get("initial_limit", 2000))),
            known_stop_count=max(1, int(raw.get("sync", {}).get("known_stop_count", 50))),
        ),
        download=DownloadConfig(max_file_size),
        source_path=config_path,
        runtime_mode=runtime_mode,
    )


def write_download_dir(config_path: Path, download_dir: Path) -> None:
    path = validate_download_root(download_dir)
    text = config_path.read_text(encoding="utf-8")
    rendered = str(path).replace("\\", "\\\\").replace('"', '\\"')
    line = f'download_dir = "{rendered}"'
    section = re.search(r"(?ms)^\[storage\]\s*$.*?(?=^\[|\Z)", text)
    if section:
        block = section.group(0)
        if re.search(r"(?m)^download_dir\s*=.*$", block):
            replacement = re.sub(r"(?m)^download_dir\s*=.*$", lambda _match: line, block)
        else:
            replacement = block.rstrip() + "\n" + line + "\n\n"
        updated = text[:section.start()] + replacement + text[section.end():]
    else:
        updated = text.rstrip() + f"\n\n[storage]\n{line}\n"
    backup = config_path.with_suffix(config_path.suffix + ".bak")
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    backup.write_text(text, encoding="utf-8")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, config_path)


def assess_download_dir(database, download_dir: Path) -> DownloadDirAssessment:
    root = validate_download_root(download_dir)
    version = int(database.connection.execute("PRAGMA user_version").fetchone()[0])
    if version == 0:
        columns = {
            str(row[1]) for row in database.connection.execute("PRAGMA table_info(posts)")
        }
        if "local_path" not in columns:
            raise ConfigError("cannot assess an unrecognized legacy database")
        configured_root = database.download_root
        try:
            remembered = database.connection.execute(
                "SELECT value FROM settings WHERE key='download_dir'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ConfigError("cannot determine the legacy download directory") from exc
        if remembered and remembered[0]:
            configured_root = validate_download_root(Path(str(remembered[0])))
        if configured_root is None:
            raise ConfigError("legacy database assessment requires the existing download root")
        rows = database.connection.execute(
            "SELECT post_id,local_path FROM posts WHERE status='downloaded' AND local_path IS NOT NULL"
        ).fetchall()
        relative_paths = []
        for post_id, local_path in rows:
            legacy_path = Path(str(local_path))
            if not legacy_path.is_absolute():
                raise ConfigError(f"legacy local_path is not absolute for post {post_id}")
            try:
                relative_paths.append(
                    legacy_path.resolve().relative_to(configured_root.resolve())
                )
            except ValueError as exc:
                raise ConfigError(
                    f"legacy local_path is outside download_dir for post {post_id}"
                ) from exc
    elif version in {1, 2}:
        rows = database.connection.execute(
            """SELECT relative_path FROM posts
            WHERE status='downloaded' AND relative_path IS NOT NULL"""
        ).fetchall()
        relative_paths = [row[0] for row in rows]
    elif version in {3, 4}:
        rows = database.connection.execute(
            """SELECT relative_path FROM query_posts
            WHERE status='downloaded' AND relative_path IS NOT NULL"""
        ).fetchall()
        relative_paths = [row[0] for row in rows]
    elif version == 5:
        rows = database.connection.execute(
            """SELECT relative_path FROM collection_posts
            WHERE status='downloaded' AND relative_path IS NOT NULL"""
        ).fetchall()
        relative_paths = [row[0] for row in rows]
    else:
        raise ConfigError(f"cannot assess unsupported database schema {version}")
    found = sum(safe_library_path(root, relative).is_file() for relative in relative_paths)
    tracked = len(relative_paths)
    return DownloadDirAssessment(tracked, found, tracked - found)


def change_download_dir(config: Config, database, download_dir: Path, *,
                        accept_missing: bool = False, interactive: bool = False,
                        confirm=None) -> tuple[Config, DownloadDirAssessment]:
    root = validate_download_root(download_dir)
    assessment = assess_download_dir(database, root)
    require_download_dir_acceptance(
        assessment, accept_missing=accept_missing, interactive=interactive, confirm=confirm
    )
    root.mkdir(parents=True, exist_ok=True)
    write_download_dir(config.source_path, root)
    database.download_root = root
    return config.with_download_dir(root), assessment


def require_download_dir_acceptance(assessment: DownloadDirAssessment, *,
                                    accept_missing: bool = False,
                                    interactive: bool = False, confirm=None) -> None:
    accepted = accept_missing
    if assessment.missing and not accepted and interactive:
        response = (confirm or input)(
            "Tracked files are missing at this location. Accept it anyway? [y/N] "
        )
        accepted = response.strip().lower() in {"y", "yes"}
    if assessment.missing and not accepted:
        raise ConfigError(
            f"download_dir unchanged: {assessment.missing} tracked files are missing; "
            "use --accept-missing only after verifying the location"
        )
