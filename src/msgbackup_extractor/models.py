"""Datenmodelle.

Reine Datentraeger ohne Logik und ohne Abhaengigkeit auf andere Module des
Pakets. Alles was hierher gehoert ist beschreibend; Entscheidungen treffen die
Module in `core/`, `apps/` und `extract/`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

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


# ---------------------------------------------------------------------------
# Extraktion
# ---------------------------------------------------------------------------


class TimestampSource(enum.StrEnum):
    """Woher der Zeitstempel eines Mediums stammt.

    Der Unterschied ist inhaltlich, nicht technisch: ein Zeitstempel aus der
    App-Datenbank sagt, wann die Nachricht gesendet wurde. Der Zeitstempel einer
    Backupdatei sagt nur, wann das Backup sie geschrieben hat - fuer eine
    Zeitachse ist das wertlos und irrefuehrend, weil alle solchen Dateien am
    Tag des Backups landen.
    """

    #: Aus der App-Datenbank, also das Datum der Nachricht.
    MESSAGE = "message"
    #: Aus den Dateimetadaten des Backups (MBFile).
    FILE = "file"


class SourceKind(enum.StrEnum):
    """Woher der Inhalt einer zu exportierenden Datei kommt.

    Der Unterschied ist wesentlich und nicht bloss technisch: Messenger auf iOS
    speichern Medien teils als Datei im Backup, teils als Blob **in** ihrer
    Datenbank. Ein Extractor, der nur Dateien kopiert, verliert die Inline-Blobs
    stillschweigend und meldet trotzdem Erfolg.
    """

    #: Eine Datei im Backup, adressiert ueber ihre fileID.
    EXTERNAL_FILE = "external_file"
    #: Ein BLOB-Wert in einer Tabelle der App-Datenbank.
    INLINE_BLOB = "inline_blob"


@dataclass(frozen=True, slots=True)
class MediaSource:
    """Adresse des Inhalts, je nach `kind` unterschiedlich befuellt."""

    kind: SourceKind
    #: Bei EXTERNAL_FILE: die fileID im Backup.
    file_id: str | None = None
    #: Bei EXTERNAL_FILE: Domain und relativer Pfad, fuer Bericht und Manifest.
    domain: str | None = None
    relative_path: str | None = None
    #: Bei INLINE_BLOB: Tabelle, Primaerschluessel und Spalte in der App-DB.
    table: str | None = None
    row_id: int | None = None
    column: str | None = None
    #: Bytes am Anfang des Blobs, die nicht zum Inhalt gehoeren.
    #: Core Data stellt Inline-Daten ein Markierungsbyte voran; ohne dieses
    #: Abschneiden waere jede exportierte Datei um ein Byte verschoben und
    #: damit unbrauchbar. Welcher Wert richtig ist, weiss das App-Profil.
    byte_offset: int = 0

    @property
    def is_external(self) -> bool:
        return self.kind is SourceKind.EXTERNAL_FILE

    def identity(self) -> str:
        """Eindeutige, stabile Kennung dieser Quelle - fuer Deduplizierung."""
        if self.is_external:
            return f"file:{self.file_id}"
        return f"blob:{self.table}:{self.row_id}:{self.column}"


@dataclass(frozen=True, slots=True)
class ChatReference:
    """Ein Chat, dem Medien zugeordnet werden koennen."""

    #: Stabile ID aus der App-Datenbank (z.B. der Primaerschluessel).
    chat_id: str
    #: Anzeigename. None, wenn kein Name belegbar ist.
    name: str | None
    #: "group", "direct" oder "unknown".
    kind: str = "unknown"

    @property
    def display_name(self) -> str:
        """Verzeichnistauglicher Name. Ohne belegbaren Namen die ID."""
        return self.name or f"chat-{self.chat_id}"


@dataclass(frozen=True, slots=True)
class MediaItem:
    """Ein Medium, das die App-Datenbank kennt - Kandidat fuer den Export.

    Alle Metadaten hier stammen aus der Datenbank oder dem Manifest. Was dort
    nicht steht, bleibt `None`; es wird nichts erfunden.
    """

    source: MediaSource
    #: Groesse in Byte, soweit bekannt.
    size: int | None = None
    #: Zugeordneter Chat. None bedeutet: nicht belegbar -> `unassigned/`.
    chat: ChatReference | None = None
    #: Originaldateiname aus der Datenbank, falls vorhanden.
    original_filename: str | None = None
    #: Zeitstempel der zugehoerigen Nachricht oder der Backupdatei.
    timestamp: datetime | None = None
    #: Woher der Zeitstempel stammt. None, wenn keiner vorliegt.
    timestamp_source: TimestampSource | None = None
    #: Von der App angegebener MIME-Type. Die Signaturerkennung hat Vorrang.
    declared_mime: str | None = None
    #: True, wenn dies ein Vorschaubild und nicht das Original ist.
    is_thumbnail: bool = False
    #: Kennung des Originals, zu dem dieses Thumbnail gehoert.
    thumbnail_of: str | None = None
    #: Primaerschluessel der Nachricht, ueber die zugeordnet wurde.
    message_id: str | None = None
    #: Welche Join-Kette die Zuordnung belegt - fuer Nachvollziehbarkeit.
    evidence: str | None = None

    @property
    def is_assigned(self) -> bool:
        return self.chat is not None


class FileOutcome(enum.StrEnum):
    """Ergebnis der Verarbeitung einer einzelnen Datei."""

    EXTRACTED = "extracted"
    #: Bereits vorhanden und inhaltsgleich, deshalb uebersprungen.
    SKIPPED = "skipped"
    #: Als Duplikat erkannt; mit --deduplicate nicht erneut geschrieben.
    DUPLICATE = "duplicate"
    FAILED = "failed"
    #: Verschluesselt, aber kein Schluessel verfuegbar.
    UNDECRYPTABLE = "undecryptable"
    #: Quelle im Backup nicht vorhanden.
    MISSING = "missing"
    #: Geschrieben, aber Quell- und Zielhash weichen ab.
    INTEGRITY_ERROR = "integrity_error"


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """Eine geplante Ausgabedatei. Enthaelt alles, was der Runner braucht.

    Der Plan ist rein rechnerisch: er entsteht ohne Schreibzugriff und ist damit
    die gemeinsame Grundlage von `--dry-run` und dem echten Lauf. Beides laeuft
    dadurch durch dieselbe Logik, statt zwei Codepfade zu sein.
    """

    item: MediaItem
    #: Zielpfad relativ zum Ausgabeverzeichnis.
    output_path: PurePosixPath
    #: Zusaetzliche Pfade, die auf denselben Inhalt zeigen (Chat-Struktur).
    link_paths: tuple[PurePosixPath, ...] = ()
    media_type: MediaType | None = None
    #: Manifest-Eintrag der Quelldatei, nur bei externen Quellen.
    entry: ManifestEntry | None = None

    @property
    def category(self) -> MediaCategory:
        return self.media_type.category if self.media_type else MediaCategory.OTHER


@dataclass(frozen=True, slots=True)
class ExtractionPlan:
    """Was ein Extraktionslauf tun wuerde."""

    files: tuple[PlannedFile, ...]
    #: Dateien, die bewusst nicht exportiert werden, mit Begruendung.
    excluded: tuple[tuple[MediaItem, str], ...] = ()
    #: Medien, die die Datenbank kennt, deren Quelle aber fehlt.
    missing: tuple[MediaItem, ...] = ()

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(f.item.size or 0 for f in self.files)

    def counts_per_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for planned in self.files:
            key = planned.category.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def size_per_category(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for planned in self.files:
            key = planned.category.value
            sizes[key] = sizes.get(key, 0) + (planned.item.size or 0)
        return sizes


@dataclass(frozen=True, slots=True)
class ExtractedFile:
    """Ergebnis fuer eine verarbeitete Datei - ein Eintrag im Export-Manifest."""

    outcome: FileOutcome
    source_kind: SourceKind
    output_path: str | None
    link_paths: tuple[str, ...] = ()
    size: int | None = None
    sha256: str | None = None
    media_type: str | None = None
    detection_method: str | None = None
    extension_mismatch: bool = False
    #: Pixelmasse, soweit aus dem Dateikopf lesbar. Die Ansicht braucht sie, um
    #: je Kachel die passende Auflaesung zu waehlen: die von den Messengern
    #: gespeicherten Vorschaubilder sind teils winzig.
    width: int | None = None
    height: int | None = None
    #: Quellangaben - Pfade nur, soweit sie fuer die Nachvollziehbarkeit noetig sind.
    source_file_id: str | None = None
    source_domain: str | None = None
    source_table: str | None = None
    source_row_id: int | None = None
    chat_name: str | None = None
    chat_id: str | None = None
    original_filename: str | None = None
    timestamp: datetime | None = None
    timestamp_source: TimestampSource | None = None
    is_thumbnail: bool = False
    thumbnail_of: str | None = None
    #: Kennung der behaltenen Datei, wenn dies ein Duplikat ist.
    duplicate_of: str | None = None
    integrity_ok: bool | None = None
    #: Fehlerklasse, nie eine Nachricht mit Inhalt.
    error: str | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome in (
            FileOutcome.EXTRACTED,
            FileOutcome.SKIPPED,
            FileOutcome.DUPLICATE,
        )


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Gesamtergebnis eines Laufs."""

    files: tuple[ExtractedFile, ...]
    dry_run: bool = False
    output_dir: Path | None = None

    def count(self, outcome: FileOutcome) -> int:
        return sum(1 for f in self.files if f.outcome is outcome)

    @property
    def successful(self) -> int:
        return sum(1 for f in self.files if f.is_success)

    @property
    def failed(self) -> int:
        return sum(
            1
            for f in self.files
            if f.outcome
            in (FileOutcome.FAILED, FileOutcome.MISSING, FileOutcome.UNDECRYPTABLE)
        )

    @property
    def integrity_errors(self) -> int:
        return self.count(FileOutcome.INTEGRITY_ERROR)

    @property
    def total_bytes(self) -> int:
        return sum(f.size or 0 for f in self.files if f.is_success)
