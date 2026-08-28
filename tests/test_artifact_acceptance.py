from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _assert_runtime(root: Path) -> None:
    assert (root / "config.toml").is_file()
    assert (root / "data" / "yande-sync.db").is_file()
    assert (root / "logs").is_dir()
    assert (root / "temp").is_dir()
    assert (root / "operation.lock").is_file()


def _assert_no_runtime(root: Path) -> None:
    assert not (root / "config.toml").exists()
    assert not (root / "data").exists()
    assert not (root / "logs").exists()
    assert not (root / "temp").exists()
    assert not (root / "operation.lock").exists()


def _copy_installed_distribution(name: str, destination: Path) -> None:
    package = distribution(name)
    for entry in package.files or ():
        if ".." in entry.parts:
            continue
        source = Path(package.locate_file(entry))
        target = destination / entry
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


@pytest.mark.skipif(not os.environ.get("YANDE_SYNC_PORTABLE_EXE"), reason="artifact not built")
def test_actual_frozen_bundle_is_relocatable(tmp_path):
    original_executable = Path(os.environ["YANDE_SYNC_PORTABLE_EXE"]).resolve()
    original_root = original_executable.parent
    moved_root = tmp_path / "moved" / "YandeSync"
    shutil.copytree(original_root, moved_root)
    executable = moved_root / original_executable.name
    cwd = tmp_path / "cwd"
    local_app_data = tmp_path / "local-app-data"
    fake_home = tmp_path / "home"
    library = tmp_path / "library"
    for directory in (cwd, local_app_data, fake_home, library):
        directory.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"LOCALAPPDATA": str(local_app_data), "USERPROFILE": str(fake_home)})

    _run(
        [str(executable), "config", "set", "download-dir", str(library)], cwd=cwd, env=env
    )
    _run([str(executable), "query", "add", "artifact-test"], cwd=cwd, env=env)

    _assert_runtime(moved_root)
    _assert_no_runtime(cwd)
    _assert_no_runtime(local_app_data / "YandeSync")
    _assert_no_runtime(fake_home / "YandeSync")
    _assert_no_runtime(original_root)


@pytest.mark.skipif(not os.environ.get("YANDE_SYNC_WHEEL"), reason="wheel not built")
def test_actual_wheel_uses_local_app_data_not_scripts(tmp_path):
    wheel = Path(os.environ["YANDE_SYNC_WHEEL"]).resolve()
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "Scripts" / "python.exe"
    launcher = environment / "Scripts" / "yande-sync.exe"
    site_packages = environment / "Lib" / "site-packages"
    for dependency in ("requests", "certifi", "charset-normalizer", "idna", "urllib3"):
        _copy_installed_distribution(dependency, site_packages)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        capture_output=True, text=True, check=True,
    )
    cwd = tmp_path / "cwd"
    local_app_data = tmp_path / "local-app-data"
    fake_home = tmp_path / "home"
    library = tmp_path / "library"
    for directory in (cwd, local_app_data, fake_home, library):
        directory.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"LOCALAPPDATA": str(local_app_data), "USERPROFILE": str(fake_home)})

    _run(
        [str(launcher), "config", "set", "download-dir", str(library)], cwd=cwd, env=env
    )
    _run([str(launcher), "query", "add", "artifact-test"], cwd=cwd, env=env)

    _assert_runtime(local_app_data / "YandeSync")
    _assert_no_runtime(cwd)
    _assert_no_runtime(environment / "Scripts")
    _assert_no_runtime(fake_home / "YandeSync")
