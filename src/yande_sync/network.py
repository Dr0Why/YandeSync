from __future__ import annotations

import time
from typing import ClassVar
from urllib.parse import urljoin

import requests

from .config import NetworkConfig
from .errors import OperationalError
from .security import validate_url


class NetworkError(OperationalError):
    pass


class SafeHttpClient:
    REDIRECT_CODES: ClassVar[frozenset[int]] = frozenset({301, 302, 307, 308})

    def __init__(self, config: NetworkConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.proxies.clear()
        if config.proxy:
            self.session.proxies.update({"http": config.proxy, "https": config.proxy})
        self.visited: list[tuple[str, str]] = []

    def get(self, url: str, *, purpose: str, stream: bool = False, params=None):
        current = validate_url(url, purpose=purpose)
        redirects = 0
        for attempt in range(self.config.max_retries + 1):
            try:
                while True:
                    validate_url(current, purpose=purpose)
                    self.visited.append((purpose, current))
                    proxies = (
                        {"http": self.config.proxy, "https": self.config.proxy}
                        if self.config.proxy else {}
                    )
                    response = self.session.get(
                        current,
                        params=params,
                        timeout=self.config.timeout_seconds,
                        allow_redirects=False,
                        stream=stream,
                        proxies=proxies,
                    )
                    params = None
                    if response.status_code not in self.REDIRECT_CODES:
                        response.raise_for_status()
                        return response
                    response.close()
                    redirects += 1
                    if redirects > self.config.max_redirects:
                        raise NetworkError("重定向次数超过安全上限")
                    location = response.headers.get("Location")
                    if not location:
                        raise NetworkError("重定向响应缺少 Location")
                    current = validate_url(urljoin(current, location), purpose=purpose)
            except (requests.RequestException, NetworkError) as exc:
                if attempt >= self.config.max_retries:
                    if self.config.proxy:
                        message = "代理请求失败（未尝试直连）"
                    else:
                        message = "直连请求失败"
                    raise NetworkError(f"{message}: {exc}") from exc
                time.sleep(min(0.25 * (2**attempt), 1.0))
        message = "代理请求失败（未尝试直连）" if self.config.proxy else "直连请求失败"
        raise NetworkError(message)

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
