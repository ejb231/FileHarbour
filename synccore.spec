# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SyncCore - one-folder distribution."""

import importlib
import os
import pathlib

block_cipher = None
ROOT = os.path.abspath(os.path.join(os.path.dirname(SPECPATH), ""))

if not os.path.isfile(os.path.join(ROOT, "main.py")):
    ROOT = os.path.abspath(".")

_rich_ud_dir = pathlib.Path(importlib.import_module("rich._unicode_data").__file__).parent
_rich_ud_files = [(str(path), "rich/_unicode_data") for path in _rich_ud_dir.glob("*.py")]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, ".syncignore"), "."),
    ]
    + _rich_ud_files,
    hiddenimports=[
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "uvicorn.lifespan",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.logging",
        "multipart",
        "multipart.multipart",
        "pydantic",
        "pydantic_settings",
        "rich._unicode_data",
        "config",
        "core",
        "core.server",
        "core.client",
        "core.engine",
        "core.watcher",
        "core.queue_worker",
        "core.orchestrator",
        "utils",
        "utils.auth",
        "utils.certs",
        "utils.conflict",
        "utils.file_index",
        "utils.file_ops",
        "utils.filters",
        "utils.logging",
        "utils.paths",
        "utils.resilience",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "pytest_asyncio",
        "test",
        "tests",
        "tkinter",
        "_tkinter",
        "unittest",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SyncCore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SyncCore",
)
