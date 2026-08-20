#!/usr/bin/env python3
"""Baut einen doppelklickbaren Starter fuer `msgx guide`.

    scripts/build-app.py                     an den Standardort
    scripts/build-app.py --into ~/Desktop
    scripts/build-app.py --msgx /pfad/zu/msgx

Was entsteht, haengt am System:

| System  | Ergebnis                        | Standardort      |
|---------|---------------------------------|------------------|
| macOS   | `msgbackup-extractor.app`       | `~/Applications` |
| Windows | `msgbackup-extractor.cmd`       | Desktop          |
| sonst   | nichts, mit Begruendung         | -                |

Auf anderen Systemen wird **abgelehnt** statt etwas Unbrauchbares angelegt: ein
`.app`-Verzeichnis auf Windows startet nichts, und ein stiller Fehlschlag ist
schlimmer als eine klare Absage.

Der Windows-Starter ist **ungeprueft**, wie die Windows-Unterstuetzung
insgesamt. Erzeugt wird er aus derselben Logik, gelaufen ist er dort nie.

Was der Starter ist und was nicht:

* Es ist ein **Starter**, keine zweite Anwendung. Es oeffnet ein Terminal und
  ruft darin `msgx guide` auf. Die eigentliche Arbeit macht dieselbe
  Kommandozeile wie immer.
* Es **muss** ein Terminal oeffnen. Ein verschluesseltes Backup braucht ein
  Passwort, und das wird ausschliesslich eingetippt - nie als Argument, nie
  ueber ein Fenster, das es irgendwo hinschreiben muesste. Eine stille
  Oberflaeche waere hier ein Rueckschritt und kein Fortschritt.
* Es wird **hier gebaut**, aus Bordmitteln. Nichts wird heruntergeladen, kein
  Fremdwerkzeug installiert. Das Icon entsteht als PNG in reinem Python, und
  `iconutil` aus macOS macht daraus ein `.icns`. Fehlt `iconutil`, entsteht das
  Bundle trotzdem, nur ohne eigenes Symbol.

Der Pfad zu `msgx` wird beim Bauen fest eingesetzt, weil ein Bundle nicht
wissen kann, welche Umgebung gemeint ist. Wird die Umgebung spaeter verschoben,
sagt der Starter das beim Start deutlich, statt lautlos nichts zu tun.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Final

APP_NAME: Final = "msgbackup-extractor"
BUNDLE_ID: Final = "de.abnun.msgbackup-extractor"

INK: Final = (0x15, 0x12, 0x0E, 0xFF)
CARVED: Final = (0xF2, 0xE8, 0xD6, 0xFF)
CLEAR: Final = (0, 0, 0, 0)

#: Kantenlaengen, die ein .icns erwartet, als (Punkte, Skalierung).
ICON_VARIANTS: Final = tuple(
    (points, scale) for points in (16, 32, 128, 256, 512) for scale in (1, 2)
)

#: Vierfach zeichnen und dann mitteln - das ist die Kantenglaettung.
SUPERSAMPLE: Final = 4


# ---------------------------------------------------------------------------
# Bild: ein winziger Rasterer und ein PNG-Schreiber, beide aus der
# Standardbibliothek. Kein Bildwerkzeug, keine Abhaengigkeit.
# ---------------------------------------------------------------------------


class Canvas:
    """RGBA-Raster mit genau den Formen, die das Symbol braucht."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = bytearray(size * size * 4)

    def _put(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            offset = (y * self.size + x) * 4
            self.pixels[offset : offset + 4] = bytes(color)

    def rounded_rect(
        self, x0: int, y0: int, x1: int, y1: int, radius: int, color
    ) -> None:
        for y in range(y0, y1):
            for x in range(x0, x1):
                dx = min(x - x0, x1 - 1 - x)
                dy = min(y - y0, y1 - 1 - y)
                if dx < radius and dy < radius:
                    ex, ey = radius - dx, radius - dy
                    if ex * ex + ey * ey > radius * radius:
                        continue
                self._put(x, y, color)

    def polygon(self, points, color) -> None:
        """Scanline-Fuellung, ungerade Kreuzungszahl gehoert zur Flaeche."""
        ys = [p[1] for p in points]
        for y in range(int(min(ys)), int(max(ys)) + 1):
            crossings: list[float] = []
            for i in range(len(points)):
                (ax, ay), (bx, by) = points[i], points[(i + 1) % len(points)]
                if (ay <= y < by) or (by <= y < ay):
                    crossings.append(ax + (y - ay) * (bx - ax) / (by - ay))
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                for x in range(int(crossings[i]), int(crossings[i + 1]) + 1):
                    self._put(x, y, color)

    def disc(self, cx: float, cy: float, r: float, color) -> None:
        for y in range(int(cy - r), int(cy + r) + 1):
            for x in range(int(cx - r), int(cx + r) + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self._put(x, y, color)

    def downsample(self, factor: int) -> Canvas:
        out = Canvas(self.size // factor)
        area = factor * factor
        for y in range(out.size):
            for x in range(out.size):
                sums = [0, 0, 0, 0]
                for sy in range(factor):
                    row = (y * factor + sy) * self.size
                    for sx in range(factor):
                        offset = (row + x * factor + sx) * 4
                        for c in range(4):
                            sums[c] += self.pixels[offset + c]
                out._put(x, y, tuple(s // area for s in sums))
        return out

    def to_png(self) -> bytes:
        raw = bytearray()
        for y in range(self.size):
            raw.append(0)  # Filtertyp 0: keine Vorhersage
            start = y * self.size * 4
            raw += self.pixels[start : start + self.size * 4]

        def chunk(kind: bytes, payload: bytes) -> bytes:
            body = kind + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(
                ">I", zlib.crc32(body) & 0xFFFFFFFF
            )

        header = struct.pack(">2I5B", self.size, self.size, 8, 6, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b"")
        )


def draw_mark(size: int) -> Canvas:
    """Das Zeichen der Startseite: ein Block, aus dem ein Bild geschnitten ist."""
    big = size * SUPERSAMPLE
    canvas = Canvas(big)
    inset = round(big * 0.055)
    radius = round(big * 0.225)
    canvas.rounded_rect(inset, inset, big - inset, big - inset, radius, INK)

    # Berge und Sonne, ausgespart wie im Reliefdruck.
    def px(fx: float, fy: float) -> tuple[float, float]:
        return (inset + fx * (big - 2 * inset), inset + fy * (big - 2 * inset))

    canvas.polygon(
        [px(0.12, 0.80), px(0.38, 0.40), px(0.55, 0.62), px(0.68, 0.44), px(0.88, 0.80)],
        CARVED,
    )
    cx, cy = px(0.71, 0.26)
    canvas.disc(cx, cy, (big - 2 * inset) * 0.105, CARVED)
    return canvas.downsample(SUPERSAMPLE)


def write_iconset(directory: Path) -> Path:
    """Legt die PNGs an, die `iconutil` erwartet."""
    iconset = directory / f"{APP_NAME}.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for points, scale in ICON_VARIANTS:
        pixels = points * scale
        name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
        (iconset / name).write_bytes(draw_mark(pixels).to_png())
    return iconset


def build_icns(iconset: Path, target: Path) -> bool:
    """Erzeugt das .icns. Ohne `iconutil` entsteht das Bundle ohne Symbol."""
    if shutil.which("iconutil") is None:
        return False
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(target)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and target.exists()


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

LAUNCHER: Final = """#!/bin/sh
# Oeffnet ein Terminal und laesst darin den gefuehrten Ablauf laufen. Ein
# Terminal ist keine Bequemlichkeitsfrage: das Passwort eines verschluesselten
# Backups wird eingetippt, nie uebergeben.
here=$(cd -- "$(dirname -- "$0")" && pwd)
exec open -a Terminal "$here/../Resources/start.command"
"""

START: Final = """#!/bin/sh
# Beim Bauen eingesetzter Pfad. Wird die Umgebung verschoben, sagt das der
# naechste Start - statt lautlos nichts zu tun.
MSGX={msgx}

printf '\\033]0;msgbackup-extractor\\007'

if [ ! -x "$MSGX" ]; then
    echo "msgbackup-extractor"
    echo
    echo "Das Programm liegt nicht mehr unter:"
    echo "    $MSGX"
    echo
    echo "Die Umgebung wurde verschoben oder geloescht. Das Bundle laesst sich"
    echo "mit dem richtigen Pfad neu bauen:"
    echo
    echo "    scripts/build-app.py --msgx /pfad/zu/msgx"
    echo
    printf 'Zum Schliessen die Eingabetaste druecken. '
    read -r _
    exit 1
fi

"$MSGX" guide
status=$?

echo
if [ "$status" -eq 0 ]; then
    printf 'Fertig. Zum Schliessen die Eingabetaste druecken. '
else
    printf 'Beendet mit Code %s. Zum Schliessen die Eingabetaste druecken. ' "$status"
fi
read -r _
exit "$status"
"""


def info_plist(version: str, with_icon: bool) -> dict[str, object]:
    plist: dict[str, object] = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": "Copyright 2026 Markus Mueller. Apache-2.0.",
        # Kein Netzzugriff, also auch keine Ausnahme dafuer. Der Eintrag steht
        # hier, damit die Abwesenheit sichtbar ist.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    }
    if with_icon:
        plist["CFBundleIconFile"] = "AppIcon"
    return plist


def build_bundle(into: Path, msgx: Path, version: str, *, quiet: bool = False) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    bundle = into / f"{APP_NAME}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    launcher = macos / APP_NAME
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)

    start = resources / "start.command"
    # shlex.quote waere hier falsch: der Pfad steht in doppelten
    # Anfuehrungszeichen im Skript, also nur die dort wirksamen Zeichen schuetzen.
    escaped = str(msgx).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    start.write_text(START.format(msgx=f'"{escaped}"'), encoding="utf-8")
    start.chmod(0o755)

    icon_ok = False
    iconset = write_iconset(resources)
    try:
        icon_ok = build_icns(iconset, resources / "AppIcon.icns")
    finally:
        shutil.rmtree(iconset, ignore_errors=True)

    (bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(info_plist(version, icon_ok))
    )
    (bundle / "Contents" / "PkgInfo").write_text("APPL????", encoding="ascii")

    if not quiet:
        print(f"Gebaut: {bundle}")
        print(f"Startet: {msgx} guide")
        if not icon_ok:
            print("Hinweis: ohne eigenes Symbol (iconutil nicht verfuegbar).")
    return bundle


# Ein Doppelklick auf eine .cmd oeffnet ein Konsolenfenster, und damit kann
# getpass fragen - dasselbe Prinzip wie das Terminal auf macOS. `pause` haelt
# das Fenster offen, sonst schliesst es sich mit der letzten Zeile.
WINDOWS_STARTER: Final = """@echo off
setlocal
title msgbackup-extractor

set "MSGX={msgx}"

if not exist "%MSGX%" (
    echo msgbackup-extractor
    echo.
    echo Das Programm liegt nicht mehr unter:
    echo     %MSGX%
    echo.
    echo Die Umgebung wurde verschoben oder geloescht. Der Starter laesst sich
    echo mit dem richtigen Pfad neu bauen:
    echo.
    echo     python scripts\\build-app.py --msgx PFAD\\ZU\\msgx.exe
    echo.
    pause
    exit /b 1
)

"%MSGX%" guide
set "status=%ERRORLEVEL%"

echo.
if "%status%"=="0" (
    echo Fertig.
) else (
    echo Beendet mit Code %status%.
)
pause
exit /b %status%
"""


def build_windows_starter(
    into: Path, msgx: Path, version: str, *, quiet: bool = False
) -> Path:
    """Schreibt den doppelklickbaren Starter fuer Windows.

    `version` wird nicht eingesetzt - eine .cmd hat keine Metadaten. Der
    Parameter bleibt, damit die Weiche beide Bauer gleich aufrufen kann.
    """
    del version
    into.mkdir(parents=True, exist_ok=True)
    starter = into / f"{APP_NAME}.cmd"
    # In einer Batchdatei ist `%` das Fluchtzeichen fuer sich selbst. Doppelte
    # Anfuehrungszeichen sind in Windows-Pfaden nicht erlaubt, also kein Thema.
    starter.write_text(
        WINDOWS_STARTER.format(msgx=str(msgx).replace("%", "%%")),
        encoding="utf-8",
        newline="\r\n",
    )
    if not quiet:
        print(f"Gebaut: {starter}")
        print(f"Startet: {msgx} guide")
        print("Ungeprueft: dieser Starter ist nie auf Windows gelaufen.")
    return starter


def candidate_paths(
    home: Path | None = None,
    repo: Path | None = None,
    interpreter_dir: Path | None = None,
):
    """Orte, an denen `msgx` liegen kann, in der Reihenfolge der Verlaesslichkeit.

    Der haeufigste Fall ist, dass dieses Skript mit dem System-Python
    aufgerufen wird - dann liegt `msgx` nicht neben dem laufenden Python, und
    ein Alias in der Shell taucht im PATH nicht auf. Deshalb wird auch dort
    gesucht, wo die Anleitung die Umgebung anlegt.
    """
    base = home or Path.home()
    wurzel = repo or Path(__file__).resolve().parent.parent
    namen = ("msgx", "msgx.exe")
    # Einsetzbar, damit ein Test nicht die Umgebung findet, in der er laeuft.
    hier = interpreter_dir or Path(sys.executable).parent
    for name in namen:
        yield hier / name                     # dasselbe venv wie dieses Python
        yield hier / "Scripts" / name         # Windows-Layout
    for verzeichnis in (wurzel / ".venv", *sorted((base / ".venvs").glob("*"))):
        for unter in ("bin", "Scripts"):
            for name in namen:
                yield verzeichnis / unter / name


def works(candidate: Path) -> bool:
    """Laeuft dieses msgx ueberhaupt?

    Vorhanden und ausfuehrbar genuegt nicht. Eine virtuelle Umgebung in iCloud
    Drive ist beides und funktioniert trotzdem nicht - iCloud versteckt die
    `.pth`-Dateien und Python ueberspringt sie stillschweigend. Ein Buendel, das
    auf so eine Umgebung zeigt, scheitert erst beim Doppelklick, und dann sieht
    es aus wie ein Fehler des Programms.
    """
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        return False
    try:
        result = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_msgx(
    home: Path | None = None,
    repo: Path | None = None,
    interpreter_dir: Path | None = None,
    *,
    search_path: bool = True,
) -> tuple[Path | None, list[Path]]:
    """Liefert das erste msgx, das laeuft - und alles, was geprueft wurde."""
    geprueft: list[Path] = []
    for candidate in candidate_paths(home, repo, interpreter_dir):
        if candidate in geprueft or not candidate.is_file():
            continue
        geprueft.append(candidate)
        if works(candidate):
            return candidate, geprueft
    found = shutil.which("msgx") if search_path else None
    if found:
        gefunden = Path(found)
        geprueft.append(gefunden)
        if works(gefunden):
            return gefunden, geprueft
    return None, geprueft


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="Zielverzeichnis (Standard: ~/Applications bzw. Desktop)",
    )
    parser.add_argument(
        "--msgx",
        type=Path,
        default=None,
        help="Pfad zu msgx, der fest eingesetzt wird (Standard: automatisch)",
    )
    arguments = parser.parse_args(argv)

    if sys.platform == "darwin":
        bauen, standardort = build_bundle, Path.home() / "Applications"
    elif sys.platform.startswith("win"):
        bauen, standardort = build_windows_starter, Path.home() / "Desktop"
    else:
        print(
            f"Fuer {sys.platform} gibt es keinen Doppelklick-Starter, und es wird\n"
            "auch keiner erfunden. Der gefuehrte Ablauf laeuft dort direkt:\n"
            "    msgx guide",
            file=sys.stderr,
        )
        return 2

    if arguments.msgx is not None:
        msgx = arguments.msgx.expanduser().resolve()
        if not works(msgx):
            print(
                f"Dieses msgx laeuft nicht: {msgx}\n"
                "Pruefen mit:  " + str(msgx) + " --version",
                file=sys.stderr,
            )
            return 2
    else:
        gefunden, geprueft = find_msgx()
        if gefunden is None:
            print("Kein funktionierendes msgx gefunden.", file=sys.stderr)
            if geprueft:
                print("\nGeprueft und verworfen:", file=sys.stderr)
                for kandidat in geprueft:
                    print(f"    {kandidat}", file=sys.stderr)
                print(
                    "\nEine virtuelle Umgebung in iCloud Drive ist vorhanden und\n"
                    "trotzdem unbrauchbar - iCloud versteckt die .pth-Dateien.\n"
                    "Siehe README, Abschnitt Installation.",
                    file=sys.stderr,
                )
            print(
                "\nOder den Pfad direkt angeben:\n"
                "    scripts/build-app.py --msgx ~/.venvs/msgbackup-extractor/bin/msgx",
                file=sys.stderr,
            )
            return 2
        msgx = gefunden.resolve()

    try:
        from msgbackup_extractor import __version__ as version
    except ImportError:
        version = "0"

    into = (arguments.into or standardort).expanduser()
    into.mkdir(parents=True, exist_ok=True)
    bauen(into, msgx, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
