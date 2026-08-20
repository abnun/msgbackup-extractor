"""Berichtsausgabe: Text fuer die Konsole, JSON fuer die Weiterverarbeitung.

Was hier **nicht** hineinkommt: Nachrichteninhalte, Kontaktnamen, vollstaendige
Dateipfade aus dem Backup. Der Bericht ist aggregiert. Von Datenbanken erscheint
nur der Dateiname, nicht der Pfad - der Name ist fuer die Diagnose noetig, der
Pfad enthaelt bei manchen Apps Chat- oder Kontaktbezeichner.

Die Textausgabe geht nach stdout, damit sie sich umleiten laesst; Logmeldungen
gehen nach stderr.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Any, Final

from msgbackup_extractor.analysis import AnalysisReport, AppReport
from msgbackup_extractor.models import DetectionStatus, TableSchema

INDENT = "    "


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------


def format_size(size: int | None) -> str:
    """Groesse in einer fuer Menschen lesbaren Einheit."""
    if size is None:
        return "unbekannt"
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PB"


def format_count(value: int) -> str:
    """Tausendertrennzeichen wie im deutschsprachigen Raum ueblich."""
    return f"{value:,}".replace(",", ".")


def format_timestamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "unbekannt"


def plural(count: int, singular: str, plural_form: str) -> str:
    """Zaehlwort mit korrekter Pluralform - der Bericht wird gelesen."""
    return f"{format_count(count)} {singular if count == 1 else plural_form}"


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "unbekannt"
    return "ja" if value else "nein"


class _TextWriter:
    """Kleiner Helfer fuer die eingerueckte Blockausgabe."""

    def __init__(self) -> None:
        self._buffer = StringIO()

    def title(self, text: str) -> None:
        self._buffer.write(f"{text}\n{'=' * len(text)}\n")

    def section(self, text: str) -> None:
        self._buffer.write(f"\n{text}\n{'-' * len(text)}\n")

    def field(self, label: str, value: object) -> None:
        self._buffer.write(f"\n{label}:\n{INDENT}{value}\n")

    def lines(self, label: str, values: list[str]) -> None:
        self._buffer.write(f"\n{label}:\n")
        if not values:
            self._buffer.write(f"{INDENT}(keine)\n")
            return
        for value in values:
            self._buffer.write(f"{INDENT}{value}\n")

    def blank(self) -> None:
        self._buffer.write("\n")

    def raw(self, text: str) -> None:
        self._buffer.write(text)

    def value(self) -> str:
        return self._buffer.getvalue()


# ---------------------------------------------------------------------------
# Analysebericht als Text
# ---------------------------------------------------------------------------


def render_analysis_text(report: AnalysisReport, *, verbose: bool = False) -> str:
    """Der Analysebericht als Klartext."""
    out = _TextWriter()
    out.title("Messenger Backup Analyzer")

    device = report.backup.device
    out.field("Backup", report.backup_path)
    out.field("Geraete-ID", report.backup.udid)
    out.field("Geraet", device.device_name or "unbekannt")
    out.field(
        "Modell / iOS",
        f"{device.product_type or 'unbekannt'} / {device.product_version or 'unbekannt'}",
    )
    out.field("Backup-Datum", format_timestamp(report.backup.backup_date))
    out.field("Vollstaendiges Backup", _yes_no(report.backup.is_full_backup))
    out.field("Verschluesselt", _yes_no(report.backup.is_encrypted))

    if report.backup.is_encrypted:
        out.field("Manifest.db verschluesselt", _yes_no(report.backup.has_manifest_key))

    out.field("Installierte Apps im Backup", format_count(len(report.backup.applications)))

    # -- Teilbericht --------------------------------------------------------
    if not report.manifest_available:
        out.section("Eingeschraenkte Analyse")
        out.raw(
            "\nDie Manifest.db konnte nicht gelesen werden. Datei- und\n"
            "Medienstatistiken fehlen daher; die Angaben oben und die\n"
            "App-Erkennung stammen aus Info.plist und Manifest.plist.\n"
        )
        if report.manifest_unavailable_reason:
            out.field("Grund", report.manifest_unavailable_reason)
    else:
        statistics = report.statistics
        assert statistics is not None
        out.section("Backup-Inhalt")
        out.field("Manifest-Eintraege", format_count(statistics.total_entries))
        out.field("Dateien", format_count(statistics.files))
        out.field("Verzeichnisse", format_count(statistics.directories))
        out.field("Symlinks", format_count(statistics.symlinks))
        out.field("Gesamtgroesse", format_size(statistics.total_size))
        out.field("Domains", format_count(len(statistics.entries_per_domain)))
        if statistics.encrypted_entries:
            out.field(
                "Verschluesselte Eintraege", format_count(statistics.encrypted_entries)
            )

    # -- Apps ---------------------------------------------------------------
    for app in report.apps:
        _render_app(out, app, verbose=verbose)

    if not report.apps:
        out.section("Messenger")
        out.raw("\nEs wurde kein unterstuetzter Messenger im Backup gefunden.\n")

    # -- Domains (nur verbose) ---------------------------------------------
    if verbose and report.all_domains:
        out.section("Alle Domains im Backup")
        out.lines(
            "Domain (Eintraege)",
            [
                f"{domain}  ({plural(count, 'Eintrag', 'Eintraege')})"
                for domain, count in report.all_domains[:50]
            ],
        )
        if len(report.all_domains) > 50:
            out.raw(f"{INDENT}... und {len(report.all_domains) - 50} weitere\n")

    # -- Warnungen ----------------------------------------------------------
    if report.warnings:
        out.section("Hinweise")
        for warning in report.warnings:
            out.raw(f"\n{INDENT}- {warning}\n")

    return out.value()


def _render_app(out: _TextWriter, app: AppReport, *, verbose: bool) -> None:
    out.section(f"Messenger: {app.profile_name}")

    detection = app.detection
    status_text = {
        DetectionStatus.CONFIRMED: "erkannt",
        DetectionStatus.AMBIGUOUS: "mehrdeutig",
        DetectionStatus.NOT_FOUND: "nicht gefunden",
    }[detection.status]
    out.field("Status", status_text)

    if detection.bundle_id:
        out.field("Bundle Identifier", detection.bundle_id)
    if detection.bundle_version:
        out.field("App-Version", detection.bundle_version)
    if len(detection.candidates) > 1:
        out.lines("Gefundene Varianten", list(detection.candidates))
    if detection.reason:
        out.field("Anmerkung", detection.reason)

    if detection.status is not DetectionStatus.CONFIRMED:
        return

    out.lines(
        "Domains",
        [
            f"{d.domain}  [{d.kind}]  {plural(d.file_count, 'Eintrag', 'Eintraege')}, "
            f"{format_size(d.total_size)}"
            for d in app.domains
        ],
    )
    out.field("Dateien", format_count(app.file_count))
    out.field("Gesamtgroesse", format_size(app.total_size))
    if app.decode_errors:
        out.field("Eintraege mit unlesbaren Metadaten", format_count(app.decode_errors))

    if app.media is not None:
        media = app.media
        labels = {
            "image": "Bilder",
            "video": "Videos",
            "audio": "Audio",
            "document": "Dokumente",
            "archive": "Archive",
            "database": "Datenbanken",
            "other": "Sonstige",
        }
        out.lines(
            "Medien nach Kategorie",
            [
                f"{labels.get(category, category):14} {format_count(count):>8}   "
                f"{format_size(media.size_per_category.get(category, 0))}"
                for category, count in sorted(
                    media.counts_per_category.items(), key=lambda kv: -kv[1]
                )
            ],
        )
        out.lines(
            "Gefundene Formate",
            [
                f"{fmt.format_name:16} {format_count(fmt.count):>8}   "
                f"{format_size(fmt.total_size):>10}   {fmt.mime_type or ''}"
                for fmt in media.formats
            ],
        )
        if media.extension_mismatches:
            out.field(
                "Dateien mit widerspruechlicher Endung",
                format_count(media.extension_mismatches),
            )
        if media.missing_payloads:
            out.field(
                "Eintraege ohne Nutzdatei im Backup", format_count(media.missing_payloads)
            )
        if media.unreadable_payloads:
            out.field("Unlesbare Nutzdateien", format_count(media.unreadable_payloads))
        if media.undecryptable:
            out.field(
                "Nicht entschluesselbar (Schluessel fehlt)",
                format_count(media.undecryptable),
            )
        if media.undetectable:
            out.field("Typ nicht bestimmbar", format_count(media.undetectable))

    out.lines(
        "SQLite-Datenbanken",
        [
            f"{db.basename}  ({format_size(db.size)}, "
            f"{plural(db.table_count, 'Tabelle', 'Tabellen')})  "
            f"Rolle: {db.role} [{db.confidence}]"
            for db in app.databases
        ],
    )
    if verbose:
        for db in app.databases:
            out.raw(f"\n{INDENT}{db.basename}: {db.role_reason}\n")
            if db.note:
                out.raw(f"{INDENT}{INDENT}Hinweis: {db.note}\n")
            if db.tables:
                out.raw(f"{INDENT}{INDENT}Tabellen: {', '.join(db.tables)}\n")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Serialisiert die im Bericht vorkommenden Sondertypen."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        # Schluesselmaterial darf nie im Bericht landen; nur die Laenge.
        return {"__bytes__": len(value)}
    if isinstance(value, tuple | set | frozenset):
        return list(value)
    raise TypeError(f"Nicht serialisierbar: {type(value).__name__}")


def _schema_to_dict(schema: TableSchema) -> dict[str, Any]:
    return {
        "name": schema.name,
        "columns": list(schema.columns),
        "column_types": schema.column_types,
        "primary_key": list(schema.primary_key),
        "foreign_keys": [list(fk) for fk in schema.foreign_keys],
        "row_count": schema.row_count,
    }


def analysis_to_dict(report: AnalysisReport, *, include_schema: bool = False) -> dict[str, Any]:
    """Der Analysebericht als JSON-taugliche Struktur.

    Enthaelt keine Schluessel, keine Inhalte und keine Backup-Dateipfade
    ausserhalb des Backup-Wurzelverzeichnisses.
    """
    payload: dict[str, Any] = {
        "tool": "msgbackup-extractor",
        "report_type": "analysis",
        "generated_at": report.generated_at,
        "backup": {
            "path": report.backup_path,
            "udid": report.backup.udid,
            "is_encrypted": report.backup.is_encrypted,
            "manifest_encrypted": report.backup.has_manifest_key,
            "backup_date": report.backup.backup_date,
            "is_full_backup": report.backup.is_full_backup,
            "was_passcode_set": report.backup.was_passcode_set,
            "manifest_version": report.backup.manifest_version,
            "device": {
                "name": report.backup.device.device_name,
                "product_type": report.backup.device.product_type,
                "product_version": report.backup.device.product_version,
                "build_version": report.backup.device.build_version,
                "last_backup_date": report.backup.device.last_backup_date,
            },
            "application_count": len(report.backup.applications),
        },
        "manifest": {
            "available": report.manifest_available,
            "unavailable_reason": report.manifest_unavailable_reason,
        },
        "is_partial": report.is_partial,
        "warnings": list(report.warnings),
    }

    if report.statistics is not None:
        statistics = report.statistics
        payload["statistics"] = {
            "total_entries": statistics.total_entries,
            "files": statistics.files,
            "directories": statistics.directories,
            "symlinks": statistics.symlinks,
            "total_size": statistics.total_size,
            "decode_errors": statistics.decode_errors,
            "encrypted_entries": statistics.encrypted_entries,
            "domain_count": len(statistics.entries_per_domain),
        }
        payload["domains"] = [
            {"domain": domain, "entries": count} for domain, count in report.all_domains
        ]

    if include_schema and report.manifest_schema:
        payload["manifest_schema"] = {
            name: _schema_to_dict(schema) for name, schema in report.manifest_schema.items()
        }

    payload["apps"] = [_app_to_dict(app) for app in report.apps]
    return payload


def _app_to_dict(app: AppReport) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": app.profile_slug,
        "name": app.profile_name,
        "detection": {
            "status": app.detection.status,
            "bundle_id": app.detection.bundle_id,
            "bundle_version": app.detection.bundle_version,
            "candidates": list(app.detection.candidates),
            "reason": app.detection.reason,
        },
        "domains": [
            {
                "domain": d.domain,
                "kind": d.kind,
                "entries": d.file_count,
                "total_size": d.total_size,
            }
            for d in app.domains
        ],
        "file_count": app.file_count,
        "total_size": app.total_size,
        "decode_errors": app.decode_errors,
        "databases": [
            {
                "file_id": db.file_id,
                "domain": db.domain,
                "basename": db.basename,
                "size": db.size,
                "table_count": db.table_count,
                "tables": list(db.tables),
                "role": db.role,
                "role_reason": db.role_reason,
                "confidence": db.confidence,
                "readable": db.readable,
                "note": db.note,
            }
            for db in app.databases
        ],
    }
    if app.media is not None:
        result["media"] = {
            "counts_per_category": app.media.counts_per_category,
            "size_per_category": app.media.size_per_category,
            "formats": [
                {
                    "format": fmt.format_name,
                    "mime_type": fmt.mime_type,
                    "category": fmt.category,
                    "count": fmt.count,
                    "total_size": fmt.total_size,
                }
                for fmt in app.media.formats
            ],
            "extension_mismatches": app.media.extension_mismatches,
            "inspected": app.media.inspected,
            "missing_payloads": app.media.missing_payloads,
            "unreadable_payloads": app.media.unreadable_payloads,
            "undecryptable": app.media.undecryptable,
            "undetectable": app.media.undetectable,
        }
    return result


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)


def write_json(payload: dict[str, Any], path: Path) -> None:
    """Schreibt einen Bericht als JSON. Legt fehlende Verzeichnisse an."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(payload) + "\n", encoding="utf-8")


