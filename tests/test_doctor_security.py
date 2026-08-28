from pathlib import Path
from typing import ClassVar

import pytest
import requests

from yande_sync import cli as cli_module
from yande_sync.config import load_config
from yande_sync.doctor import print_doctor, run_doctor
from yande_sync.network import NetworkError


@pytest.fixture
def config(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    text = source.read_text(encoding="utf-8")
    text = text.replace('proxy = ""', 'proxy = "http://127.0.0.1:10090"')
    text = text.replace("require_proxy = false", "require_proxy = true")
    text = text.replace("allow_direct = true", "allow_direct = false")
    config_path.write_text(text, encoding="utf-8")
    return load_config(config_path)


class FakeResponse:
    def close(self):
        pass


class MihomoConfigResponse(FakeResponse):
    is_redirect = False
    is_permanent_redirect = False

    def raise_for_status(self):
        pass

    def json(self):
        return {"allow-lan": True, "bind-address": "*"}


class MihomoSession:
    def __init__(self):
        self.requested = False

    def get(self, _url, **_kwargs):
        self.requested = True
        return MihomoConfigResponse()

    def close(self):
        pass


class SuccessfulClient:
    requested: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, _network):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get(self, url, **kwargs):
        self.requested.append((url, kwargs))
        return FakeResponse()


class FailingClient(SuccessfulClient):
    def get(self, _url, **_kwargs):
        raise NetworkError("proxy request failed")


def doctor_check(result, name):
    return next(check for check in result.checks if check[0] == name)


def test_doctor_ignores_mihomo_lan_and_bind_configuration(
    config, monkeypatch, capsys
):
    mihomo_session = MihomoSession()
    monkeypatch.setattr(requests, "Session", lambda: mihomo_session)
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: True)

    result = run_doctor(config, probe_remote=False)
    print_doctor(result)

    assert result.ok is True
    assert [check[0] for check in result.checks] == [
        "Python",
        "存储目录",
        "本机代理端口",
        "URL 白名单",
    ]
    output = capsys.readouterr().out
    assert "Mihomo 安全配置" not in output
    assert "AllowLAN" not in output
    assert "BindAddress" not in output
    assert mihomo_session.requested is False


def test_all_remaining_doctor_checks_execute(config, monkeypatch):
    SuccessfulClient.requested.clear()
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: True)
    monkeypatch.setattr("yande_sync.doctor.SafeHttpClient", SuccessfulClient)

    result = run_doctor(config)

    assert result.ok is True
    assert [check[0] for check in result.checks] == [
        "Python",
        "存储目录",
        "本机代理端口",
        "URL 白名单",
        "代理访问 yande.re",
    ]
    assert SuccessfulClient.requested == [
        (
            "https://yande.re/post.json",
            {"purpose": "api", "params": {"limit": 1}},
        )
    ]


def test_direct_mode_skips_proxy_port_check(config, monkeypatch):
    direct_network = config.network.__class__(
        None, config.network.control_url, False, True,
        config.network.timeout_seconds, config.network.max_retries,
        config.network.max_redirects,
    )
    direct_config = config.__class__(
        direct_network, config.storage, config.sync, config.download,
        config.source_path, config.runtime_mode,
    )
    SuccessfulClient.requested.clear()
    monkeypatch.setattr(
        "yande_sync.doctor._port_open",
        lambda *_args: (_ for _ in ()).throw(AssertionError("proxy check is not expected")),
    )
    monkeypatch.setattr("yande_sync.doctor.SafeHttpClient", SuccessfulClient)

    result = run_doctor(direct_config)

    assert result.ok is True
    assert "本机代理端口" not in [check[0] for check in result.checks]
    assert doctor_check(result, "直连访问 yande.re")[1] is True
    assert "代理访问 yande.re" not in [check[0] for check in result.checks]
    assert SuccessfulClient.requested


def test_unavailable_proxy_still_fails_doctor(config, monkeypatch):
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: False)

    result = run_doctor(config)

    assert result.ok is False
    assert doctor_check(result, "本机代理端口")[1] is False
    assert doctor_check(result, "代理访问 yande.re")[1] is False


def test_failed_url_allowlist_validation_still_fails_doctor(config, monkeypatch):
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: True)
    monkeypatch.setattr("yande_sync.doctor.validate_url", lambda _url: None)

    result = run_doctor(config, probe_remote=False)

    assert result.ok is False
    assert doctor_check(result, "URL 白名单")[1] is False


def test_failed_yande_proxy_connectivity_still_fails_doctor(config, monkeypatch):
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: True)
    monkeypatch.setattr("yande_sync.doctor.SafeHttpClient", FailingClient)

    result = run_doctor(config)

    assert result.ok is False
    assert doctor_check(result, "代理访问 yande.re")[1] is False


def test_require_doctor_allows_operation_when_remaining_checks_pass(
    config, monkeypatch, capsys
):
    monkeypatch.setattr("yande_sync.doctor._port_open", lambda *_args: True)
    monkeypatch.setattr("yande_sync.doctor.SafeHttpClient", SuccessfulClient)
    monkeypatch.setattr(cli_module, "run_doctor", run_doctor)

    cli_module.require_doctor(config)

    assert "doctor 通过" in capsys.readouterr().out
