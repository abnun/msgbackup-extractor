"""Zugriff auf ein lokales Apple-iPhone-Backup.

Dieses Modul ist der einzige Ort, der den Backup-Pfad besitzt. Alle Lesezugriffe
laufen hierdurch, und zwar ausschliesslich mit `open(..., "rb")`. Geschrieben
wird hier nichts - das Modul hat keine Schreibfunktion.

Wo Backups liegen und was bei fehlenden Rechten zu tun ist, unterscheidet sich
je Betriebssystem. Beides kommt aus `core/platforms.py`, damit es genau eine
Stelle dafuer gibt. Ein `PermissionError` wird in eine erklaerende Meldung
uebersetzt statt rohe Fehler durchzulassen.

Der Zugriff auf ein Backup ueber `--backup` funktioniert unabhaengig davon auf
jedem System: dafuer braucht es nur ein lesbares Verzeichnis.
"""

from __future__ import annotations

import plistlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from msgbackup_extractor.core import platforms
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.models import ApplicationInfo, BackupInfo, DeviceInfo

logger = get_logger("backup")

INFO_PLIST: Final = "Info.plist"
MANIFEST_PLIST: Final = "Manifest.plist"
MANIFEST_DB: Final = "Manifest.db"
STATUS_PLIST: Final = "Status.plist"

#: Ohne diese Dateien ist es kein verwertbares Backup.
REQUIRED_FILES: Final = (MANIFEST_PLIST, MANIFEST_DB)


class NotABackupError(ValueError):
    """Das Verzeichnis ist kein (vollstaendiges) Apple-Backup."""


class BackupAccessError(RuntimeError):
    """Das Backup ist vorhanden, kann aber nicht gelesen werden."""


def default_backup_roots(home: Path | None = None) -> tuple[Path, ...]:
    """Alle Orte, an denen dieses System Backups ablegt.

    Auf macOS ist das einer, auf Windows koennen es mehrere sein, weil iTunes
    und die Apple-Geraete-App unterschiedliche Verzeichnisse verwenden.
    """
    return tuple(location.path for location in platforms.backup_locations(home))


def default_backup_root(home: Path | None = None) -> Path:
    """Der erste Suchort. Fuer Meldungen, in denen ein einzelner Pfad genuegt.

    Kennt dieses System keinen Standardort, wird ein Pfad unterhalb von $HOME
    zurueckgegeben, damit die Meldung nicht leer bleibt.
    """
    roots = default_backup_roots(home)
    if roots:
        return roots[0]
    return (home or Path.home()) / "MobileSync" / "Backup"


def list_local_backups(root: Path | None = None) -> tuple[Path, ...]:
    """Alle Backup-Verzeichnisse an den Standardorten, neueste zuerst.

    Hilft beim Auffinden des Backups, ohne dass die Geraete-ID bekannt sein
    muss. Verzeichnisse ohne Manifest.plist werden uebergangen.

    Args:
        root: Wenn gesetzt, wird nur dort gesucht. Ohne Angabe alle Orte, die
            dieses System kennt.
    """
    directories = [root] if root is not None else list(default_backup_roots())
    found: list[Path] = []
    denied: Path | None = None

    for directory in directories:
        try:
            candidates = [p for p in directory.iterdir() if p.is_dir()]
        except PermissionError:
            # Erst merken: ein zweiter Ort kann trotzdem lesbar sein.
            denied = denied or directory
            continue
        except OSError:
            continue
        found.extend(p for p in candidates if (p / MANIFEST_PLIST).is_file())

    if not found and denied is not None:
        raise BackupAccessError(_permission_hint(denied))
    return tuple(sorted(found, key=lambda p: p.stat().st_mtime, reverse=True))


def _permission_hint(path: Path) -> str:
    return platforms.permission_hint(path)


# ---------------------------------------------------------------------------
# Plist-Hilfen
# ---------------------------------------------------------------------------