def render_diagnostics_text(message: str, diagnostics: dict[str, Any]) -> str:
    """Diagnosebericht, wenn die Analyse nicht fortgesetzt werden kann."""
    out = _TextWriter()
    out.title("Diagnosebericht")
    out.field("Problem", message)
    for key, value in diagnostics.items():
        if isinstance(value, dict):
            out.lines(
                key,
                [f"{name}: {', '.join(map(str, columns))}" for name, columns in value.items()],
            )
        elif isinstance(value, list | tuple):
            out.lines(key, [str(item) for item in value])
        else:
            out.field(key, value)
    out.blank()
    out.raw(
        "Die Analyse wurde abgebrochen, statt ein Ergebnis auf Grundlage von\n"
        "Annahmen zu liefern. Bitte diesen Bericht der Fehlermeldung beilegen.\n"
    )
    return out.value()


# ---------------------------------------------------------------------------
# Extraktionsbericht
# ---------------------------------------------------------------------------

_CATEGORY_LABELS: Final[dict[str, str]] = {
    "image": "Bilder",
    "video": "Videos",
    "audio": "Audio",
    "document": "Dokumente",
    "archive": "Archive",
    "database": "Datenbanken",
    "other": "Sonstige",
}

_OUTCOME_LABELS: Final[dict[str, str]] = {
    "extracted": "Extrahiert",
    "skipped": "Uebersprungen",
    "duplicate": "Duplikate",
    "failed": "Fehlgeschlagen",
    "undecryptable": "Nicht entschluesselbar",
    "missing": "Quelle fehlt",
    "integrity_error": "Integritaetsfehler",
}


