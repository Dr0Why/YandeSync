from __future__ import annotations

import socket
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Config
from .network import NetworkError, SafeHttpClient
from .security import SecurityError, validate_url


@dataclass(slots=True)
class DoctorResult:
    ok: bool
    checks: list[tuple[str, bool, str]]


def _port_open(host: str, port: int, timeout: float = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_doctor(config: Config, *, probe_remote: bool = True) -> DoctorResult:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python", sys.version_info >= (3, 13), sys.version.split()[0]))
    try:
        config.storage.check_access()
        checks.append(("存储目录", True, str(config.storage.root)))
    except OSError as exc:
        checks.append(("存储目录", False, str(exc)))
    proxy_open = True
    if config.network.proxy:
        parsed = urlsplit(config.network.proxy)
        proxy_open = _port_open(parsed.hostname or "", parsed.port or 0)
        checks.append(("本机代理端口", proxy_open, config.network.proxy))

    rejected = 0
    for url in ("http://yande.re", "https://example.com", "https://yande.re.evil.example"):
        try:
            validate_url(url)
        except SecurityError:
            rejected += 1
    checks.append(("URL 白名单", rejected == 3, f"拒绝 {rejected}/3 个恶意样例"))

    can_probe = proxy_open or not config.network.require_proxy
    access_label = "代理访问 yande.re" if config.network.proxy else "直连访问 yande.re"
    if probe_remote and can_probe:
        try:
            with SafeHttpClient(config.network) as client:
                response = client.get(
                    "https://yande.re/post.json", purpose="api", params={"limit": 1}
                )
                response.close()
            checks.append((access_label, True, "成功"))
        except (NetworkError, SecurityError, ValueError) as exc:
            checks.append((access_label, False, str(exc)))
    elif probe_remote:
        checks.append((access_label, False, "代理端口未监听"))
    return DoctorResult(all(item[1] for item in checks), checks)


def print_doctor(result: DoctorResult) -> None:
    for name, ok, detail in result.checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    print("doctor 通过" if result.ok else "doctor 未通过，已禁止网络任务")
