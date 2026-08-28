from __future__ import annotations

from dataclasses import dataclass

from .database import Database
from .models import Post
from .yande_api import YandeApi


@dataclass(slots=True)
class CheckResult:
    received: list[Post]
    new_posts: list[Post]
    pages_requested: int


def incremental_check(db: Database, api: YandeApi, source, *, limit: int,
                      known_stop_count: int) -> CheckResult:
    known = db.source_post_ids(int(source["source_id"]))
    first_check = not known
    received: list[Post] = []
    consecutive_known = 0
    page = 1
    while len(received) < limit:
        batch = api.page(source["tag_query"], page, min(limit - len(received), api.page_size))
        if not batch:
            break
        received.extend(batch)
        if not first_check:
            for post in batch:
                if post.post_id in known and post.post_id <= (source["highest_seen_post_id"] or 0):
                    consecutive_known += 1
                else:
                    consecutive_known = 0
                if consecutive_known >= known_stop_count:
                    break
        if consecutive_known >= known_stop_count or len(batch) < api.page_size:
            break
        page += 1
    new_posts = db.store_source_posts(
        int(source["source_id"]), int(source["collection_id"]), received
    )
    return CheckResult(received, new_posts, page)