def render_dry_run_text(outcome: object) -> str:
    """Bericht eines Probelaufs. Es wird nichts geschrieben."""
    from msgbackup_extractor.extraction import ExtractionOutcome

    assert isinstance(outcome, ExtractionOutcome)
    plan = outcome.plan
    out = _TextWriter()
    out.title("Probelauf (es wird nichts geschrieben)")

    out.field("Messenger", f"{outcome.profile_name} ({outcome.detection.bundle_id})")
    out.field("Wuerde extrahieren", plural(plan.total_files, "Datei", "Dateien"))
    out.field("Datenmenge", format_size(plan.total_size))

    counts = plan.counts_per_category()
    sizes = plan.size_per_category()
    out.lines(
        "Nach Kategorie",
        [
            f"{_CATEGORY_LABELS.get(category, category):14} {format_count(count):>8}   "
            f"{format_size(sizes.get(category, 0))}"
            for category, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    )

    thumbnails = sum(1 for f in plan.files if f.item.is_thumbnail)
    if thumbnails:
        out.field("davon Vorschaubilder", format_count(thumbnails))

    assigned = sum(1 for f in plan.files if f.item.is_assigned)
    if outcome.domain_fallback:
        out.field(
            "Chat-Zuordnung",
            "nicht moeglich - Export erfolgt anhand der Domains",
        )
    else:
        out.field(
            "Chat-Zuordnung",
            f"{format_count(assigned)} von {format_count(plan.total_files)}, "
            f"{format_count(plan.total_files - assigned)} nach unassigned/",
        )

    if plan.missing:
        out.field("Quelle fehlt im Backup", plural(len(plan.missing), "Datei", "Dateien"))
    if plan.excluded:
        out.lines(
            "Bewusst ausgeschlossen",
            sorted({reason for _, reason in plan.excluded}),
        )

    out.blank()
    out.raw(
        "Ein Probelauf bildet keine Inhaltshashes - dafuer muesste er die Daten\n"
        "vollstaendig lesen und waere so teuer wie der echte Lauf. Duplikate\n"
        "werden daher erst beim echten Export erkannt.\n"
    )
    _render_notes(out, outcome)
    return out.value()


def render_extraction_text(outcome: object) -> str:
    """Abschlussbericht eines echten Laufs."""
    from msgbackup_extractor.extraction import ExtractionOutcome
    from msgbackup_extractor.models import FileOutcome

    assert isinstance(outcome, ExtractionOutcome)
    result = outcome.result
    out = _TextWriter()
    out.title("Extraktion abgeschlossen")

    out.field("Messenger", f"{outcome.profile_name} ({outcome.detection.bundle_id})")
    if result.output_dir is not None:
        out.field("Ausgabeverzeichnis", result.output_dir)

    rows = [
        (label, result.count(FileOutcome(value)))
        for value, label in _OUTCOME_LABELS.items()
    ]
    out.lines(
        "Ergebnis",
        [f"{label:24} {format_count(count):>8}" for label, count in rows if count],
    )
    out.field("Erfolgreich insgesamt", format_count(result.successful))
    out.field("Fehlgeschlagen insgesamt", format_count(result.failed))
    out.field("Integritaetsfehler", format_count(result.integrity_errors))
    out.field("Geschriebene Datenmenge", format_size(result.total_bytes))

    if result.integrity_errors:
        out.blank()
        out.raw(
            "FEHLER: Bei mindestens einer Datei weichen Quell- und Zielhash ab.\n"
            "Die betroffenen Eintraege stehen im Export-Manifest mit\n"
            '"integrity_ok": false. Diese Dateien sind nicht verwendbar.\n'
        )

    _render_notes(out, outcome)
    return out.value()


def _render_notes(out: _TextWriter, outcome: object) -> None:
    from msgbackup_extractor.extraction import ExtractionOutcome

    assert isinstance(outcome, ExtractionOutcome)
    if outcome.dangling_references:
        out.field(
            "Verweise ohne Datei im Backup",
            format_count(outcome.dangling_references),
        )
    if outcome.notes:
        out.section("Hinweise")
        for note in outcome.notes:
            out.raw(f"\n{INDENT}- {note}\n")


# ---------------------------------------------------------------------------
# Pruefbericht
# ---------------------------------------------------------------------------


def render_verify_text(result: object) -> str:
    """Bericht der nachtraeglichen Integritaetspruefung."""
    from msgbackup_extractor.extract.verify import VerifyResult, VerifyStatus

    assert isinstance(result, VerifyResult)
    out = _TextWriter()
    out.title("Integritaetspruefung")

    out.field("Manifest", result.manifest.path)
    out.field("Messenger", result.manifest.app or "unbekannt")
    out.field("Erstellt", result.manifest.generated_at or "unbekannt")
    out.field("Geprueft", plural(result.checked, "Datei", "Dateien"))

    labels = {
        VerifyStatus.OK: "In Ordnung",
        VerifyStatus.MISSING: "Datei fehlt",
        VerifyStatus.SIZE_MISMATCH: "Groesse weicht ab",
        VerifyStatus.HASH_MISMATCH: "Hash weicht ab",
        VerifyStatus.UNREADABLE: "Nicht lesbar",
        VerifyStatus.LINK_MISSING: "Verknuepfung fehlt",
        VerifyStatus.SKIPPED: "Ohne erwartete Datei",
    }
    out.lines(
        "Ergebnis",
        [
            f"{label:24} {format_count(result.count(status)):>8}"
            for status, label in labels.items()
            if result.count(status)
        ],
    )

    if result.is_intact:
        out.blank()
        out.raw("Der Export ist unveraendert und vollstaendig.\n")
        return out.value()

    out.section("Beanstandungen")
    for problem in result.problems[:50]:
        out.raw(f"\n{INDENT}{labels[problem.status]}: {problem.output_path}\n")
        if problem.status is VerifyStatus.HASH_MISMATCH:
            out.raw(f"{INDENT}{INDENT}erwartet: {problem.expected_sha256}\n")
            out.raw(f"{INDENT}{INDENT}gefunden: {problem.actual_sha256}\n")
        elif problem.status is VerifyStatus.SIZE_MISMATCH:
            out.raw(
                f"{INDENT}{INDENT}erwartet: {problem.expected_size} Byte, "
                f"gefunden: {problem.actual_size} Byte\n"
            )
        elif problem.missing_links:
            for link in problem.missing_links:
                out.raw(f"{INDENT}{INDENT}fehlende Verknuepfung: {link}\n")
    if len(result.problems) > 50:
        out.raw(f"\n{INDENT}... und {len(result.problems) - 50} weitere\n")
    return out.value()


def verify_to_dict(result: object) -> dict[str, Any]:
    """Pruefbericht als JSON-taugliche Struktur."""
    from msgbackup_extractor.extract.verify import VerifyResult, VerifyStatus

    assert isinstance(result, VerifyResult)
    return {
        "tool": "msgbackup-extractor",
        "report_type": "verification",
        "manifest": str(result.manifest.path),
        "app": result.manifest.app,
        "backup_udid": result.manifest.backup_udid,
        "checked": result.checked,
        "intact": result.is_intact,
        "counts": {
            status.value: result.count(status)
            for status in VerifyStatus
            if result.count(status)
        },
        "problems": [
            {
                "output_path": problem.output_path,
                "status": problem.status.value,
                "expected_sha256": problem.expected_sha256,
                "actual_sha256": problem.actual_sha256,
                "expected_size": problem.expected_size,
                "actual_size": problem.actual_size,
                "missing_links": list(problem.missing_links),
            }
            for problem in result.problems
        ],
    }
