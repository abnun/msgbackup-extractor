"""Orchestrierung des `analyze`-Modus.

Streng read-only: es wird gelesen, gezaehlt und berichtet, nichts geschrieben
und nichts extrahiert. Der Bericht ist standardmaessig aggregiert - keine
Klartextpfade, keine Kontaktdaten, keine Inhalte.

Bei einem verschluesselten Backup ohne Passwort entsteht bewusst ein
**Teilbericht**: Geraet, iOS-Version, Verschluesselungszustand und die
App-Erkennung stammen aus `Info.plist` und `Manifest.plist` und sind ohne
Passwort verfuegbar. Datei- und Medienstatistiken brauchen die Manifest.db und
fehlen dann - ausgewiesen als solche, nicht stillschweigend als Null.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from msgbackup_extractor.apps.base import AppProfile, DatabaseCandidate, DatabaseRole
from msgbackup_extractor.apps.registry import detect_all, get_profile
from msgbackup_extractor.core import media as media_module
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.manifest import (
    ManifestReader,
    ManifestSchemaError,
    ManifestStatistics,
)
from msgbackup_extractor.core.sqlite_ro import (
    NotASQLiteDatabase,
    describe_database,
    open_readonly,
)
from msgbackup_extractor.models import (
    BackupInfo,
    DetectionResult,
    DetectionStatus,
    DomainMatch,
    FileKind,
    ManifestEntry,
    MediaCategory,
    MediaType,
    TableSchema,
)

logger = get_logger("analysis")


class AnalysisBlocked(RuntimeError):
    """Die Analyse kann nicht sinnvoll fortgesetzt werden.

    Traegt eine Diagnose, die dem Nutzer sagt, was fehlt - statt ein Ergebnis
    zu liefern, das auf Annahmen beruht.
    """

    def __init__(self, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


# ---------------------------------------------------------------------------
# Berichtsdaten
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormatCount:
    """Wie oft ein konkretes Format vorkommt und wie viel Platz es belegt."""

    format_name: str
    mime_type: str | None
    category: MediaCategory
    count: int
    total_size: int


@dataclass(frozen=True, slots=True)
class DatabaseReport:
    """Eine gefundene SQLite-Datenbank der App."""

    file_id: str
    domain: str
    #: Nur der Dateiname, nicht der vollstaendige Pfad.
    basename: str
    size: int | None
    tables: tuple[str, ...]
    table_count: int
    role: str
    role_reason: str
    confidence: str
    readable: bool
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MediaSummary:
    """Aggregierte Medienstatistik."""

    counts_per_category: dict[str, int]
    size_per_category: dict[str, int]
    formats: tuple[FormatCount, ...]
    extension_mismatches: int
    undetectable: int
    inspected: int
    missing_payloads: int
    unreadable_payloads: int


@dataclass(frozen=True, slots=True)
class AppReport:
    """Analyseergebnis fuer einen erkannten Messenger."""

    profile_slug: str
    profile_name: str
    detection: DetectionResult
    domains: tuple[DomainMatch, ...] = ()
    file_count: int = 0
    total_size: int = 0
    media: MediaSummary | None = None
    databases: tuple[DatabaseReport, ...] = ()
    decode_errors: int = 0


@dataclass(slots=True)
class AnalysisReport:
    """Vollstaendiges Ergebnis eines `analyze`-Laufs."""

    backup_path: Path
    generated_at: datetime
    backup: BackupInfo
    #: True, wenn die Manifest.db gelesen werden konnte.
    manifest_available: bool
    #: Grund, falls die Manifest.db nicht gelesen werden konnte.
    manifest_unavailable_reason: str | None = None
    manifest_schema: dict[str, TableSchema] = field(default_factory=dict)
    statistics: ManifestStatistics | None = None
    apps: tuple[AppReport, ...] = ()
    #: Alle Domains im Backup mit Eintragszahl - hilft beim Auffinden von Apps.
    all_domains: tuple[tuple[str, int], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_partial(self) -> bool:
        return not self.manifest_available


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

#: So viele Bytes werden pro Datei fuer die Signaturpruefung gelesen.
_PROBE_SIZE = media_module.HEADER_SIZE


class Analyzer:
    """Fuehrt die Analyse aus. Enthaelt keine Schreiboperationen."""

    def __init__(
        self,
        backup: AppleBackup,
        *,
        manifest_db: Path | None = None,
        app_slug: str | None = None,
        bundle_id: str | None = None,
        inspect_media: bool = True,
    ) -> None:
        """
        Args:
            manifest_db: Pfad zu einer bereits entschluesselten Manifest.db.
                Ohne Angabe wird die Datei im Backup verwendet - das
                funktioniert nur bei unverschluesselten Backups.
            app_slug: Nur dieses Profil pruefen. Ohne Angabe alle.
            bundle_id: Loest eine mehrdeutige Erkennung auf.
            inspect_media: Wenn False, werden keine Nutzdateien gelesen. Der
                Bericht enthaelt dann keine Formatstatistik.
        """
        self.backup = backup
        self.manifest_db = manifest_db or backup.manifest_db_path
        self.app_slug = app_slug
        self.bundle_id = bundle_id
        self.inspect_media = inspect_media

    def run(self) -> AnalysisReport:
        info = self.backup.info()
        warnings: list[str] = []

        report = AnalysisReport(
            backup_path=self.backup.path,
            generated_at=datetime.now(UTC),
            backup=info,
            manifest_available=False,
        )

        try:
            with ManifestReader(self.manifest_db) as reader:
                report.manifest_available = True
                report.manifest_schema = reader.schemas
                report.statistics = reader.statistics()
                report.all_domains = tuple(
                    sorted(report.statistics.entries_per_domain.items(), key=lambda kv: -kv[1])
                )
                report.apps = self._analyze_apps(info, reader, warnings)
        except NotASQLiteDatabase as error:
            report.manifest_unavailable_reason = str(error)
            if info.is_encrypted:
                warnings.append(
                    "Das Backup ist verschluesselt. Ohne Passwort sind nur die "
                    "Metadaten aus Info.plist und Manifest.plist auswertbar."
                )
            report.apps = self._analyze_apps_metadata_only(info)
        except ManifestSchemaError as error:
            raise AnalysisBlocked(
                str(error),
                diagnostics={
                    "manifest_tables": {
                        name: list(schema.columns) for name, schema in error.schemas.items()
                    }
                },
            ) from error

        if report.statistics and report.statistics.decode_errors:
            count = report.statistics.decode_errors
            warnings.append(
                f"{count} Manifest-{'Eintrag hat' if count == 1 else 'Eintraege haben'} "
                "unlesbare Metadaten. Sie werden weiterverarbeitet, aber ohne "
                "Groesse, Zeitstempel und Schluessel."
            )

        report.warnings = tuple(warnings)
        return report

    # -- App-Analyse --------------------------------------------------------

    def _profiles_to_check(self, info: BackupInfo) -> list[tuple[AppProfile, DetectionResult]]:
        if self.app_slug is not None:
            profile = get_profile(self.app_slug)
            return [(profile, profile.detect(info))]
        return list(detect_all(info))

    def _resolve_detection(self, detection: DetectionResult) -> DetectionResult:
        """Loest Mehrdeutigkeit auf, wenn der Nutzer `--bundle-id` gesetzt hat."""
        if detection.status is not DetectionStatus.AMBIGUOUS or self.bundle_id is None:
            return detection
        if self.bundle_id not in detection.candidates:
            raise AnalysisBlocked(
                f"--bundle-id {self.bundle_id} passt zu keinem gefundenen Kandidaten. "
                f"Gefunden wurden: {', '.join(detection.candidates)}."
            )
        return DetectionResult(
            app_name=detection.app_name,
            status=DetectionStatus.CONFIRMED,
            bundle_id=self.bundle_id,
            candidates=detection.candidates,
            reason="Aus mehreren Kandidaten durch --bundle-id ausgewaehlt.",
        )

    def _analyze_apps_metadata_only(self, info: BackupInfo) -> tuple[AppReport, ...]:
        """App-Erkennung ohne Manifest.db - funktioniert ohne Passwort."""
        reports: list[AppReport] = []
        for profile, raw in self._profiles_to_check(info):
            detection = self._resolve_detection(raw)
            if detection.status is DetectionStatus.NOT_FOUND and self.app_slug is None:
                continue
            reports.append(
                AppReport(
                    profile_slug=profile.slug,
                    profile_name=profile.name,
                    detection=detection,
                )
            )
        return tuple(reports)

    def _analyze_apps(
        self, info: BackupInfo, reader: ManifestReader, warnings: list[str]
    ) -> tuple[AppReport, ...]:
        available_domains = reader.domain_names()
        reports: list[AppReport] = []

        for profile, raw in self._profiles_to_check(info):
            detection = self._resolve_detection(raw)

            if detection.status is DetectionStatus.NOT_FOUND:
                if self.app_slug is not None:
                    reports.append(
                        AppReport(
                            profile_slug=profile.slug,
                            profile_name=profile.name,
                            detection=detection,
                        )
                    )
                continue

            if detection.status is DetectionStatus.AMBIGUOUS:
                warnings.append(
                    f"{profile.name}: {detection.reason} Bis zur Auswahl werden "
                    "keine Dateien zugeordnet."
                )
                reports.append(
                    AppReport(
                        profile_slug=profile.slug,
                        profile_name=profile.name,
                        detection=detection,
                    )
                )
                continue

            assert detection.bundle_id is not None
            domains = profile.match_domains(detection.bundle_id, available_domains)
            if not domains:
                warnings.append(
                    f"{profile.name} ist installiert, aber keine der "
                    f"{len(available_domains)} Domains im Backup gehoert dazu. "
                    "Moeglicherweise wurden die App-Daten nicht gesichert."
                )

            entries = tuple(reader.entries(domains=tuple(d.domain for d in domains)))
            files = tuple(entry for entry in entries if entry.kind is FileKind.FILE)

            domains = self._enrich_domains(domains, entries)
            media_summary = self._summarise_media(files) if self.inspect_media else None
            databases = self._analyze_databases(profile, files)

            reports.append(
                AppReport(
                    profile_slug=profile.slug,
                    profile_name=profile.name,
                    detection=detection,
                    domains=domains,
                    file_count=len(files),
                    total_size=sum(entry.size or 0 for entry in files),
                    media=media_summary,
                    databases=databases,
                    decode_errors=sum(1 for entry in entries if entry.decode_error),
                )
            )

        return tuple(reports)

    @staticmethod
    def _enrich_domains(
        domains: tuple[DomainMatch, ...], entries: tuple[ManifestEntry, ...]
    ) -> tuple[DomainMatch, ...]:
        counts: Counter[str] = Counter()
        sizes: Counter[str] = Counter()
        for entry in entries:
            counts[entry.domain] += 1
            if entry.kind is FileKind.FILE:
                sizes[entry.domain] += entry.size or 0
        return tuple(
            DomainMatch(
                domain=domain.domain,
                kind=domain.kind,
                file_count=counts[domain.domain],
                total_size=sizes[domain.domain],
            )
            for domain in domains
        )

    # -- Medien -------------------------------------------------------------

    def _probe(self, entry: ManifestEntry) -> tuple[MediaType | None, str | None]:
        """Liest den Kopf einer Nutzdatei und bestimmt den Typ.

        Gibt `(None, grund)` zurueck, wenn die Datei fehlt oder unlesbar ist -
        beides sind normale Befunde in echten Backups, kein Abbruchgrund.
        """
        path = self.backup.payload_path(entry.file_id)
        if not path.is_file():
            return None, "missing"
        try:
            with path.open("rb") as handle:
                header = handle.read(_PROBE_SIZE)
        except OSError as error:
            logger.debug("Nutzdatei %s nicht lesbar: %s", entry.file_id, type(error).__name__)
            return None, "unreadable"
        if entry.is_encrypted:
            # Verschluesselte Nutzdaten haben keine erkennbare Signatur; der Typ
            # laesst sich erst nach der Entschluesselung bestimmen.
            return None, "encrypted"
        return media_module.detect(header, filename=entry.relative_path), None

    def _summarise_media(self, files: tuple[ManifestEntry, ...]) -> MediaSummary:
        counts: Counter[str] = Counter()
        sizes: Counter[str] = Counter()
        format_counts: Counter[tuple[str, str | None, MediaCategory]] = Counter()
        format_sizes: Counter[tuple[str, str | None, MediaCategory]] = Counter()
        mismatches = undetectable = missing = unreadable = inspected = 0

        for entry in files:
            detected, reason = self._probe(entry)
            if detected is None:
                match reason:
                    case "missing":
                        missing += 1
                    case "unreadable":
                        unreadable += 1
                    case _:
                        undetectable += 1
                continue

            inspected += 1
            size = entry.size or 0
            counts[detected.category.value] += 1
            sizes[detected.category.value] += size
            key = (detected.format_name or "unbekannt", detected.mime_type, detected.category)
            format_counts[key] += 1
            format_sizes[key] += size
            if detected.extension_mismatch:
                mismatches += 1

        formats = tuple(
            FormatCount(
                format_name=name,
                mime_type=mime,
                category=category,
                count=count,
                total_size=format_sizes[(name, mime, category)],
            )
            for (name, mime, category), count in format_counts.most_common()
        )

        return MediaSummary(
            counts_per_category=dict(counts),
            size_per_category=dict(sizes),
            formats=formats,
            extension_mismatches=mismatches,
            undetectable=undetectable,
            inspected=inspected,
            missing_payloads=missing,
            unreadable_payloads=unreadable,
        )

    # -- Datenbanken --------------------------------------------------------

    def _analyze_databases(
        self, profile: AppProfile, files: tuple[ManifestEntry, ...]
    ) -> tuple[DatabaseReport, ...]:
        candidates: list[DatabaseCandidate] = []
        notes: dict[str, str] = {}
        readable: dict[str, bool] = {}

        for entry in files:
            path = self.backup.payload_path(entry.file_id)
            if not path.is_file() or entry.is_encrypted:
                continue
            detected, _ = self._probe(entry)
            if detected is None or detected.category is not MediaCategory.DATABASE:
                continue

            tables: tuple[str, ...] = ()
            try:
                with open_readonly(path) as connection:
                    tables = tuple(describe_database(connection, count_rows=False))
                readable[entry.file_id] = True
            except NotASQLiteDatabase as error:
                notes[entry.file_id] = str(error)
                readable[entry.file_id] = False
            candidates.append(DatabaseCandidate(entry=entry, tables=tables))

        roles: tuple[DatabaseRole, ...] = profile.classify_databases(tuple(candidates))

        return tuple(
            DatabaseReport(
                file_id=role.candidate.entry.file_id,
                domain=role.candidate.entry.domain,
                basename=role.candidate.entry.basename,
                size=role.candidate.entry.size,
                tables=role.candidate.tables,
                table_count=len(role.candidate.tables),
                role=role.role,
                role_reason=role.reason,
                confidence=role.confidence,
                readable=readable.get(role.candidate.entry.file_id, False),
                note=notes.get(role.candidate.entry.file_id),
            )
            for role in roles
        )
