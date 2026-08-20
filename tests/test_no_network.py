"""Erzwingt die Zusage, dass das Programm keine Netzwerkkommunikation betreibt.

Zwei unabhaengige Pruefungen:

1. **Statisch.** Der ausgelieferte Quellcode importiert kein Netzwerkmodul.
2. **Dynamisch.** Mit einem gesperrten `socket` laesst sich das gesamte Paket
   importieren, ohne dass ein Socket entsteht.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import socket
from pathlib import Path

import pytest

import msgbackup_extractor

#: Module, deren Verwendung eine Netzwerkverbindung ermoeglichen wuerde.
FORBIDDEN_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "http.client",
        "https",
        "urllib",
        "urllib.request",
        "urllib3",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "requests",
        "httpx",
        "aiohttp",
        "asyncio",
        "xmlrpc",
        "webbrowser",
        "socketserver",
        "multiprocessing.connection",
    }
)

SOURCE_ROOT = Path(msgbackup_extractor.__file__).parent


def _source_files() -> list[Path]:
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    assert files, "Es wurden keine Quelldateien gefunden"
    return files


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_source_file_imports_no_network_module(path: Path) -> None:
    imported = _imported_modules(path)
    forbidden = {
        name
        for name in imported
        if name in FORBIDDEN_MODULES or name.split(".")[0] in FORBIDDEN_MODULES
    }
    assert not forbidden, f"{path.name} importiert Netzwerkmodule: {sorted(forbidden)}"


def test_importing_the_whole_package_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sperrt `socket` und importiert danach jedes Modul des Pakets."""

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Es wurde versucht, einen Socket zu oeffnen")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)

    for module in pkgutil.walk_packages(
        msgbackup_extractor.__path__, prefix="msgbackup_extractor."
    ):
        importlib.import_module(module.name)


def test_no_url_literals_in_source() -> None:
    """Keine Endpunkte im Code - auch nicht auskommentiert oder ungenutzt."""
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for marker in ("http://", "https://", "ftp://", "ws://", "wss://"):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert not offenders, f"URL-Literale gefunden: {offenders}"
