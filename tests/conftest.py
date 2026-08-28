from __future__ import annotations

from pathlib import Path

import pytest

from yande_sync.config import NetworkConfig
from yande_sync.database import Database


@pytest.fixture
def network_config():
    return NetworkConfig("http://127.0.0.1:10090", "http://127.0.0.1:9790", True,
                         False, 1, 0, 3)


@pytest.fixture
def db(tmp_path: Path):
    with Database(tmp_path / "state.db", tmp_path / "downloads") as database:
        yield database
