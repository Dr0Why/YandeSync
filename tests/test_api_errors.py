from __future__ import annotations

import pytest

from yande_sync.errors import RemoteDataError
from yande_sync.yande_api import YandeApi


class Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.closed = False

    def json(self):
        if self.error:
            raise self.error
        return self.payload

    def close(self):
        self.closed = True


class Client:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


@pytest.mark.parametrize(
    ("payload", "message"),
    [({"id": 1}, "must be a list"), (["not-an-object"], "not an object"),
     ([{"id": 1}], "invalid yande.re record")],
)
def test_malformed_api_records_are_operational_errors(payload, message):
    response = Response(payload)
    with pytest.raises(RemoteDataError, match=message):
        YandeApi(Client(response)).page("tag", 1)
    assert response.closed


def test_invalid_json_is_wrapped_and_response_is_closed():
    response = Response(error=ValueError("bad json"))
    with pytest.raises(RemoteDataError, match="malformed JSON"):
        YandeApi(Client(response)).page("tag", 1)
    assert response.closed
