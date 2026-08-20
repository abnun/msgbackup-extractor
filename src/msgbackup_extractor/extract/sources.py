"""Einheitlicher Lesezugriff auf die zwei Quellen von Mediendaten.

Messenger auf iOS legen Medien teils als Datei im Backup ab, teils als Blob in
ihrer Datenbank. Ein Extractor, der nur Dateien kopiert, verliert die
Inline-Blobs stillschweigend - am vermessenen Backup waeren das 714
Originalmedien gewesen.

Dieses Modul verbirgt den Unterschied hinter einer Schnittstelle, damit Planer,
Runner und Integritaetspruefung nur einen Weg kennen. Zusaetzlich verbirgt es,
ob eine Backupdatei verschluesselt ist.

Alle Zugriffe sind ausschliesslich lesend.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from msgbackup_extractor.core import media as media_module
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.encryption import (
    BackupKeys,
    DecryptionError,
    decrypt_head,
    decrypt_stream,
)
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.secure_memory import SecretBytes
from msgbackup_extractor.models import ManifestEntry, MediaItem, MediaType

logger = get_logger("sources")

CHUNK_SIZE: Final = 1024 * 1024


class SourceUnavailable(RuntimeError):
    """Der Inhalt ist nicht lesbar. Die Unterklasse nennt den Grund."""


class SourceMissing(SourceUnavailable):
    """Die Quelldatei ist im Backup nicht vorhanden."""


class SourceUndecryptable(SourceUnavailable):
    """Die Quelle ist verschluesselt und es liegt kein Schluessel vor."""


class SourceUnreadable(SourceUnavailable):
    """Die Datei ist vorhanden, laesst sich aber nicht lesen.

    Typisch: fehlende Rechte oder ein Lesefehler des Datentraegers. Das ist ein
    Befund fuer den Bericht, kein Grund, den ganzen Lauf abzubrechen - deshalb
    wird der rohe `OSError` hier in einen erwarteten Fehler uebersetzt.
    """


@dataclass(slots=True)
class MediaReader:
    """Liest Medieninhalte, unabhaengig von Quelle und Verschluesselung."""

    backup: AppleBackup
    #: Verbindung zur App-Datenbank, fuer Inline-Blobs. Read-only.
    connection: sqlite3.Connection | None = None
    #: Klassenschluessel, falls das Backup verschluesselt ist.
    keys: BackupKeys | None = None
    #: fileID -> Manifest-Eintrag, fuer Groesse und Schluessel.
    entries: dict[str, ManifestEntry] | None = None

    # -- Hilfen -------------------------------------------------------------

    def _entry(self, file_id: str) -> ManifestEntry | None:
        return (self.entries or {}).get(file_id)

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise SourceUnavailable(
                "Fuer Inline-Blobs ist eine Verbindung zur App-Datenbank noetig."
            )
        return self.connection

    @staticmethod
    def _quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _read_blob(self, item: MediaItem) -> bytes:
        """Liest einen Inline-Blob aus der App-Datenbank."""
        source = item.source
        if source.table is None or source.row_id is None or source.column is None:
            raise SourceUnavailable("Unvollstaendige Blob-Adresse")
        connection = self._require_connection()
        row = connection.execute(
            f"SELECT {self._quote(source.column)} FROM {self._quote(source.table)} "
            "WHERE Z_PK = ?",
            (source.row_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise SourceMissing(
                f"Der Blob {source.table}:{source.row_id} existiert nicht mehr."
            )
        raw = bytes(row[0])
        # Fuehrende Verwaltungsbytes abschneiden - siehe MediaSource.byte_offset.
        if source.byte_offset:
            if len(raw) <= source.byte_offset:
                raise SourceUnavailable(
                    f"Der Blob {source.table}:{source.row_id} ist kuerzer als sein "
                    f"Vorspann ({len(raw)} Byte)."
                )
            raw = raw[source.byte_offset :]
        return raw

    # -- Oeffentliche Schnittstelle -----------------------------------------

    def exists(self, item: MediaItem) -> bool:
        """Ist der Inhalt grundsaetzlich erreichbar?"""
        source = item.source
        if not source.is_external:
            return self.connection is not None
        return source.file_id is not None and self.backup.payload_exists(source.file_id)

    def head(self, item: MediaItem, length: int = media_module.HEADER_SIZE) -> bytes:
        """Liest den Anfang des Inhalts - genug fuer die Signaturerkennung.

        Bei verschluesselten Backups wird nur der Anfang entschluesselt; ein
        grosses Video muss dafuer nicht vollstaendig gelesen werden.
        """
        source = item.source
        if not source.is_external:
            return self._read_blob(item)[:length]

        if source.file_id is None:
            raise SourceUnavailable("Externe Quelle ohne fileID")
        path = self.backup.payload_path(source.file_id)
        if not path.is_file():
            raise SourceMissing(f"Nutzdatei {source.file_id[:8]} fehlt im Backup")

        entry = self._entry(source.file_id)
        try:
            if entry is not None and entry.is_encrypted:
                key = self._file_key(entry)
                try:
                    return decrypt_head(path, key, length)
                finally:
                    key.wipe()
            with path.open("rb") as handle:
                return handle.read(length)
        except OSError as error:
            raise SourceUnreadable(
                f"Nutzdatei {source.file_id[:8]} nicht lesbar: {type(error).__name__}"
            ) from error

    def stream(self, item: MediaItem) -> Iterator[bytes]:
        """Liefert den vollstaendigen Inhalt in Bloecken.

        Der Iterator muss erschoepft werden. Grosse Dateien landen dadurch nie
        vollstaendig im Speicher.
        """
        source = item.source
        if not source.is_external:
            data = self._read_blob(item)
            for offset in range(0, len(data), CHUNK_SIZE):
                yield data[offset : offset + CHUNK_SIZE]
            return

        if source.file_id is None:
            raise SourceUnavailable("Externe Quelle ohne fileID")
        path = self.backup.payload_path(source.file_id)
        if not path.is_file():
            raise SourceMissing(f"Nutzdatei {source.file_id[:8]} fehlt im Backup")

        entry = self._entry(source.file_id)
        try:
            if entry is not None and entry.is_encrypted:
                key = self._file_key(entry)
                try:
                    with path.open("rb") as handle:
                        yield from decrypt_stream(handle, key, size=entry.size)
                finally:
                    key.wipe()
                return

            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    yield chunk
        except OSError as error:
            raise SourceUnreadable(
                f"Nutzdatei {source.file_id[:8]} nicht lesbar: {type(error).__name__}"
            ) from error

    def _file_key(self, entry: ManifestEntry) -> SecretBytes:
        """Entpackt den Dateischluessel oder erklaert, warum es nicht geht."""
        if self.keys is None:
            raise SourceUndecryptable(
                "Die Datei ist verschluesselt, es liegen aber keine Schluessel vor."
            )
        if entry.protection_class is None or entry.encryption_key is None:
            raise SourceUndecryptable(
                "Der Manifest-Eintrag enthaelt keinen Dateischluessel; sein "
                "MBFile-Blob war unlesbar."
            )
        try:
            return self.keys.unwrap_file_key(entry.protection_class, entry.encryption_key)
        except DecryptionError as error:
            raise SourceUndecryptable(str(error)) from error

    def detect_type(self, item: MediaItem) -> MediaType:
        """Bestimmt den Medientyp aus dem Inhalt.

        Der Originaldateiname aus der Datenbank liefert die Endung fuer die
        Mismatch-Erkennung. Fehlt er, wird der relative Pfad im Backup
        verwendet - und wenn auch der fehlt, entscheidet allein die Signatur.
        """
        filename = item.original_filename or item.source.relative_path
        return media_module.detect(self.head(item), filename=filename)
