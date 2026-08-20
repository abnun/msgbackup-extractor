"""Datenmodelle.

Reine Datentraeger ohne Logik und ohne Abhaengigkeit auf andere Module des
Pakets. Alles was hierher gehoert ist beschreibend; Entscheidungen treffen die
Module in `core/`, `apps/` und `extract/`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Medien
# ---------------------------------------------------------------------------


class MediaCategory(enum.StrEnum):
    """Grobkategorie, die die Zielverzeichnisse des Exports bestimmt."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    DATABASE = "database"
    OTHER = "other"

    @property
    def directory(self) -> str:
        """Verzeichnisname unter `media/` im Export."""
        return {
            MediaCategory.IMAGE: "images",
            MediaCategory.VIDEO: "videos",
            MediaCategory.AUDIO: "audio",
            MediaCategory.DOCUMENT: "documents",
            MediaCategory.ARCHIVE: "documents",
            MediaCategory.DATABASE: "databases",
            MediaCategory.OTHER: "other",
        }[self]


class DetectionMethod(enum.StrEnum):
    """Wie ein Medientyp bestimmt wurde, absteigend nach Verlaesslichkeit."""

    MAGIC = "magic"
    MIME = "mime"
    EXTENSION = "extension"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MediaType:
    """Ergebnis der Medienerkennung fuer einen Dateiinhalt."""

    category: MediaCategory
    mime_type: str | None
    extension: str | None
    detection_method: DetectionMethod
    #: True, wenn die Dateiendung dem erkannten Inhalt widerspricht.
    extension_mismatch: bool = False
    #: Kurzbezeichnung des erkannten Formats, z.B. "JPEG", "HEIC", "DOCX".
    format_name: str | None = None


# ---------------------------------------------------------------------------
# Apple-Backup
# ---------------------------------------------------------------------------


class FileKind(enum.IntEnum):
    """Bedeutung von `Files.flags` in der Manifest.db."""

    FILE = 1
    DIRECTORY = 2
    SYMLINK = 4


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Geraetedaten aus Info.plist. Bewusst ohne Seriennummer/IMEI im Bericht."""

    device_name: str | None = None
    product_type: str | None = None
    product_version: str | None = None
    build_version: str | None = None
    last_backup_date: datetime | None = None
    #: Bundle Identifier aller im Backup vermerkten Apps.
    installed_applications: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplicationInfo:
    """Ein Eintrag aus Manifest.plist:Applications."""

    bundle_id: str
    bundle_version: str | None = None
    #: True, wenn der Eintrag auch in Info.plist:Installed Applications steht.
    confirmed_installed: bool = False


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Alles, was ohne Passwort ueber ein Backup bekannt ist."""

    path: Path
    udid: str
    is_encrypted: bool
    device: DeviceInfo
    applications: tuple[ApplicationInfo, ...] = ()
    manifest_version: str | None = None
    backup_date: datetime | None = None
    was_passcode_set: bool | None = None
    is_full_backup: bool | None = None
    #: True, wenn Manifest.plist einen ManifestKey enthaelt (Manifest.db verschluesselt).
    has_manifest_key: bool = False

    def application(self, bundle_id: str) -> ApplicationInfo | None:
        for app in self.applications:
            if app.bundle_id == bundle_id:
                return app
        return None


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Ein Eintrag der Tabelle `Files`, inklusive dekodierter MBFile-Felder.

    Felder, die aus dem NSKeyedArchiver-Blob stammen, sind optional: das Blob
    kann fehlen oder eine unbekannte Struktur haben. Fehlende Werte bleiben
    `None` und werden nie geraten.
    """

    file_id: str
    domain: str
    relative_path: str
    kind: FileKind | None
    size: int | None = None
    protection_class: int | None = None
    encryption_key: bytes | None = None
    mode: int | None = None
    inode: int | None = None
    birth: datetime | None = None
    last_modified: datetime | None = None
    last_status_change: datetime | None = None
    #: Fehlermeldung, falls das MBFile-Blob nicht dekodiert werden konnte.
    decode_error: str | None = None

    @property
    def is_file(self) -> bool:
        return self.kind is FileKind.FILE

    @property
    def is_encrypted(self) -> bool:
        return self.encryption_key is not None

    @property
    def basename(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]

    @property
    def storage_subdirectory(self) -> str:
        """Unterverzeichnis im Backup (erste zwei Hex-Zeichen der fileID)."""
        return self.file_id[:2]


@dataclass(frozen=True, slots=True)
class TableSchema:
    """Introspektionsergebnis fuer eine SQLite-Tabelle."""

    name: str
    columns: tuple[str, ...]
    column_types: dict[str, str] = field(default_factory=dict)
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[tuple[str, str, str], ...] = ()
    row_count: int | None = None

    def has(self, *columns: str) -> bool:
        return all(c in self.columns for c in columns)


# ---------------------------------------------------------------------------
# Messenger-Erkennung
# ---------------------------------------------------------------------------


class DetectionStatus(enum.StrEnum):
    """Ergebnis einer App-Erkennung. `AMBIGUOUS` fuehrt zum Diagnosebericht."""

    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class DomainMatch:
    """Eine Backup-Domain, die einer App zugeordnet wurde."""

    domain: str
    #: "app", "group", "plugin" oder "unknown" - aus dem Domain-Praefix.
    kind: str
    file_count: int = 0
    total_size: int = 0


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Ergebnis von `AppProfile.detect()`.

    `status is CONFIRMED` bedeutet: der Bundle Identifier wurde in den
    Backup-Metadaten tatsaechlich gefunden. Alles andere ist ein Grund fuer
    einen Diagnosebericht, nicht fuer eine Annahme.
    """

    app_name: str
    status: DetectionStatus
    bundle_id: str | None = None
    bundle_version: str | None = None
    #: Alle verifizierten Kandidaten - mehr als einer bedeutet AMBIGUOUS.
    candidates: tuple[str, ...] = ()
    domains: tuple[DomainMatch, ...] = ()
    #: Menschenlesbare Begruendung fuer den Diagnosebericht.
    reason: str | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.status is DetectionStatus.CONFIRMED
