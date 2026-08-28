from pathlib import Path

import pytest

from yande_sync.cli import (
    DEFAULT_SYNC_CONCURRENCY,
    DEFAULT_SYNC_LIMIT,
    PUBLIC_COMMANDS,
    parser,
)
from yande_sync.config import ConfigError, bootstrap_config, load_config


def test_public_command_surface_is_small():
    choices = next(
        action.choices for action in parser()._actions if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    assert tuple(choices) == PUBLIC_COMMANDS


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], 2000),
        (["--limit", "1"], 1),
        (["--limit", "500"], 500),
        (["--limit", "5000"], 5000),
    ],
)
def test_sync_limit_default_and_explicit_overrides(arguments, expected):
    args = parser().parse_args(["sync", *arguments])
    assert args.limit == expected
    assert DEFAULT_SYNC_LIMIT == 2000


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_sync_limit_still_rejects_invalid_values(value):
    with pytest.raises(SystemExit):
        parser().parse_args(["sync", "--limit", value])


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [([], 8), (["--concurrency", "1"], 1), (["--concurrency", "8"], 8),
     (["--concurrency", "16"], 16), (["--concurrency", "32"], 32)],
)
def test_sync_concurrency_default_and_explicit_overrides(arguments, expected):
    args = parser().parse_args(["sync", *arguments])
    assert args.concurrency == expected
    assert DEFAULT_SYNC_CONCURRENCY == 8


@pytest.mark.parametrize("value", ["0", "-1", "33", "abc"])
def test_sync_concurrency_rejects_out_of_range_and_non_integer_values(value):
    with pytest.raises(SystemExit):
        parser().parse_args(["sync", "--concurrency", value])


def test_bootstrap_discovery_limit_matches_sync_default(tmp_path):
    config_path = tmp_path / "config.toml"
    bootstrap_config(config_path)
    assert load_config(config_path).sync.initial_limit == DEFAULT_SYNC_LIMIT


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["tag_a"], ["tag_a"]),
        (["tag_a", "tag_b", "tag_c"], ["tag_a", "tag_b", "tag_c"]),
        (["artist_name rating:safe"], ["artist_name rating:safe"]),
        (
            ["artist name", "artist_old_name rating:safe"],
            ["artist name", "artist_old_name rating:safe"],
        ),
    ],
)
def test_query_add_preserves_argument_boundaries(arguments, expected):
    args = parser().parse_args(["query", "add", *arguments])
    assert args.tags == expected


def test_config_set_has_narrow_accept_missing_option():
    args = parser().parse_args(
        ["config", "set", "download-dir", r"E:\Pictures\Yande", "--accept-missing"]
    )
    assert args.accept_missing is True
    assert args.value == Path(r"E:\Pictures\Yande")


def test_legacy_archive_root_is_resolved_from_config_location(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """[network]
proxy = "http://127.0.0.1:10090"
require_proxy = true
allow_direct = false

[storage]
root = "YandeArchive"
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.storage.root == tmp_path.resolve()
    assert config.storage.legacy_root == (tmp_path / "YandeArchive").resolve()
    assert config.storage.download_dir == (tmp_path / "YandeArchive" / "downloads").resolve()


def test_default_config_is_not_loaded_from_current_working_directory(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("invalid = true", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.source_path == (Path(__file__).resolve().parents[1] / "config.toml")


@pytest.mark.parametrize(
    ("line", "replacement"),
    [
        ("proxy = \"\"", "proxy = \"socks5://127.0.0.1:10090\""),
        ("proxy = \"\"", "proxy = \"http://user:pass@127.0.0.1:10090\""),
        ("control_url = \"http://127.0.0.1:9790\"", "control_url = \"http://example.com\""),
        ("max_redirects = 3", "max_redirects = 100"),
    ],
)
def test_security_configuration_cannot_be_relaxed(tmp_path, line, replacement):
    source = (Path(__file__).resolve().parents[1] / "config.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(source.replace(line, replacement), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_default_network_configuration_uses_direct_connection(tmp_path):
    path = tmp_path / "config.toml"
    bootstrap_config(path)

    network = load_config(path).network

    assert network.proxy is None
    assert network.require_proxy is False
    assert network.allow_direct is True


def test_optional_proxy_configuration_is_accepted(tmp_path):
    path = tmp_path / "config.toml"
    bootstrap_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'proxy = ""', 'proxy = "http://127.0.0.1:8080"'
        ),
        encoding="utf-8",
    )

    assert load_config(path).network.proxy == "http://127.0.0.1:8080"


def test_required_proxy_must_be_configured(tmp_path):
    path = tmp_path / "config.toml"
    bootstrap_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "require_proxy = false", "require_proxy = true"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="require_proxy"):
        load_config(path)
