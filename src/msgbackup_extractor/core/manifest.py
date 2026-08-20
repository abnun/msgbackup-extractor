"""Lesen der Manifest.db und Dekodieren der MBFile-Metadaten.

Zwei Dinge werden hier bewusst nicht angenommen:

1. **Das Tabellenschema.** Die Tabelle heisst ueblicherweise `Files` und hat
   die Spalten `fileID`, `domain`, `relativePath`, `flags`, `file`. Geprueft
   wird das trotzdem: Namen werden case-insensitiv aufgeloest, zusaetzliche
   Spalten stoeren nicht, und fehlt eine Pflichtspalte, entsteht ein
   Diagnosefehler statt eines geratenen Ergebnisses.

2. **Der Inhalt der `file`-Spalte.** Dort steht ein NSKeyedArchiver-Plist mit
   einem MBFile-Objekt. Ist es unlesbar oder anders aufgebaut als erwartet,
   wird der Eintrag mit `decode_error` versehen und weiterverarbeitet - eine
   einzelne kaputte Zeile darf nicht den ganzen Lauf verlieren.
"""

from __future__ import annotations

import plistlib
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.sqlite_ro import describe_database, find_table, open_readonly
from msgbackup_extractor.models import FileKind, ManifestEntry, TableSchema

logger = get_logger("manifest")

#: Moegliche Namen der Dateitabelle, in der Reihenfolge der Wahrscheinlichkeit.
FILES_TABLE_CANDIDATES: Final = ("Files", "File", "MBFiles")

#: Pflichtspalten. Ohne diese ist das Manifest nicht verwertbar.
REQUIRED_COLUMNS: Final = ("fileID", "domain", "relativePath")

#: Optionale Spalten, deren Fehlen den Funktionsumfang einschraenkt.
OPTIONAL_COLUMNS: Final = ("flags", "file")

#: Bezugszeitpunkt der MBFile-Zeitstempel.
#:
#: Am echten Backup verifiziert: jeder gepruefte Wert ergibt mit der
#: Unix-Epoche ein plausibles Datum, mit der Apple-Epoche (2001) kein einziges -
#: dort landen sie 31 Jahre in der Zukunft. MBFile und Core Data verwenden
#: also unterschiedliche Bezugszeitpunkte; Core-Data-Zeitstempel (ZDATE) zaehlen
#: ab 2001 und werden im jeweiligen App-Profil umgerechnet.
MBFILE_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

#: Bezugszeitpunkt von Core-Data-Zeitstempeln, hier nur zur Abgrenzung.
APPLE_EPOCH: Final = datetime(2001, 1, 1, tzinfo=UTC)

#: Frueheste plausible Zeit - vor dem ersten iPhone gab es keine iOS-Backups.
EARLIEST_PLAUSIBLE: Final = datetime(2007, 1, 1, tzinfo=UTC)

#: Laenge eines `EncryptionKey`-Blobs: 4 Byte Protection Class + 40 Byte Wrapped Key.
ENCRYPTION_KEY_BLOB_LENGTH: Final = 44


class ManifestSchemaError(RuntimeError):
    """Das Manifest hat eine Struktur, die nicht verwertbar ist.

    Fuehrt bewusst zum Abbruch mit Diagnosebericht, nicht zu Rateversuchen.
    """

    def __init__(self, message: str, *, schemas: dict[str, TableSchema] | None = None) -> None:
        super().__init__(message)
        self.schemas = schemas or {}


# ---------------------------------------------------------------------------
# MBFile-Dekodierung
# ---------------------------------------------------------------------------


