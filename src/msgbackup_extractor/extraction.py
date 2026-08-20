"""Orchestrierung des `extract`-Modus.

Ablauf:

1. App im Backup erkennen und verifizieren.
2. Manifest lesen, Eintraege der App-Domains sammeln.
3. App-Datenbank finden und lesbar machen (bei verschluesselten Backups in das
   Arbeitsverzeichnis der Session entschluesselt, niemals im Backup).
4. Medien aufzaehlen lassen - das Profil liefert Quelle, Chat, Originalname und
   Zeitstempel.
5. Uebrige App-Dateien ergaenzen, damit Datenbanken und Metadaten nicht
   verloren gehen.
6. Plan bauen, ausfuehren, Manifest und Bericht schreiben.

Zwei Punkte, die hier bewusst so geloest sind:

* Kann das Profil keine Zuordnung belegen, faellt der Export auf die reine
  Dateiauswahl anhand der Domains zurueck - **mit** Hinweis im Bericht, statt
  eine Chat-Struktur zu erfinden.
* `--dry-run` durchlaeuft Schritt 1 bis 5 vollstaendig und bricht vor dem
  Schreiben ab. Der Probelauf beruht damit auf demselben Plan wie der Ernstfall.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from msgbackup_extractor.apps.base import AppProfile, MediaContext, MediaEnumeration
from msgbackup_extractor.apps.registry import detect_all, get_profile
from msgbackup_extractor.core import media as media_module
from msgbackup_extractor.core.encryption import DecryptionError, decrypt_file_to
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.manifest import ManifestReader
from msgbackup_extractor.core.paths import OutputGuard
from msgbackup_extractor.core.session import BackupSession
from msgbackup_extractor.core.sqlite_ro import (
    NotASQLiteDatabase,
    describe_database,
    looks_like_sqlite,
    open_readonly,
)
from msgbackup_extractor.extract.planner import ExtractOptions, Planner
from msgbackup_extractor.extract.runner import Runner
from msgbackup_extractor.extract.sources import MediaReader
from msgbackup_extractor.models import (
    DetectionResult,
    DetectionStatus,
    ExtractionPlan,
    ExtractionResult,
    FileKind,
    ManifestEntry,
    MediaItem,
    MediaSource,
    SourceKind,
)

logger = get_logger("extraction")


class ExtractionBlocked(RuntimeError):
    """Die Extraktion kann nicht sinnvoll beginnen."""

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass(slots=True)
class ExtractionOutcome:
    """Ergebnis eines Laufs, inklusive Plan und Hinweisen."""

    profile_slug: str
    profile_name: str
    detection: DetectionResult
    plan: ExtractionPlan
    result: ExtractionResult
    notes: tuple[str, ...] = ()
    #: True, wenn ohne Datenbankzuordnung gearbeitet wurde.
    domain_fallback: bool = False
    dangling_references: int = 0


@dataclass(slots=True)
class Extractor:
    """Fuehrt die Extraktion fuer eine App aus."""

    session: BackupSession
    output_dir: Path
    options: ExtractOptions = field(default_factory=ExtractOptions)
    app_slug: str | None = None
    bundle_id: str | None = None

    def run(self) -> ExtractionOutcome:
        info = self.session.backup.info()
        profile, detection = self._resolve_profile(info)

        if not self.session.manifest.is_available:
            raise ExtractionBlocked(
                self.session.manifest.unavailable_reason
                or "Die Manifest.db konnte nicht gelesen werden."
            )

        notes: list[str] = []
        with ExitStack() as stack:
            reader = stack.enter_context(ManifestReader(self.session.manifest.path))
            domains = profile.match_domains(
                detection.bundle_id or "", reader.domain_names()
            )
            if not domains:
                raise ExtractionBlocked(
                    f"{profile.name} ist installiert, aber keine Domain im Backup "
                    "gehoert dazu. Es gibt nichts zu extrahieren."
                )

            entries = tuple(
                entry
                for entry in reader.entries(domains=tuple(d.domain for d in domains))
                if entry.kind is FileKind.FILE
            )
            entries_by_id = {entry.file_id: entry for entry in entries}

            enumeration, connection = self._enumerate(profile, entries, stack, notes)

            media_reader = MediaReader(
                backup=self.session.backup,
                connection=connection,
                keys=self.session.keys,
                entries=entries_by_id,
            )

            referenced = {
                item.source.file_id
                for item in enumeration.items
                if item.source.file_id is not None
            }
            items = enumeration.items + self._remaining_entries(entries, referenced)

            plan = Planner(reader=media_reader, options=self.options).build(items)
            guard = OutputGuard(
                root=self.output_dir, forbidden_roots=(self.session.backup.path,)
            )
            result = Runner(
                reader=media_reader, guard=guard, options=self.options
            ).run(plan)

        notes.extend(enumeration.notes)
        return ExtractionOutcome(
            profile_slug=profile.slug,
            profile_name=profile.name,
            detection=detection,
            plan=plan,
            result=result,
            notes=tuple(notes),
            domain_fallback=not enumeration.is_supported,
            dangling_references=enumeration.dangling_references,
        )

    # -- App-Erkennung ------------------------------------------------------

    def _resolve_profile(self, info: object) -> tuple[AppProfile, DetectionResult]:
        if self.app_slug is not None:
            profile = get_profile(self.app_slug)
            detection = profile.detect(info)  # type: ignore[arg-type]
            candidates = [(profile, detection)]
        else:
            candidates = [
                (p, d)
                for p, d in detect_all(info)  # type: ignore[arg-type]
                if d.status is not DetectionStatus.NOT_FOUND
            ]

        if not candidates:
            raise ExtractionBlocked(
                "Im Backup wurde kein unterstuetzter Messenger gefunden. "
                "Ohne erkannte App gibt es nichts zu extrahieren."
            )
        if len(candidates) > 1:
            names = ", ".join(p.slug for p, _ in candidates)
            raise ExtractionBlocked(
                f"Es wurden mehrere Messenger gefunden ({names}). Waehle einen "
                "mit --app aus; ein Export mehrerer Apps in dasselbe Verzeichnis "
                "waere nicht eindeutig."
            )

        profile, detection = candidates[0]
        detection = self._disambiguate(detection)
        if detection.status is not DetectionStatus.CONFIRMED:
            raise ExtractionBlocked(
                detection.reason
                or f"{profile.name} konnte nicht eindeutig erkannt werden.",
                diagnostics={"candidates": list(detection.candidates)},
            )
        return profile, detection

    def _disambiguate(self, detection: DetectionResult) -> DetectionResult:
        if detection.status is not DetectionStatus.AMBIGUOUS or self.bundle_id is None:
            return detection
        if self.bundle_id not in detection.candidates:
            raise ExtractionBlocked(
                f"--bundle-id {self.bundle_id} passt zu keinem gefundenen "
                f"Kandidaten. Gefunden: {', '.join(detection.candidates)}."
            )
        return DetectionResult(
            app_name=detection.app_name,
            status=DetectionStatus.CONFIRMED,
            bundle_id=self.bundle_id,
            candidates=detection.candidates,
            reason="Aus mehreren Kandidaten durch --bundle-id ausgewaehlt.",
        )

    # -- Datenbank und Enumeration -----------------------------------------

    def _enumerate(
        self,
        profile: AppProfile,
        entries: tuple[ManifestEntry, ...],
        stack: ExitStack,
        notes: list[str],
    ) -> tuple[MediaEnumeration, sqlite3.Connection | None]:
        """Oeffnet die App-Datenbank und laesst das Profil Medien aufzaehlen."""
        database = self._locate_database(profile, entries)
        if database is None:
            notes.append(
                "Es wurde keine lesbare App-Datenbank gefunden. Der Export "
                "erfolgt anhand der Domains, ohne Chat-Zuordnung."
            )
            return MediaEnumeration(unsupported_reason="keine Datenbank"), None

        entry, path = database
        try:
            connection = stack.enter_context(open_readonly(path))
        except NotASQLiteDatabase as error:
            notes.append(
                f"Die App-Datenbank ist nicht lesbar ({error}). Der Export "
                "erfolgt anhand der Domains, ohne Chat-Zuordnung."
            )
            return MediaEnumeration(unsupported_reason=str(error)), None

        schemas = describe_database(connection, count_rows=False)
        external = {
            candidate.basename: candidate
            for candidate in entries
            if "_EXTERNAL_DATA/" in candidate.relative_path
        }
        context = MediaContext(
            connection=connection,
            schemas=schemas,
            external_files=external,
            entries_by_path={c.relative_path: c for c in entries},
        )
        enumeration = profile.enumerate_media(context)
        if not enumeration.is_supported:
            notes.append(
                f"{enumeration.unsupported_reason} Der Export erfolgt anhand der "
                "Domains, ohne Chat-Zuordnung."
            )
            return enumeration, connection

        logger.debug(
            "%d Medien aus %s aufgezaehlt", len(enumeration.items), entry.basename
        )
        return enumeration, connection

    def _locate_database(
        self, profile: AppProfile, entries: tuple[ManifestEntry, ...]
    ) -> tuple[ManifestEntry, Path] | None:
        """Findet die Datenbank, die das Profil fuer die Zuordnung braucht.

        Kriterium ist nicht der Dateiname, sondern der Inhalt: die Datei muss
        eine lesbare SQLite-Datenbank sein und die vom Profil benoetigten
        Tabellen enthalten. So funktioniert es auch, wenn Threema seine
        Datenbank umbenennt.
        """
        required = {name.upper() for name in profile.requires_tables()}
        best: tuple[ManifestEntry, Path] | None = None

        for entry in sorted(entries, key=lambda e: -(e.size or 0)):
            path = self._readable_path(entry)
            if path is None or not looks_like_sqlite(path):
                continue
            try:
                with open_readonly(path) as connection:
                    tables = {
                        name.upper() for name in describe_database(connection, count_rows=False)
                    }
            except NotASQLiteDatabase:
                continue
            if not required or required <= tables:
                return entry, path
            if best is None:
                best = (entry, path)

        if best is not None:
            logger.debug(
                "Keine Datenbank mit den Tabellen %s gefunden", sorted(required)
            )
        return None

    def _readable_path(self, entry: ManifestEntry) -> Path | None:
        """Pfad, unter dem die Datei lesbar ist - notfalls entschluesselt."""
        source = self.session.backup.payload_path(entry.file_id)
        if not source.is_file():
            return None
        if not entry.is_encrypted:
            return source

        keys = self.session.keys
        if keys is None or entry.protection_class is None or entry.encryption_key is None:
            return None
        destination = self.session.ensure_work_dir() / f"db-{entry.file_id}.sqlite"
        if destination.is_file():
            return destination
        try:
            with keys.unwrap_file_key(entry.protection_class, entry.encryption_key) as key:
                decrypt_file_to(source, destination, key, size=entry.size)
        except (DecryptionError, OSError):
            destination.unlink(missing_ok=True)
            return None
        return destination

    # -- Restliche Dateien --------------------------------------------------

    @staticmethod
    def _remaining_entries(
        entries: tuple[ManifestEntry, ...], referenced: set[str]
    ) -> tuple[MediaItem, ...]:
        """App-Dateien, die die Enumeration nicht abgedeckt hat.

        Sonst gingen genau die Dinge verloren, die keine Chat-Medien sind: die
        App-Datenbank selbst, Einstellungen, Logs. Sie landen ueber die
        Typerkennung in `databases/` bzw. `metadata/`.
        """
        return tuple(
            MediaItem(
                source=MediaSource(
                    kind=SourceKind.EXTERNAL_FILE,
                    file_id=entry.file_id,
                    domain=entry.domain,
                    relative_path=entry.relative_path,
                ),
                size=entry.size,
                original_filename=entry.basename,
                timestamp=entry.last_modified,
            )
            for entry in entries
            if entry.file_id not in referenced
        )


def probe_size(reader: MediaReader, item: MediaItem) -> int:
    """Groesse eines Mediums, notfalls durch Lesen bestimmt."""
    if item.size is not None:
        return item.size
    return len(reader.head(item, media_module.HEADER_SIZE))
