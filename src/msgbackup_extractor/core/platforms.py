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
_WINDOWS_ROOTS: Final = (
    ("AppData/Roaming/Apple Computer/MobileSync/Backup", "iTunes"),
    ("Apple/MobileSync/Backup", "Apple-Geraete-App"),
    ("AppData/Roaming/Apple/MobileSync/Backup", "Apple-Geraete-App"),
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


def backup_locations(home: Path | None = None) -> tuple[BackupLocation, ...]:
    """Alle Orte, an denen auf diesem System Backups liegen koennen."""
    base = home or Path.home()
    if is_macos():
        entries = _MACOS_ROOTS
    elif is_windows():
        entries = _WINDOWS_ROOTS
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
