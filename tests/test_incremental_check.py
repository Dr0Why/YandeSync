from yande_sync.compare import incremental_check
from yande_sync.models import Post


def make_post(post_id):
    return Post(post_id, f"{post_id}.jpg", "jpg", 1, 1, 1, "x" * 32,
                f"https://files.yande.re/{post_id}.jpg", "tag", "", None)


class Api:
    page_size = 2

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def page(self, _tags, page, _limit):
        self.calls += 1
        return self.pages[page - 1] if page <= len(self.pages) else []


def test_second_check_has_zero_new(db):
    query = db.add_query("tag", "tag")
    source = db.collection_sources(query["collection_id"])[0]
    api = Api([[make_post(2), make_post(1)], []])
    first = incremental_check(db, api, source, limit=10, known_stop_count=2)
    source = db.collection_sources(query["collection_id"])[0]
    second = incremental_check(db, Api([[make_post(2), make_post(1)]]), source,
                               limit=10, known_stop_count=2)
    assert len(first.new_posts) == 2
    assert second.new_posts == []


def test_incremental_stops_after_known_batch(db):
    query = db.add_query("tag", "tag")
    db.store_posts(query["query_id"], [make_post(3), make_post(2), make_post(1)])
    source = db.collection_sources(query["collection_id"])[0]
    api = Api([[make_post(4), make_post(3)], [make_post(2), make_post(1)], [make_post(0)]])
    result = incremental_check(db, api, source, limit=10, known_stop_count=2)
    assert [item.post_id for item in result.new_posts] == [4]
    assert api.calls == 2
