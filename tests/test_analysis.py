"""Tests fuer die Analyse-Orchestrierung und die Berichtsausgabe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from msgbackup_extractor.analysis import AnalysisBlocked, AnalysisReport, Analyzer
from msgbackup_extractor.core import reports
from msgbackup_extractor.models import DetectionStatus, MediaCategory
from tests.conftest import (
    PNG,
    TEST_PASSWORD,
    THREEMA_BUNDLE_ID,
    analysis_session,
    analyze,
    sample_files,
)
from tests.support.backup_builder import (
    UNKNOWN_SCHEMA,
    BackupFile,
    BuiltBackup,
    build_backup,
)


def _run(backup: BuiltBackup, **kwargs: object) -> AnalysisReport:
    """Analyse ohne Passwort - bei verschluesselten Backups also ein Teilbericht."""
    return analyze(backup, **kwargs)


# ---------------------------------------------------------------------------
# Vollstaendige Analyse
# ---------------------------------------------------------------------------


def test_analysis_of_plain_backup_is_complete(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    assert report.manifest_available
    assert not report.is_partial
    assert report.statistics is not None
    assert report.statistics.total_entries == len(plain_backup.files)


def test_threema_is_reported_with_domains(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.detection.status is DetectionStatus.CONFIRMED
    assert app.detection.bundle_id == THREEMA_BUNDLE_ID
    assert {d.kind for d in app.domains} == {"app", "group"}
    assert app.file_count > 0
    assert app.total_size > 0


def test_foreign_app_files_are_not_counted(plain_backup: BuiltBackup) -> None:
    """Karten-App und HomeDomain duerfen nicht zu Threema gezaehlt werden."""
    report = _run(plain_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    threema_files = [
        f for f in plain_backup.files if "threema" in f.domain and f.flags == 1
    ]
    assert app.file_count == len(threema_files)


def test_media_summary_counts_categories(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    media = next(a for a in report.apps if a.profile_slug == "threema").media
    assert media is not None
    assert media.counts_per_category[MediaCategory.IMAGE.value] >= 3
    assert media.counts_per_category[MediaCategory.VIDEO.value] >= 1
    assert media.counts_per_category[MediaCategory.AUDIO.value] >= 1
    assert media.counts_per_category[MediaCategory.DOCUMENT.value] >= 1


def test_formats_are_reported_dynamically(plain_backup: BuiltBackup) -> None:
    """Die Formatliste kommt aus den Funden, nicht aus einer festen Liste."""
    report = _run(plain_backup)
    media = next(a for a in report.apps if a.profile_slug == "threema").media
    assert media is not None
    names = {fmt.format_name for fmt in media.formats}
    assert {"JPEG", "PNG", "HEIC", "MP4", "M4A", "PDF"} <= names
    assert sum(fmt.count for fmt in media.formats) == media.inspected


def test_extension_mismatch_is_counted(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    media = next(a for a in report.apps if a.profile_slug == "threema").media
    assert media is not None and media.extension_mismatches == 1


def test_missing_payload_is_counted_not_fatal(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    media = next(a for a in report.apps if a.profile_slug == "threema").media
    assert media is not None and media.missing_payloads == 1


def test_decode_errors_produce_a_warning(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    assert any("unlesbare Metadaten" in warning for warning in report.warnings)


def test_databases_are_classified(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    core_data = next(db for db in app.databases if db.basename == "ThreemaData.sqlite")
    assert core_data.role == "messages"
    assert core_data.confidence == "high"
    assert core_data.readable
    assert "Z_METADATA" in core_data.tables


def test_unreadable_database_is_reported_honestly(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    broken = next(db for db in app.databases if db.basename == "kaputt.sqlite")
    assert not broken.readable
    assert broken.role == "unknown"
    assert broken.note


def test_no_media_inspection_skips_reading_payloads(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup, inspect_media=False)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.media is None
    assert app.file_count > 0


# ---------------------------------------------------------------------------
# Verschluesselte Backups: Teilbericht
# ---------------------------------------------------------------------------


def test_encrypted_backup_yields_partial_report(encrypted_backup: BuiltBackup) -> None:
    report = _run(encrypted_backup)
    assert report.is_partial
    assert not report.manifest_available
    assert report.manifest_unavailable_reason is not None
    assert report.statistics is None


def test_partial_report_still_detects_the_app(encrypted_backup: BuiltBackup) -> None:
    """Die App-Erkennung braucht kein Passwort - das ist der Sinn des Teilberichts."""
    report = _run(encrypted_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.detection.status is DetectionStatus.CONFIRMED
    assert app.detection.bundle_id == THREEMA_BUNDLE_ID
    assert app.detection.bundle_version == "6.1.2"


def test_partial_report_warns_about_the_password(encrypted_backup: BuiltBackup) -> None:
    report = _run(encrypted_backup)
    assert any("Passwort" in warning for warning in report.warnings)


def test_partial_report_has_no_file_statistics(encrypted_backup: BuiltBackup) -> None:
    report = _run(encrypted_backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.file_count == 0
    assert app.domains == ()
    assert app.media is None


# ---------------------------------------------------------------------------
# Nicht raten: Diagnose statt Ergebnis
# ---------------------------------------------------------------------------


def test_unknown_manifest_schema_blocks_with_diagnostics(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b", [], schema=UNKNOWN_SCHEMA, installed_applications=[THREEMA_BUNDLE_ID]
    )
    with pytest.raises(AnalysisBlocked) as error:
        analyze(backup)
    assert "keine Dateitabelle" in str(error.value)
    assert "manifest_tables" in error.value.diagnostics


def test_ambiguous_detection_yields_warning_not_a_choice(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-ch.threema.iapp", "Documents/a.png", PNG)],
        installed_applications=["ch.threema.iapp", "ch.threema.work.iapp"],
    )
    report = analyze(backup)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.detection.status is DetectionStatus.AMBIGUOUS
    assert app.domains == ()
    assert app.file_count == 0
    assert any("mehrdeutig" in w.lower() or "Mehrere" in w for w in report.warnings)


def test_bundle_id_resolves_ambiguity(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-ch.threema.iapp", "Documents/a.png", PNG)],
        installed_applications=["ch.threema.iapp", "ch.threema.work.iapp"],
    )
    report = analyze(backup, bundle_id="ch.threema.iapp")
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.detection.status is DetectionStatus.CONFIRMED
    assert app.detection.bundle_id == "ch.threema.iapp"
    assert app.file_count == 1


def test_wrong_bundle_id_is_rejected(tmp_path: Path) -> None:
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("AppDomain-ch.threema.iapp", "Documents/a.png", PNG)],
        installed_applications=["ch.threema.iapp", "ch.threema.work.iapp"],
    )
    with pytest.raises(AnalysisBlocked, match="passt zu keinem"):
        analyze(backup, bundle_id="ch.threema.gibt-es-nicht")


def test_app_filter_reports_a_miss_explicitly(backup_without_threema: BuiltBackup) -> None:
    report = analyze(backup_without_threema, app_slug="threema")
    app = report.apps[0]
    assert app.detection.status is DetectionStatus.NOT_FOUND


def test_no_apps_reported_when_none_detected(backup_without_threema: BuiltBackup) -> None:
    report = analyze(backup_without_threema)
    assert report.apps == ()


def test_installed_app_without_domains_warns(tmp_path: Path) -> None:
    """App installiert, aber keine Daten gesichert - das muss auffallen."""
    backup = build_backup(
        tmp_path / "b",
        [BackupFile("HomeDomain", "Library/a.plist", b"bplist00")],
        installed_applications=[THREEMA_BUNDLE_ID],
    )
    report = analyze(backup)
    assert any("keine der" in warning for warning in report.warnings)


# ---------------------------------------------------------------------------
# Berichtsausgabe
# ---------------------------------------------------------------------------


def test_text_report_contains_key_facts(plain_backup: BuiltBackup) -> None:
    text = reports.render_analysis_text(_run(plain_backup))
    for expected in ("Messenger Backup Analyzer", "Threema", THREEMA_BUNDLE_ID, "Bilder"):
        assert expected in text


def test_text_report_contains_no_file_paths(plain_backup: BuiltBackup) -> None:
    """Der Bericht ist aggregiert: Domains ja, Dateipfade nein."""
    text = reports.render_analysis_text(_run(plain_backup))
    assert "Documents/img/photo1.jpg" not in text
    assert "photo1.jpg" not in text
    # Datenbanknamen sind fuer die Diagnose noetig und enthalten keine Kontakte.
    assert "ThreemaData.sqlite" in text


def test_verbose_report_adds_schema_details(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    plain = reports.render_analysis_text(report, verbose=False)
    verbose = reports.render_analysis_text(report, verbose=True)
    assert len(verbose) > len(plain)
    assert "Z_METADATA" in verbose
    assert "Alle Domains im Backup" in verbose


def test_partial_report_explains_the_limitation(encrypted_backup: BuiltBackup) -> None:
    text = reports.render_analysis_text(_run(encrypted_backup))
    assert "Eingeschraenkte Analyse" in text
    assert "Manifest.db" in text


def test_json_report_is_valid_and_serialisable(plain_backup: BuiltBackup) -> None:
    payload = reports.analysis_to_dict(_run(plain_backup))
    parsed = json.loads(reports.to_json(payload))
    assert parsed["report_type"] == "analysis"
    assert parsed["apps"][0]["detection"]["bundle_id"] == THREEMA_BUNDLE_ID
    assert parsed["statistics"]["files"] > 0


def test_json_report_contains_no_key_material(plain_backup: BuiltBackup) -> None:
    backup = build_backup(
        plain_backup.path.parent / "enc-json",
        sample_files(),
        password="pw",
        encrypt_manifest=False,
        installed_applications=[THREEMA_BUNDLE_ID],
    )
    payload = reports.analysis_to_dict(analyze(backup))
    text = reports.to_json(payload)
    assert "encryption_key" not in text
    assert "\\u0000" not in text


def test_json_schema_is_optional(plain_backup: BuiltBackup) -> None:
    report = _run(plain_backup)
    assert "manifest_schema" not in reports.analysis_to_dict(report)
    with_schema = reports.analysis_to_dict(report, include_schema=True)
    assert "Files" in with_schema["manifest_schema"]


def test_write_json_creates_parent_directories(plain_backup: BuiltBackup,
                                               tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "bericht.json"
    reports.write_json(reports.analysis_to_dict(_run(plain_backup)), target)
    assert json.loads(target.read_text(encoding="utf-8"))["report_type"] == "analysis"


def test_diagnostics_report_explains_the_abort() -> None:
    text = reports.render_diagnostics_text(
        "Keine Dateitabelle gefunden", {"manifest_tables": {"Foo": ["id", "payload"]}}
    )
    assert "Diagnosebericht" in text
    assert "Foo: id, payload" in text
    assert "abgebrochen" in text


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1000, "1.00 KB"),
        (1_500_000, "1.50 MB"),
        (4_820_000_000, "4.82 GB"),
        (None, "unbekannt"),
    ],
)
def test_format_size(size: int | None, expected: str) -> None:
    assert reports.format_size(size) == expected


def test_format_count_uses_thousands_separator() -> None:
    assert reports.format_count(12438) == "12.438"


def test_plural_forms() -> None:
    assert reports.plural(1, "Eintrag", "Eintraege") == "1 Eintrag"
    assert reports.plural(2, "Eintrag", "Eintraege") == "2 Eintraege"


# ---------------------------------------------------------------------------
# Verschluesselte Backups mit Passwort: vollstaendiger Bericht
# ---------------------------------------------------------------------------


def test_encrypted_backup_with_password_is_complete(encrypted_backup: BuiltBackup) -> None:
    report = analyze(encrypted_backup, password=TEST_PASSWORD)
    assert report.manifest_available
    assert not report.is_partial
    assert report.statistics is not None
    assert report.statistics.total_entries == len(encrypted_backup.files)
    assert report.statistics.encrypted_entries > 0


def test_encrypted_and_plain_reports_agree(
    plain_backup: BuiltBackup, encrypted_backup: BuiltBackup
) -> None:
    """Dasselbe Backup verschluesselt und unverschluesselt muss gleich aussehen.

    Das ist der scharfe Test fuer die Entschluesselung: Kategorien, Formate und
    Groessen muessen uebereinstimmen, obwohl der eine Lauf durch AES gehen musste.

    Genau eine Datei weicht ab, und zwar begruendet: das Fixture enthaelt einen
    Eintrag mit absichtlich unlesbarem MBFile-Blob. Ohne Blob gibt es keinen
    Dateischluessel, also ist der Inhalt im verschluesselten Backup Chiffrat und
    sein Typ nicht bestimmbar. Er wird als "nicht entschluesselbar" gezaehlt -
    nicht geraten.
    """
    plain = analyze(plain_backup)
    encrypted = analyze(encrypted_backup, password=TEST_PASSWORD)

    plain_app = next(a for a in plain.apps if a.profile_slug == "threema")
    encrypted_app = next(a for a in encrypted.apps if a.profile_slug == "threema")

    assert encrypted_app.file_count == plain_app.file_count
    assert encrypted_app.total_size == plain_app.total_size
    assert encrypted_app.media is not None and plain_app.media is not None

    # Die eine unbestimmbare Datei aus dem Vergleich herausrechnen.
    assert encrypted_app.media.undecryptable == 1
    assert encrypted_app.media.inspected == plain_app.media.inspected - 1

    corrupted = plain_backup.file_by_path("Documents/kaputte-metadaten.jpg")
    assert corrupted.corrupt_metadata, "Der Test haengt an diesem Fixture-Merkmal"

    expected_counts = dict(plain_app.media.counts_per_category)
    expected_counts["image"] -= 1
    assert encrypted_app.media.counts_per_category == expected_counts

    expected_formats = {
        (f.format_name, f.count - 1 if f.format_name == "JPEG" else f.count)
        for f in plain_app.media.formats
    }
    assert {
        (f.format_name, f.count) for f in encrypted_app.media.formats
    } == expected_formats
    assert encrypted_app.media.extension_mismatches == plain_app.media.extension_mismatches


def test_encrypted_databases_are_classified(encrypted_backup: BuiltBackup) -> None:
    """Auch verschluesselte Datenbanken muessen klassifizierbar sein."""
    with analysis_session(encrypted_backup, password=TEST_PASSWORD) as session:
        report = Analyzer(session).run()
        app = next(a for a in report.apps if a.profile_slug == "threema")
        core_data = next(db for db in app.databases if db.basename == "ThreemaData.sqlite")
        assert core_data.readable
        assert core_data.role == "messages"
        assert "Z_METADATA" in core_data.tables
        # Die lesbare Kopie liegt im Arbeitsverzeichnis, nicht im Backup.
        assert core_data.readable_path is not None
        assert encrypted_backup.path not in core_data.readable_path.parents


def test_media_detection_works_through_encryption(encrypted_backup: BuiltBackup) -> None:
    report = analyze(encrypted_backup, password=TEST_PASSWORD)
    media = next(a for a in report.apps if a.profile_slug == "threema").media
    assert media is not None
    assert {"JPEG", "PNG", "HEIC", "MP4", "M4A", "PDF"} <= {
        fmt.format_name for fmt in media.formats
    }
    # Genau der Eintrag mit dem absichtlich kaputten MBFile-Blob.
    assert media.undecryptable == 1


def test_truncated_encrypted_file_is_counted_not_fatal(
    encrypted_backup: BuiltBackup,
) -> None:
    """Die abgeschnittene Datei im Fixture darf den Lauf nicht abbrechen."""
    report = analyze(encrypted_backup, password=TEST_PASSWORD)
    app = next(a for a in report.apps if a.profile_slug == "threema")
    assert app.file_count > 0
    assert app.media is not None