def mbfile_timestamp(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Wandelt einen MBFile-Zeitstempel in ein `datetime`, oder None.

    Zeitstempel koennen 0, negativ oder unsinnig sein. Unplausible Werte werden
    zu None - eine erfundene Zeit waere schlimmer als keine.

    Die Obergrenze ist absichtlich "jetzt" (mit einem Tag Spielraum fuer
    Uhrenabweichungen) und nicht irgendein fernes Jahr: eine Datei kann nicht in
    der Zukunft geaendert worden sein. Genau diese Pruefung deckt eine
    verwechselte Epoche sofort auf, statt sie 31 Jahre spaeter sichtbar werden
    zu lassen.

    Args:
        now: Bezugszeit fuer die Obergrenze. Nur fuer Tests zu setzen.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    try:
        stamp = MBFILE_EPOCH + timedelta(seconds=float(value))
    except (OverflowError, ValueError, OSError):
        return None

    upper = (now or datetime.now(UTC)) + timedelta(days=1)
    if not (EARLIEST_PLAUSIBLE <= stamp <= upper):
        logger.debug("Unplausibler MBFile-Zeitstempel verworfen: %s", stamp.isoformat())
        return None
    return stamp


def _resolve(objects: list[Any], value: Any) -> Any:
    """Loest eine NSKeyedArchiver-UID in das referenzierte Objekt auf."""
    if isinstance(value, plistlib.UID):
        index = value.data
        if 0 <= index < len(objects):
            return objects[index]
        return None
    return value


@dataclass(frozen=True, slots=True)
class MBFile:
    """Die aus einem MBFile-Blob gelesenen Felder."""

    size: int | None = None
    protection_class: int | None = None
    encryption_key: bytes | None = None
    mode: int | None = None
    inode: int | None = None
    flags: int | None = None
    relative_path: str | None = None
    birth: datetime | None = None
    last_modified: datetime | None = None
    last_status_change: datetime | None = None


def decode_mbfile(blob: bytes | None) -> MBFile:
    """Dekodiert die `file`-Spalte eines Manifest-Eintrags.

    Raises:
        ValueError: Wenn das Blob kein verwertbares NSKeyedArchiver-Plist ist.
    """
    if not blob:
        raise ValueError("leeres MBFile-Blob")

    try:
        plist = plistlib.loads(blob)
    except Exception as error:  # plistlib wirft verschiedene Typen
        raise ValueError(f"kein lesbares Plist: {error}") from error

    if not isinstance(plist, dict):
        raise ValueError("Plist enthaelt kein Dictionary")

    objects = plist.get("$objects")
    top = plist.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        raise ValueError("kein NSKeyedArchiver-Aufbau ($objects/$top fehlen)")

    root = _resolve(objects, top.get("root"))
    if not isinstance(root, dict):
        raise ValueError("Wurzelobjekt ist kein Dictionary")

    encryption_key: bytes | None = None
    key_object = _resolve(objects, root.get("EncryptionKey"))
    if isinstance(key_object, dict):
        raw = key_object.get("NS.data")
        if isinstance(raw, bytes):
            if len(raw) < 8:
                raise ValueError(f"EncryptionKey zu kurz ({len(raw)} Bytes)")
            # Die ersten 4 Byte sind die Protection Class, danach der Wrapped Key.
            encryption_key = raw[4:]
    elif isinstance(key_object, bytes):
        encryption_key = key_object[4:]

    relative_path = _resolve(objects, root.get("RelativePath"))

    def _int(key: str) -> int | None:
        value = root.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return MBFile(
        size=_int("Size"),
        protection_class=_int("ProtectionClass"),
        encryption_key=encryption_key,
        mode=_int("Mode"),
        inode=_int("InodeNumber"),
        flags=_int("Flags"),
        relative_path=relative_path if isinstance(relative_path, str) else None,
        birth=mbfile_timestamp(root.get("Birth")),
        last_modified=mbfile_timestamp(root.get("LastModified")),
        last_status_change=mbfile_timestamp(root.get("LastStatusChange")),
    )


# ---------------------------------------------------------------------------
# Schema-Aufloesung
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestLayout:
    """Die im konkreten Manifest tatsaechlich vorgefundene Struktur."""

    table: str
    #: Kanonischer Name -> tatsaechlicher Spaltenname in der Datenbank.
    columns: dict[str, str]
    schema: TableSchema

    @property
    def has_metadata_blob(self) -> bool:
        """True, wenn die `file`-Spalte vorhanden ist (Groesse, Schluessel, Zeiten)."""
        return "file" in self.columns

    @property
    def has_flags(self) -> bool:
        return "flags" in self.columns


def resolve_layout(schemas: dict[str, TableSchema]) -> ManifestLayout:
    """Ermittelt Tabelle und Spaltennamen aus dem tatsaechlichen Schema.

    Raises:
        ManifestSchemaError: Wenn keine Dateitabelle oder eine Pflichtspalte fehlt.
    """
    table = find_table(schemas, *FILES_TABLE_CANDIDATES)
    if table is None:
        raise ManifestSchemaError(
            "In der Manifest.db wurde keine Dateitabelle gefunden. Gesucht wurde "
            f"nach {', '.join(FILES_TABLE_CANDIDATES)}; vorhanden sind: "
            f"{', '.join(sorted(schemas)) or '(keine Tabellen)'}.",
            schemas=schemas,
        )

    lowered = {column.lower(): column for column in table.columns}
    columns: dict[str, str] = {}
    missing: list[str] = []
    for canonical in REQUIRED_COLUMNS:
        actual = lowered.get(canonical.lower())
        if actual is None:
            missing.append(canonical)
        else:
            columns[canonical] = actual

    if missing:
        raise ManifestSchemaError(
            f"Der Tabelle {table.name} fehlen die Pflichtspalten "
            f"{', '.join(missing)}. Vorhanden: {', '.join(table.columns)}.",
            schemas=schemas,
        )

    for canonical in OPTIONAL_COLUMNS:
        actual = lowered.get(canonical.lower())
        if actual is not None:
            columns[canonical] = actual
        else:
            logger.warning(
                "Spalte %s fehlt in Tabelle %s; entsprechende Angaben bleiben leer",
                canonical,
                table.name,
            )

    return ManifestLayout(table=table.name, columns=columns, schema=table)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestStatistics:
    """Aggregierte Kennzahlen ueber ein Manifest - ohne Klartextpfade."""

    total_entries: int
    files: int
    directories: int
    symlinks: int
    total_size: int
    entries_per_domain: dict[str, int]
    size_per_domain: dict[str, int]
    decode_errors: int
    encrypted_entries: int


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class ManifestReader:
    """Liest eine (entschluesselte) Manifest.db strikt lesend.

    Nutzung:

        with ManifestReader(path) as reader:
            for entry in reader.entries():
                ...
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._context: Any = None
        self.schemas: dict[str, TableSchema] = {}
        self.layout: ManifestLayout | None = None

    # -- Lebenszyklus -------------------------------------------------------

    def __enter__(self) -> ManifestReader:
        self._context = open_readonly(self.path)
        self._connection = self._context.__enter__()
        self.schemas = describe_database(self._connection)
        self.layout = resolve_layout(self.schemas)
        logger.debug(
            "Manifest-Schema erkannt: Tabelle %s mit %d Spalten",
            self.layout.table,
            len(self.layout.schema.columns),
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context is not None:
            self._context.__exit__(*exc)
        self._connection = None
        self._context = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("ManifestReader muss als Context Manager verwendet werden")
        return self._connection

    def _require_layout(self) -> ManifestLayout:
        if self.layout is None:
            raise RuntimeError("ManifestReader muss als Context Manager verwendet werden")
        return self.layout

    # -- Abfragen -----------------------------------------------------------

    def entries(self, *, domains: tuple[str, ...] | None = None) -> Iterator[ManifestEntry]:
        """Iteriert ueber die Manifest-Eintraege.

        Args:
            domains: Wenn gesetzt, werden nur diese Domains geliefert. Der
                Vergleich ist exakt; Praefix-Suche macht `domain_names()`.
        """
        layout = self._require_layout()
        columns = layout.columns

        selected = [columns["fileID"], columns["domain"], columns["relativePath"]]
        if layout.has_flags:
            selected.append(columns["flags"])
        if layout.has_metadata_blob:
            selected.append(columns["file"])

        query = f"SELECT {', '.join(_quote(c) for c in selected)} FROM {_quote(layout.table)}"
        parameters: tuple[str, ...] = ()
        if domains is not None:
            if not domains:
                return
            placeholders = ",".join("?" * len(domains))
            query += f" WHERE {_quote(columns['domain'])} IN ({placeholders})"
            parameters = domains

        for row in self.connection.execute(query, parameters):
            yield self._to_entry(row, layout)

    def _to_entry(self, row: sqlite3.Row, layout: ManifestLayout) -> ManifestEntry:
        columns = layout.columns
        file_id = row[columns["fileID"]]
        domain = row[columns["domain"]] or ""
        relative_path = row[columns["relativePath"]] or ""

        kind: FileKind | None = None
        if layout.has_flags:
            raw_flags = row[columns["flags"]]
            try:
                kind = FileKind(raw_flags)
            except ValueError:
                kind = None

        metadata = MBFile()
        decode_error: str | None = None
        if layout.has_metadata_blob:
            try:
                metadata = decode_mbfile(row[columns["file"]])
            except ValueError as error:
                decode_error = str(error)
                logger.debug("MBFile-Blob von %s nicht lesbar: %s", file_id, error)

        if kind is None and metadata.flags is not None:
            try:
                kind = FileKind(metadata.flags)
            except ValueError:
                kind = None

        return ManifestEntry(
            file_id=str(file_id),
            domain=str(domain),
            relative_path=str(relative_path),
            kind=kind,
            size=metadata.size,
            protection_class=metadata.protection_class,
            encryption_key=metadata.encryption_key,
            mode=metadata.mode,
            inode=metadata.inode,
            birth=metadata.birth,
            last_modified=metadata.last_modified,
            last_status_change=metadata.last_status_change,
            decode_error=decode_error,
        )

    def domain_names(self) -> tuple[str, ...]:
        """Alle im Manifest vorkommenden Domains, alphabetisch."""
        layout = self._require_layout()
        column = _quote(layout.columns["domain"])
        rows = self.connection.execute(
            f"SELECT DISTINCT {column} FROM {_quote(layout.table)} ORDER BY {column}"
        ).fetchall()
        return tuple(str(row[0]) for row in rows if row[0])

    def count(self) -> int:
        layout = self._require_layout()
        return int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM {_quote(layout.table)}"
            ).fetchone()[0]
        )

    def statistics(self, *, domains: tuple[str, ...] | None = None) -> ManifestStatistics:
        """Aggregiert Kennzahlen. Enthaelt bewusst keine Pfade oder Dateinamen."""
        entries_per_domain: Counter[str] = Counter()
        size_per_domain: Counter[str] = Counter()
        files = directories = symlinks = 0
        total_size = decode_errors = encrypted = 0
        total = 0

        for entry in self.entries(domains=domains):
            total += 1
            entries_per_domain[entry.domain] += 1
            if entry.decode_error:
                decode_errors += 1
            if entry.encryption_key is not None:
                encrypted += 1
            match entry.kind:
                case FileKind.FILE:
                    files += 1
                    if entry.size:
                        total_size += entry.size
                        size_per_domain[entry.domain] += entry.size
                case FileKind.DIRECTORY:
                    directories += 1
                case FileKind.SYMLINK:
                    symlinks += 1
                case _:
                    pass

        return ManifestStatistics(
            total_entries=total,
            files=files,
            directories=directories,
            symlinks=symlinks,
            total_size=total_size,
            entries_per_domain=dict(entries_per_domain),
            size_per_domain=dict(size_per_domain),
            decode_errors=decode_errors,
            encrypted_entries=encrypted,
        )
