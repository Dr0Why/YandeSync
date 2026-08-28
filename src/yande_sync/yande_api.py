from __future__ import annotations

from .errors import RemoteDataError
from .models import Post
from .network import SafeHttpClient
from .security import SecurityError

API_URL = "https://yande.re/post.json"


class YandeApi:
    def __init__(self, client: SafeHttpClient, page_size: int = 100):
        self.client = client
        self.page_size = page_size

    def page(self, tags: str, page: int, limit: int | None = None) -> list[Post]:
        count = min(self.page_size, limit or self.page_size)
        response = self.client.get(
            API_URL, purpose="api", params={"tags": tags, "page": page, "limit": count}
        )
        try:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RemoteDataError("yande.re returned malformed JSON") from exc
        finally:
            response.close()
        if not isinstance(payload, list):
            raise RemoteDataError("yande.re response must be a list")
        posts = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise RemoteDataError(f"yande.re record {index} is not an object")
            try:
                posts.append(Post.from_api(item))
            except (KeyError, TypeError, ValueError, SecurityError) as exc:
                post_id = item.get("id", "unknown")
                raise RemoteDataError(
                    f"invalid yande.re record at index {index}, id={post_id}"
                ) from exc
        return posts
