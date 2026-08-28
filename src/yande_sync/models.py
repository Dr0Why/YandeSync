from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .security import validate_file_extension, validate_file_metadata, validate_url


@dataclass(frozen=True, slots=True)
class Post:
    post_id: int
    file_name: str
    file_ext: str
    width: int
    height: int
    file_size: int
    md5: str
    file_url: str
    tags: str
    source: str
    remote_created_at: str | None

    @classmethod
    def from_api(cls, data: dict) -> Post:
        post_id = int(data["id"])
        file_url = str(data["file_url"])
        validate_url(file_url, purpose="file")
        suffix = Path(file_url.split("?", 1)[0]).suffix.lower().lstrip(".")
        ext = validate_file_extension(str(data.get("file_ext") or suffix))
        file_size = int(data.get("file_size") or 0)
        md5 = str(data.get("md5") or "").lower()
        validate_file_metadata(file_size, md5)
        return cls(
            post_id=post_id,
            file_name=f"{post_id}.{ext}",
            file_ext=ext,
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            file_size=file_size,
            md5=md5,
            file_url=file_url,
            tags=str(data.get("tags") or ""),
            source=str(data.get("source") or ""),
            remote_created_at=str(data.get("created_at") or "") or None,
        )


@dataclass(frozen=True, slots=True)
class DownloadResult:
    post_id: int
    local_path: Path | None
    bytes_received: int
    actual_md5: str
    result: str
    error_type: str | None = None
    error_message: str | None = None
