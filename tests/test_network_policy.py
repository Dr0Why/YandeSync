import pytest
import requests

from yande_sync.config import NetworkConfig
from yande_sync.network import NetworkError, SafeHttpClient
from yande_sync.security import SecurityError


class FakeResponse:
    def __init__(self, status=200, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.trust_env = True
        self.proxies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def test_ignores_environment_and_forces_proxy(network_config):
    session = FakeSession([FakeResponse()])
    client = SafeHttpClient(network_config, session)
    client.get("https://yande.re/post.json", purpose="api")
    assert session.trust_env is False
    assert session.calls[0][1]["proxies"] == {
        "http": "http://127.0.0.1:10090", "https": "http://127.0.0.1:10090"
    }
    assert session.calls[0][1]["allow_redirects"] is False


def test_direct_connection_is_default_and_ignores_environment_proxy():
    config = NetworkConfig(None, "http://127.0.0.1:9790", False, True, 1, 0, 3)
    session = FakeSession([FakeResponse()])
    session.proxies.update({"https": "http://environment.invalid:8080"})

    client = SafeHttpClient(config, session)
    client.get("https://yande.re/post.json", purpose="api")

    assert session.trust_env is False
    assert session.proxies == {}
    assert session.calls[0][1]["proxies"] == {}


def test_third_party_redirect_rejected_before_request(network_config):
    session = FakeSession([FakeResponse(302, "https://evil.example/file.jpg")])
    client = SafeHttpClient(network_config, session)
    with pytest.raises(SecurityError):
        client.get("https://files.yande.re/a.jpg", purpose="file")
    assert len(session.calls) == 1


def test_proxy_failure_does_not_retry_direct(network_config):
    session = FakeSession([requests.exceptions.ProxyError("down")])
    client = SafeHttpClient(network_config, session)
    with pytest.raises(NetworkError, match="未尝试直连"):
        client.get("https://yande.re/post.json", purpose="api")
    assert len(session.calls) == 1
    assert session.calls[0][1]["proxies"]


def test_direct_failure_uses_direct_connection_message():
    config = NetworkConfig(None, "http://127.0.0.1:9790", False, True, 1, 0, 3)
    session = FakeSession([requests.exceptions.ConnectionError("down")])
    client = SafeHttpClient(config, session)

    with pytest.raises(NetworkError, match="直连请求失败") as captured:
        client.get("https://yande.re/post.json", purpose="api")

    assert "代理" not in str(captured.value)
    assert len(session.calls) == 1
    assert session.calls[0][1]["proxies"] == {}


def test_real_closed_local_proxy_port_fails_without_direct_connection():
    config = NetworkConfig("http://127.0.0.1:1", "http://127.0.0.1:9790", True,
                           False, 0.2, 0, 0)
    with SafeHttpClient(config) as client, pytest.raises(NetworkError, match="未尝试直连"):
        client.get("https://yande.re/post.json", purpose="api")
