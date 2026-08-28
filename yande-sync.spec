# -*- mode: python ; coding: utf-8 -*-

import os
import shutil

a = Analysis(
    ["src/yande_sync/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[("config.example.toml", "."), ("README.md", ".")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yande-sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
bundle = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="YandeSync",
)

release_root = os.path.join(DISTPATH, "YandeSync")
shutil.copy2("config.example.toml", os.path.join(release_root, "config.example.toml"))
shutil.copy2("README.md", os.path.join(release_root, "README.md"))
