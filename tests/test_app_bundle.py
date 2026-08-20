"""Das doppelklickbare macOS-Bundle.

Der Bauer liegt in `scripts/`, nicht im Paket: er ist ein Werkzeug fuer eine
Plattform, keine Bibliotheksfunktion. Geladen wird er hier ueber den Pfad.

Geprueft wird die Struktur, die Rechte und das Einsetzen des Pfades - nicht,
ob macOS das Bundle mag. Das kann nur ein Doppelklick zeigen.
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILDER = REPO / "scripts" / "build-app.py"


def _load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_app", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder() -> ModuleType:
    return _load_builder()


@pytest.fixture
def fake_msgx(tmp_path: Path) -> Path:
    """Ein ausfuehrbarer Platzhalter - gebaut wird gegen einen Pfad, nicht gegen msgx."""
    path = tmp_path / "bin" / "msgx"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Struktur
# ---------------------------------------------------------------------------


def test_bundle_has_the_layout_macos_expects(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1.2.3", quiet=True)

    assert bundle.name.endswith(".app")
    launcher = bundle / "Contents" / "MacOS" / builder.APP_NAME
    start = bundle / "Contents" / "Resources" / "start.command"
    assert launcher.is_file()
    assert start.is_file()
    assert os.access(launcher, os.X_OK), "der Starter muss ausfuehrbar sein"
    assert os.access(start, os.X_OK)
    assert (bundle / "Contents" / "PkgInfo").read_text() == "APPL????"


def test_info_plist_is_readable_and_points_at_the_launcher(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1.2.3", quiet=True)

    plist = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())

    assert plist["CFBundleExecutable"] == builder.APP_NAME
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleShortVersionString"] == "1.2.3"
    assert plist["CFBundleIdentifier"] == builder.BUNDLE_ID
    # Kein Netzzugriff, also auch keine Ausnahme dafuer.
    assert plist["NSAppTransportSecurity"]["NSAllowsArbitraryLoads"] is False


def test_rebuilding_replaces_the_previous_bundle(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    """Sonst bleiben Reste einer alten Fassung im Bundle liegen."""
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)
    fremd = bundle / "Contents" / "Resources" / "uebrig.txt"
    fremd.write_text("alt", encoding="utf-8")

    builder.build_bundle(tmp_path, fake_msgx, "2", quiet=True)

    assert not fremd.exists()


# ---------------------------------------------------------------------------
# Der eingesetzte Pfad
# ---------------------------------------------------------------------------


def test_the_msgx_path_is_baked_in_and_quoted(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)

    start = (bundle / "Contents" / "Resources" / "start.command").read_text()

    assert f'MSGX="{fake_msgx}"' in start
    assert "guide" in start


@pytest.mark.parametrize(
    "verzeichnis",
    ["mit leerzeichen", 'mit"anfuehrung', "mit$dollar", "mit'apostroph"],
)
def test_an_awkward_path_survives_the_shell(
    builder: ModuleType, tmp_path: Path, verzeichnis: str
) -> None:
    """Ein Pfad mit Sonderzeichen darf das Startskript nicht zerlegen.

    Geprueft wird nicht der Text, sondern das Ergebnis: die Shell muss aus dem
    Skript denselben Pfad herauslesen, der eingesetzt wurde.
    """
    msgx = tmp_path / verzeichnis / "msgx"
    msgx.parent.mkdir(parents=True)
    msgx.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    msgx.chmod(0o755)

    bundle = builder.build_bundle(tmp_path / "ziel", msgx, "1", quiet=True)
    start = bundle / "Contents" / "Resources" / "start.command"

    # Nur die Zuweisung ausfuehren und ausgeben lassen.
    zuweisung = next(
        line for line in start.read_text().splitlines() if line.startswith("MSGX=")
    )
    result = subprocess.run(
        ["/bin/sh", "-c", f'{zuweisung}\nprintf "%s" "$MSGX"'],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == str(msgx)


def test_a_moved_environment_is_reported_not_ignored(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    """Verschwindet msgx, muss der Start es sagen - nicht lautlos nichts tun."""
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)
    fake_msgx.unlink()
    start = bundle / "Contents" / "Resources" / "start.command"

    result = subprocess.run(
        ["/bin/sh", str(start)], capture_output=True, text=True, input="\n", check=False
    )

    assert result.returncode == 1
    assert "liegt nicht mehr" in result.stdout
    assert "build-app.py" in result.stdout


# ---------------------------------------------------------------------------
# Sicherheitsmodell
# ---------------------------------------------------------------------------


def test_the_bundle_never_passes_a_password_and_never_reaches_the_network(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    """Das Bundle ist ein Starter.

    Verboten sind Konstrukte, die ein Passwort *weitergeben* oder etwas
    abrufen - nicht das Wort selbst. Der Starter erklaert in einem Kommentar,
    warum er ein Terminal oeffnet, und dabei muss er es nennen duerfen.
    """
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)

    verboten = (
        "--password",       # die Option, die es nicht gibt
        "--passwort",
        "password=",        # als Variable weitergereicht
        "passwort=",
        "read -s",          # still eingelesen und dann uebergeben
        "find-generic-password",   # aus dem Schluesselbund geholt
        "osascript",        # ein Dialog, der es irgendwohin schreiben muesste
        "curl",
        "wget",
        "nc ",
        "ftp",
    )
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.suffix == ".icns":
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for muster in verboten:
            assert muster not in text, f"{path.name} enthaelt {muster!r}"
        # Eine echte URL waere ein Abruf. Die DOCTYPE-Zeile eines Plists nennt
        # Apples DTD als Kennung des Dateiformats, nicht als Adresse.
        urls = [
            zeile
            for zeile in text.splitlines()
            if ("http://" in zeile or "https://" in zeile)
            and "<!doctype plist" not in zeile
        ]
        assert not urls, f"{path.name} nennt eine URL: {urls}"


def test_the_launcher_opens_a_terminal(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    """Ohne Terminal koennte getpass nicht fragen - das ist der ganze Punkt."""
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)

    launcher = (bundle / "Contents" / "MacOS" / builder.APP_NAME).read_text()

    assert "Terminal" in launcher
    assert "start.command" in launcher


# ---------------------------------------------------------------------------
# Das Symbol
# ---------------------------------------------------------------------------


def test_draw_mark_produces_a_valid_png(builder: ModuleType) -> None:
    """Der PNG-Schreiber ist selbst gebaut, also wird er geprueft."""
    data = builder.draw_mark(32).to_png()

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (32, 32)
    # Ein Bild aus einer einzigen Farbe waere ein Fehler im Rasterer.
    canvas = builder.draw_mark(32)
    farben = {bytes(canvas.pixels[i : i + 4]) for i in range(0, len(canvas.pixels), 4)}
    assert len(farben) > 2, "Block, Aussparung und Transparenz muessen vorkommen"


def test_the_iconset_covers_every_size_icns_needs(
    builder: ModuleType, tmp_path: Path
) -> None:
    iconset = builder.write_iconset(tmp_path)

    namen = {p.name for p in iconset.iterdir()}
    for points, scale in builder.ICON_VARIANTS:
        suffix = "@2x" if scale == 2 else ""
        assert f"icon_{points}x{points}{suffix}.png" in namen


@pytest.mark.skipif(shutil.which("iconutil") is None, reason="iconutil nur auf macOS")
def test_iconutil_accepts_the_generated_iconset(
    builder: ModuleType, tmp_path: Path, fake_msgx: Path
) -> None:
    """Der Nachweis, dass die selbst geschriebenen PNGs gueltig sind."""
    bundle = builder.build_bundle(tmp_path, fake_msgx, "1", quiet=True)

    icns = bundle / "Contents" / "Resources" / "AppIcon.icns"
    assert icns.is_file(), "iconutil hat das Symbol nicht erzeugt"
    assert icns.stat().st_size > 1000
    assert "CFBundleIconFile" in plistlib.loads(
        (bundle / "Contents" / "Info.plist").read_bytes()
    )
