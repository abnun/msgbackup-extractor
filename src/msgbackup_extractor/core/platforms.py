"""Plattformabhaengiges an einer Stelle.

Der Kern dieses Programms ist plattformunabhaengig: SQLite, plistlib, hashlib,
`cryptography` und das Formatparsing laufen ueberall, wo Python laeuft. An
genau vier Stellen ist das Betriebssystem relevant, und die stehen hier:

1. **Wo iTunes bzw. die Apple-Geraete-App ihre Backups ablegt.**
2. **Welche Verzeichnisse in die Cloud synchronisiert werden**, damit der
   Export nicht versehentlich hochgeladen wird.
3. **Wie man die Zwischenablage in eine Pipe bekommt**, fuer die Uebergabe der
   Auswahl aus der lokalen Ansicht.
4. **Was man tun muss, wenn das Backup nicht lesbar ist.**

Ehrlichkeitshinweis: entwickelt und geprueft wurde auf macOS. Die
Windows-Pfade stammen aus Apples Dokumentation und dem ueblichen Verhalten der
Apple-Geraete-App, nicht aus einem Testlauf auf einem Windows-Rechner. Wenn ein
Backup dort nicht gefunden wird, hilft `--backup` mit dem vollen Pfad; der
restliche Ablauf ist davon unabhaengig.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class BackupLocation:
    """Ein Ort, an dem Backups liegen koennen."""

    #: Pfad relativ zum Heimatverzeichnis, oder absolut.
    path: Path
    #: Woher er kommt, fuer die Fehlermeldung.
    source: str


#: macOS: der Finder legt Backups immer hier ab.
_MACOS_ROOTS: Final = (
    ("Library/Application Support/MobileSync/Backup", "Finder"),
)

#: Windows: iTunes und die Apple-Geraete-App aus dem Microsoft Store legen
#: Backups an unterschiedlichen Orten ab. Beide werden gesucht, weil beide
#: verbreitet sind.
#:
#: Zwei verschiedene Bezugspunkte, und das ist wichtig: die iTunes-Pfade haengen
#: an %APPDATA%, der Pfad der Geraete-App am Benutzerprofil. %APPDATA% ist NICHT
#: zwingend <Profil>\AppData\Roaming - in einem Roaming- oder Domaenenprofil
#: kann es umgeleitet sein. Wer es ableitet statt es zu lesen, sucht dort am
#: falschen Ort und meldet dann "kein Backup gefunden".
_WINDOWS_APPDATA_ROOTS: Final = (
    ("Apple Computer/MobileSync/Backup", "iTunes"),
    ("Apple/MobileSync/Backup", "Apple-Geraete-App"),
)
_WINDOWS_PROFILE_ROOTS: Final = (
    ("Apple/MobileSync/Backup", "Apple-Geraete-App"),
)

#: Auf anderen Systemen gibt es keinen offiziellen Ort. Ein per libimobiledevice
#: erzeugtes oder kopiertes Backup wird ueber --backup angegeben.
_OTHER_ROOTS: Final = ()

#: Verzeichnisse unterhalb von $HOME, die als synchronisiert gelten.
_MACOS_CLOUD: Final = (
    ("Library/Mobile Documents", "iCloud Drive"),
    ("Library/CloudStorage", "macOS Cloud Storage (Provider-Mount)"),
)
_WINDOWS_CLOUD: Final = (
    ("iCloudDrive", "iCloud Drive"),
    ("iCloud Drive", "iCloud Drive"),
    ("OneDrive", "Microsoft OneDrive"),
)

#: Auf allen Systemen gleich benannt.
_COMMON_CLOUD: Final = (
    ("Dropbox", "Dropbox"),
    ("OneDrive", "Microsoft OneDrive"),
    ("Google Drive", "Google Drive"),
    ("pCloud Drive", "pCloud"),
    ("Sync", "Sync.com"),
    ("Nextcloud", "Nextcloud"),
    ("ownCloud", "ownCloud"),
    ("Seafile", "Seafile"),
    ("MEGA", "MEGA"),
)


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def backup_locations(
    home: Path | None = None, appdata: Path | None = None
) -> tuple[BackupLocation, ...]:
    """Alle Orte, an denen auf diesem System Backups liegen koennen.

    `appdata` ist der Wert von %APPDATA%. Fehlt er, wird der ueblich Ort unter
    dem Profil angenommen - das ist eine Annahme und deshalb nur der Rueckfall,
    nicht der Regelfall.
    """
    base = home or Path.home()
    if is_macos():
        entries = _MACOS_ROOTS
    elif is_windows():
        roaming = appdata
        if roaming is None:
            aus_umgebung = os.environ.get("APPDATA")
            roaming = Path(aus_umgebung) if aus_umgebung else base / "AppData" / "Roaming"
        gefunden = [
            BackupLocation(path=roaming / relative, source=source)
            for relative, source in _WINDOWS_APPDATA_ROOTS
        ]
        gefunden += [
            BackupLocation(path=base / relative, source=source)
            for relative, source in _WINDOWS_PROFILE_ROOTS
        ]
        # Gleiche Pfade koennen doppelt auftreten, wenn %APPDATA% der uebliche
        # Ort ist. Reihenfolge erhalten, Duplikate entfernen.
        gesehen: set[Path] = set()
        eindeutig: list[BackupLocation] = []
        for eintrag in gefunden:
            if eintrag.path not in gesehen:
                gesehen.add(eintrag.path)
                eindeutig.append(eintrag)
        return tuple(eindeutig)
    else:
        entries = _OTHER_ROOTS
    return tuple(
        BackupLocation(path=base / relative, source=source) for relative, source in entries
    )


def cloud_markers() -> tuple[tuple[str, str], ...]:
    """Verzeichnisse, die als von einem Cloud-Dienst synchronisiert gelten."""
    if is_macos():
        specific = _MACOS_CLOUD
    elif is_windows():
        specific = _WINDOWS_CLOUD
    else:
        specific = ()
    # Reihenfolge: erst die plattformeigenen, dann die gemeinsamen; Duplikate
    # schaden nicht, weil die Pruefung beim ersten Treffer endet.
    return tuple(specific) + _COMMON_CLOUD


def clipboard_command() -> str:
    """Befehl, der die Zwischenablage auf die Standardausgabe schreibt.

    Wird nur in Beispieltexten verwendet - das Programm selbst liest die
    Zwischenablage nie.
    """
    if is_macos():
        return "pbpaste"
    if is_windows():
        return "powershell -Command Get-Clipboard"
    return "xclip -selection clipboard -o"


def open_command() -> tuple[str, ...]:
    """Befehl, der eine Datei mit dem Standardprogramm des Systems oeffnet.

    Wird nur benutzt, um am Ende die erzeugte Ansicht anzuzeigen, und nur wenn
    der Anwender danach gefragt wurde. Es ist ein lokaler Aufruf; ein
    Netzzugriff findet dabei nicht statt.
    """
    if is_macos():
        return ("open",)
    if is_windows():
        # cmd /c start braucht ein leeres Fensterargument, sonst wird der Pfad
        # als Fenstertitel verstanden.
        return ("cmd", "/c", "start", "")
    return ("xdg-open",)


def command_line_budget() -> int:
    """Wie viele Zeichen ein Befehl haben darf, damit er sicher noch geht.

    Nicht das dokumentierte Maximum, sondern ein Vorrat darunter: unter macOS
    und Linux zaehlt die Umgebung mit ins `ARG_MAX`, und cmd.exe bricht bei
    8191 Zeichen ab. Wer knapp unter der Grenze plant, baut einen Befehl, der
    auf einem anderen Rechner platzt.

    Wird gebraucht, um zu entscheiden, ob die ausgewaehlten Pfade direkt in den
    Befehl passen oder ob es die Liste ueber die Standardeingabe braucht.
    """
    if is_windows():
        return 7000
    return 100_000


def permission_hint(path: Path) -> str:
    """Was zu tun ist, wenn ein Backup-Verzeichnis nicht lesbar ist."""
    if is_macos():
        return (
            f"Kein Leserecht auf {path.name}. Unter macOS braucht das Terminal dafuer "
            '"Festplattenvollzugriff": Systemeinstellungen > Datenschutz & Sicherheit > '
            "Festplattenvollzugriff, dort das Terminal hinzufuegen und neu starten."
        )
    if is_windows():
        return (
            f"Kein Leserecht auf {path.name}. Unter Windows liegen die Backups im "
            "Benutzerprofil und sind normalerweise lesbar. Fehlt das Recht, hilft ein "
            "Blick in die Ordnereigenschaften unter Sicherheit, oder eine Kopie des "
            "Backup-Verzeichnisses an einen frei zugaenglichen Ort."
        )
    return (
        f"Kein Leserecht auf {path.name}. Bitte die Dateirechte des Verzeichnisses "
        "pruefen."
    )


def platform_name() -> str:
    """Anzeigename des Systems, fuer Berichte und Fehlermeldungen."""
    if is_macos():
        return "macOS"
    if is_windows():
        return "Windows"
    return sys.platform