def _read_plist(path: Path) -> dict[str, Any]:
    """Liest ein Plist strikt lesend. Fehlt es, ist das Ergebnis leer."""
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except FileNotFoundError:
        return {}
    except PermissionError as error:
        raise BackupAccessError(_permission_hint(path)) from error
    except Exception as error:
        logger.warning("%s ist nicht lesbar: %s", path.name, type(error).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


# ---------------------------------------------------------------------------
# AppleBackup
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EncryptionState:
    """Was ueber die Verschluesselung des Backups bekannt ist."""

    is_encrypted: bool
    keybag: bytes | None = None
    manifest_key: bytes | None = None

    @property
    def manifest_is_encrypted(self) -> bool:
        return self.is_encrypted and self.manifest_key is not None


class AppleBackup:
    """Read-only-Sicht auf ein Backup-Verzeichnis.

    Das Objekt liest beim Erzeugen nur die Plists; Manifest.db wird erst
    angefasst, wenn sie gebraucht wird (und bei verschluesselten Backups erst
    nach der Entschluesselung).
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        if not self.path.is_dir():
            raise NotABackupError(f"Kein Verzeichnis: {self.path.name}")

        missing = [name for name in REQUIRED_FILES if not (self.path / name).is_file()]
        if missing:
            raise NotABackupError(
                f"{self.path.name} sieht nicht wie ein Apple-Backup aus; es fehlen: "
                f"{', '.join(missing)}. Erwartet wird das Verzeichnis mit der "
                "Geraete-ID unterhalb von MobileSync/Backup."
            )

        self._info = _read_plist(self.path / INFO_PLIST)
        self._manifest = _read_plist(self.path / MANIFEST_PLIST)
        self._status = _read_plist(self.path / STATUS_PLIST)

    # -- Pfade --------------------------------------------------------------

    @property
    def manifest_db_path(self) -> Path:
        return self.path / MANIFEST_DB

    def payload_path(self, file_id: str) -> Path:
        """Ort der Nutzdatei zu einer fileID: `<backup>/<erste 2 Hex>/<fileID>`."""
        return self.path / file_id[:2] / file_id

    def payload_exists(self, file_id: str) -> bool:
        return self.payload_path(file_id).is_file()

    def open_payload(self, file_id: str) -> Any:
        """Oeffnet eine Nutzdatei lesend. Der Aufrufer schliesst sie."""
        return self.payload_path(file_id).open("rb")

    def read_payload(self, file_id: str) -> bytes:
        """Liest eine Nutzdatei vollstaendig. Nur fuer kleine Dateien gedacht."""
        return self.payload_path(file_id).read_bytes()

    # -- Verschluesselung ---------------------------------------------------

    @property
    def encryption(self) -> EncryptionState:
        """Verschluesselungszustand aus Manifest.plist."""
        is_encrypted = bool(self._manifest.get("IsEncrypted", False))
        keybag = self._manifest.get("BackupKeyBag")
        manifest_key = self._manifest.get("ManifestKey")
        return EncryptionState(
            is_encrypted=is_encrypted,
            keybag=keybag if isinstance(keybag, bytes) else None,
            manifest_key=manifest_key if isinstance(manifest_key, bytes) else None,
        )

    @property
    def is_encrypted(self) -> bool:
        return self.encryption.is_encrypted

    # -- Metadaten ----------------------------------------------------------

    @property
    def udid(self) -> str:
        """Geraete-ID. Bevorzugt der Verzeichnisname, wie vom Finder vergeben."""
        return self.path.name

    def device_info(self) -> DeviceInfo:
        info = self._info
        lockdown = self._manifest.get("Lockdown")
        lockdown = lockdown if isinstance(lockdown, dict) else {}

        installed = info.get("Installed Applications")
        bundle_ids = tuple(
            sorted(value for value in installed if isinstance(value, str))
        ) if isinstance(installed, list) else ()

        return DeviceInfo(
            device_name=_as_str(info.get("Device Name")) or _as_str(info.get("Display Name")),
            product_type=_as_str(info.get("Product Type")) or _as_str(lockdown.get("ProductType")),
            product_version=(
                _as_str(info.get("Product Version")) or _as_str(lockdown.get("ProductVersion"))
            ),
            build_version=_as_str(info.get("Build Version")),
            last_backup_date=_as_datetime(info.get("Last Backup Date")),
            installed_applications=bundle_ids,
        )

    def applications(self) -> tuple[ApplicationInfo, ...]:
        """Alle Apps aus Manifest.plist, angereichert um die Info.plist-Bestaetigung.

        Ein Bundle Identifier gilt nur dann als bestaetigt installiert, wenn er
        auch in `Info.plist:Installed Applications` steht. Das ist die Grundlage
        dafuer, dass die App-Erkennung nichts raten muss.
        """
        raw = self._manifest.get("Applications")
        confirmed = set(self.device_info().installed_applications)

        found: dict[str, ApplicationInfo] = {}
        if isinstance(raw, dict):
            for bundle_id, meta in raw.items():
                if not isinstance(bundle_id, str):
                    continue
                version = None
                if isinstance(meta, dict):
                    version = _as_str(meta.get("CFBundleVersion"))
                found[bundle_id] = ApplicationInfo(
                    bundle_id=bundle_id,
                    bundle_version=version,
                    confirmed_installed=bundle_id in confirmed,
                )

        # Apps, die nur in Info.plist stehen, gehen nicht verloren.
        for bundle_id in confirmed - set(found):
            found[bundle_id] = ApplicationInfo(bundle_id=bundle_id, confirmed_installed=True)

        return tuple(found[key] for key in sorted(found))

    def info(self) -> BackupInfo:
        """Alles, was ohne Passwort ueber das Backup bekannt ist."""
        encryption = self.encryption
        is_full = self._status.get("IsFullBackup")
        passcode = self._manifest.get("WasPasscodeSet")

        return BackupInfo(
            path=self.path,
            udid=self.udid,
            is_encrypted=encryption.is_encrypted,
            device=self.device_info(),
            applications=self.applications(),
            manifest_version=_as_str(self._manifest.get("Version")),
            backup_date=(
                _as_datetime(self._manifest.get("Date"))
                or _as_datetime(self._status.get("Date"))
            ),
            was_passcode_set=passcode if isinstance(passcode, bool) else None,
            is_full_backup=is_full if isinstance(is_full, bool) else None,
            has_manifest_key=encryption.manifest_key is not None,
        )

    # -- Diagnose -----------------------------------------------------------

    def present_metadata_files(self) -> tuple[str, ...]:
        """Welche der erwarteten Metadatendateien tatsaechlich vorhanden sind."""
        return tuple(
            name
            for name in (INFO_PLIST, MANIFEST_PLIST, MANIFEST_DB, STATUS_PLIST)
            if (self.path / name).is_file()
        )

    def payload_directories(self) -> Iterator[Path]:
        """Die `00`-`ff`-Unterverzeichnisse, die tatsaechlich existieren."""
        for entry in sorted(self.path.iterdir()):
            if entry.is_dir() and len(entry.name) == 2:
                try:
                    int(entry.name, 16)
                except ValueError:
                    continue
                yield entry

    def __repr__(self) -> str:
        return f"AppleBackup({self.udid}, encrypted={self.is_encrypted})"
