"""Kommandozeilenschnittstelle.

Enthaelt bewusst keine Businesslogik: die Unterbefehle validieren Argumente,
rufen die zustaendigen Module und geben Berichte aus. Berichte gehen nach
stdout, Logmeldungen nach stderr - so bleibt `--json` umleitbar.

Passwoerter werden ausschliesslich interaktiv erfragt. Es gibt bewusst kein
Argument dafuer.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from msgbackup_extractor import __version__
from msgbackup_extractor.analysis import AnalysisBlocked, AnalysisReport, Analyzer
from msgbackup_extractor.apps.registry import profile_slugs
from msgbackup_extractor.apps.registry import profile_slugs as _profile_slugs  # noqa: F401
from msgbackup_extractor.core import reports
from msgbackup_extractor.core.backup import (
    AppleBackup,
    BackupAccessError,
    NotABackupError,
    default_backup_root,
    list_local_backups,
)
from msgbackup_extractor.core.encryption import DecryptionError, WrongPasswordError
from msgbackup_extractor.core.logging_setup import configure_logging
from msgbackup_extractor.core.manifest import ManifestSchemaError
from msgbackup_extractor.core.paths import (
    CloudSyncedPathError,
    OutputGuardError,
    require_non_cloud_path,
)
from msgbackup_extractor.core.session import BackupSession, interactive_password
from msgbackup_extractor.core.sqlite_ro import (
    NotASQLiteDatabase,
    describe_database,
    open_readonly,
)
from msgbackup_extractor.extract import export_manifest
from msgbackup_extractor.extract.export_manifest import InvalidManifest
from msgbackup_extractor.extract.planner import ExtractOptions
from msgbackup_extractor.extract.verify import verify as verify_export
from msgbackup_extractor.extraction import ExtractionBlocked, Extractor
from msgbackup_extractor.models import MediaCategory

PROGRAM = "msgx"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ERROR = 3
EXIT_DIAGNOSTICS = 4
EXIT_NOT_IMPLEMENTED = 5


# ---------------------------------------------------------------------------
# Argumentparser
# ---------------------------------------------------------------------------


def _add_backup_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backup",
        type=Path,
        metavar="PFAD",
        help=(
            "Verzeichnis des Backups (das mit der Geraete-ID). Ohne Angabe "
            "werden die Backups am Standardort aufgelistet."
        ),
    )


def _add_app_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app",
        choices=profile_slugs(),
        help="Nur diesen Messenger pruefen. Ohne Angabe werden alle geprueft.",
    )
    parser.add_argument(
        "--bundle-id",
        metavar="ID",
        help="Loest eine mehrdeutige Erkennung auf, wenn mehrere Varianten gefunden wurden.",
    )


def _add_password_argument(parser: argparse.ArgumentParser) -> None:
    """Es gibt bewusst nur einen Schalter, kein Passwort-Argument."""
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "Fragt bei einem verschluesselten Backup nicht nach dem Passwort. "
            "Der Bericht beschraenkt sich dann auf die unverschluesselten "
            "Metadaten aus Info.plist und Manifest.plist."
        ),
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Technische Zusatzinformationen. Gibt auch dann keine "
            "Nachrichteninhalte aus."
        ),
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help=(
            "Zeigt Dateipfade im Klartext an. Standardmaessig werden sie "
            "maskiert, weil sie Kontakt- und Chatnamen enthalten koennen."
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PFAD",
        dest="json_path",
        help="Schreibt den Bericht zusaetzlich als JSON in diese Datei.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Extrahiert Messenger-Daten aus einem lokalen Apple-iPhone-Backup. "
            "Arbeitet ausschliesslich lokal und liest das Backup nur."
        ),
        epilog=(
            "Das Backup wird niemals veraendert. Geschrieben wird ausschliesslich "
            "in das mit --output angegebene Verzeichnis."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="BEFEHL")

    analyze = subparsers.add_parser(
        "analyze",
        help="Backup analysieren (read-only, extrahiert nichts)",
        description=(
            "Untersucht das Backup und berichtet, was vorhanden ist. Es werden "
            "keine Dateien geschrieben und keine Daten extrahiert."
        ),
    )
    _add_backup_argument(analyze)
    _add_app_arguments(analyze)
    _add_password_argument(analyze)
    _add_common_arguments(analyze)
    analyze.add_argument(
        "--no-media-inspection",
        action="store_true",
        help=(
            "Liest keine Nutzdateien. Schneller, aber ohne Formatstatistik "
            "(die Typerkennung braucht die Dateikoepfe)."
        ),
    )
    analyze.add_argument(
        "--include-schema",
        action="store_true",
        help="Nimmt das vollstaendige Manifest-Schema in den JSON-Bericht auf.",
    )
    analyze.set_defaults(handler=_command_analyze)

    database = subparsers.add_parser(
        "database",
        help="Schemata der gefundenen App-Datenbanken ausgeben",
        description=(
            "Oeffnet die gefundenen SQLite-Datenbanken strikt lesend und gibt "
            "deren Schema aus. Es werden keine Inhalte ausgegeben."
        ),
    )
    _add_backup_argument(database)
    _add_app_arguments(database)
    _add_password_argument(database)
    _add_common_arguments(database)
    database.set_defaults(handler=_command_database)

    backups = subparsers.add_parser(
        "backups",
        help="Lokale Backups am Standardort auflisten",
    )
    backups.add_argument(
        "--root",
        type=Path,
        metavar="PFAD",
        help="Anderes Wurzelverzeichnis als der Standardort.",
    )
    backups.set_defaults(handler=_command_backups)

    extract = subparsers.add_parser(
        "extract",
        help="Messenger-Dateien in ein Ausgabeverzeichnis extrahieren",
        description=(
            "Extrahiert die Dateien der erkannten App. Das Backup wird nur "
            "gelesen; geschrieben wird ausschliesslich in --output."
        ),
    )
    _add_backup_argument(extract)
    _add_app_arguments(extract)
    _add_password_argument(extract)
    _add_common_arguments(extract)
    extract.add_argument(
        "--output",
        type=Path,
        metavar="PFAD",
        required=True,
        help="Ausgabeverzeichnis. Muss ausserhalb des Backups liegen.",
    )
    extract.add_argument(
        "--dry-run",
        action="store_true",
        help="Zeigt an, was extrahiert wuerde, und schreibt nichts.",
    )
    extract.add_argument(
        "--organize-by-chat",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Legt zusaetzlich eine Struktur nach Chat an. Standardmaessig an; "
            "die Dateien werden per Hardlink geteilt und belegen keinen "
            "zusaetzlichen Speicher."
        ),
    )
    extract.add_argument(
        "--hardlinks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Teilt Dateien zwischen media/ und chats/ per Hardlink. Mit "
            "--no-hardlinks werden Kopien angelegt (doppelter Speicherbedarf)."
        ),
    )
    extract.add_argument(
        "--thumbnails",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Exportiert auch die von der App gespeicherten Vorschaubilder nach "
            "media/thumbnails/. Nuetzlich fuer eine spaetere Galerieansicht."
        ),
    )
    extract.add_argument(
        "--deduplicate",
        action="store_true",
        help=(
            "Schreibt inhaltsgleiche Dateien nur einmal. Ohne diese Option "
            "werden Duplikate exportiert und im Manifest markiert."
        ),
    )
    extract.add_argument(
        "--types",
        metavar="LISTE",
        help=(
            "Nur diese Kategorien exportieren, kommagetrennt. Moeglich: "
            + ", ".join(c.value for c in MediaCategory)
        ),
    )
    extract.add_argument(
        "--allow-cloud-output",
        action="store_true",
        help=(
            "Erlaubt ein Ausgabeverzeichnis in einem Cloud-Sync-Ordner. "
            "Die Daten werden dann vom Betriebssystem hochgeladen."
        ),
    )
    extract.set_defaults(handler=_command_extract)

    verify = subparsers.add_parser(
        "verify",
        help="Einen Export anhand seines Manifests pruefen",
        description=(
            "Prueft Vorhandensein, Groesse und SHA-256 jeder exportierten Datei. "
            "Das Backup wird dafuer nicht benoetigt."
        ),
    )
    verify.add_argument(
        "--manifest",
        type=Path,
        metavar="PFAD",
        required=True,
        help="Pfad zu export-manifest.json oder zum Ausgabeverzeichnis.",
    )
    verify.add_argument("--verbose", action="store_true", help="Technische Details.")
    verify.add_argument(
        "--json", type=Path, metavar="PFAD", dest="json_path",
        help="Schreibt den Pruefbericht zusaetzlich als JSON.",
    )
    verify.set_defaults(handler=_command_verify)

    return parser


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


def _resolve_backup(arguments: argparse.Namespace) -> AppleBackup:
    """Oeffnet das Backup oder erklaert, wie es zu finden ist."""
    if arguments.backup is None:
        raise SystemExit(_report_available_backups())

    path = arguments.backup.expanduser()
    try:
        require_non_cloud_path(path, purpose="Backup-Verzeichnis", allow=True)
        return AppleBackup(path)
    except (NotABackupError, BackupAccessError) as error:
        print(f"Fehler: {error}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from error


def _open_session(
    backup: AppleBackup, arguments: argparse.Namespace
) -> BackupSession:
    """Oeffnet eine Session und fragt das Passwort nur, wenn es gebraucht wird."""
    metadata_only = getattr(arguments, "metadata_only", False)
    provider = None if metadata_only else interactive_password
    if backup.is_encrypted and not metadata_only:
        print(
            "Das Backup ist verschluesselt. Das Passwort wird nicht gespeichert "
            "und erscheint in keiner Ausgabe.",
            file=sys.stderr,
        )
    return BackupSession(backup, password_provider=provider)


def _report_available_backups(root: Path | None = None) -> int:
    """Listet gefundene Backups auf. Rueckgabewert ist der Exitcode."""
    directory = root or default_backup_root()
    try:
        found = list_local_backups(directory)
    except BackupAccessError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR

    if not found:
        print(f"Keine Backups gefunden unter:\n    {directory}", file=sys.stderr)
        print(
            "\nEin lokales Backup erstellst du im Finder: iPhone anschliessen, "
            "in der Seitenleiste auswaehlen, \n"
            '"Sichere alle Daten des iPhone auf diesem Mac" aktivieren, '
            "Verschluesselung einschalten,\ndann \"Jetzt sichern\".",
            file=sys.stderr,
        )
        return EXIT_ERROR

    print("Gefundene Backups (neueste zuerst):\n")
    for path in found:
        try:
            backup = AppleBackup(path)
            device = backup.device_info()
            label = device.device_name or "unbekanntes Geraet"
            encrypted = "verschluesselt" if backup.is_encrypted else "unverschluesselt"
            print(f"    {path.name}")
            print(f"        {label}, iOS {device.product_version or '?'}, {encrypted}")
        except (NotABackupError, BackupAccessError):
            print(f"    {path.name}\n        (nicht lesbar)")
    print("\nVerwende den Pfad mit --backup.")
    return EXIT_OK


def _emit(text: str, payload: dict[str, object] | None, json_path: Path | None) -> None:
    """Gibt den Bericht aus und schreibt optional JSON."""
    print(text)
    if json_path is not None and payload is not None:
        target = json_path.expanduser()
        reports.write_json(payload, target)
        print(f"JSON-Bericht geschrieben: {target}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Unterbefehle
# ---------------------------------------------------------------------------


def _command_backups(arguments: argparse.Namespace) -> int:
    return _report_available_backups(arguments.root)


def _command_analyze(arguments: argparse.Namespace) -> int:
    backup = _resolve_backup(arguments)
    try:
        with _open_session(backup, arguments) as session:
            report = Analyzer(
                session,
                app_slug=arguments.app,
                bundle_id=arguments.bundle_id,
                inspect_media=not arguments.no_media_inspection,
            ).run()
            text = reports.render_analysis_text(report, verbose=arguments.verbose)
            payload = reports.analysis_to_dict(
                report, include_schema=arguments.include_schema
            )
    except WrongPasswordError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR
    except DecryptionError as error:
        print(f"Fehler bei der Entschluesselung: {error}", file=sys.stderr)
        return EXIT_ERROR
    except (AnalysisBlocked, ManifestSchemaError) as error:
        diagnostics = getattr(error, "diagnostics", {})
        print(reports.render_diagnostics_text(str(error), diagnostics), file=sys.stderr)
        return EXIT_DIAGNOSTICS

    _emit(text, payload, arguments.json_path)

    if report.is_partial and report.backup.is_encrypted:
        print(
            "\nHinweis: Dies ist ein Teilbericht. Fuer die vollstaendige Analyse "
            "das Backup-Passwort eingeben (also ohne --metadata-only aufrufen).",
            file=sys.stderr,
        )
    return EXIT_OK


def _command_database(arguments: argparse.Namespace) -> int:
    backup = _resolve_backup(arguments)
    try:
        with _open_session(backup, arguments) as session:
            report = Analyzer(
                session,
                app_slug=arguments.app,
                bundle_id=arguments.bundle_id,
                inspect_media=True,
            ).run()
            return _render_database_report(arguments, session, report)
    except WrongPasswordError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR
    except DecryptionError as error:
        print(f"Fehler bei der Entschluesselung: {error}", file=sys.stderr)
        return EXIT_ERROR
    except (AnalysisBlocked, ManifestSchemaError) as error:
        diagnostics = getattr(error, "diagnostics", {})
        print(reports.render_diagnostics_text(str(error), diagnostics), file=sys.stderr)
        return EXIT_DIAGNOSTICS


def _render_database_report(
    arguments: argparse.Namespace, session: BackupSession, report: AnalysisReport
) -> int:
    """Gibt die Schemata aus.

    Die Session muss dabei noch offen sein: bei verschluesselten Backups zeigt
    `readable_path` auf eine entschluesselte Kopie in ihrem Arbeitsverzeichnis.
    """
    found = 0
    lines: list[str] = ["Datenbankschemata", "=" * len("Datenbankschemata"), ""]
    payload: dict[str, object] = {
        "tool": "msgbackup-extractor",
        "report_type": "database-schema",
        "databases": [],
    }
    schema_list: list[dict[str, object]] = []

    for app in report.apps:
        for database in app.databases:
            found += 1
            lines.append(f"{app.profile_name}: {database.basename}")
            lines.append(f"    Domain:    {database.domain}")
            lines.append(f"    Groesse:   {reports.format_size(database.size)}")
            lines.append(f"    Rolle:     {database.role} [{database.confidence}]")
            lines.append(f"    Grundlage: {database.role_reason}")
            if not database.readable:
                lines.append(f"    Hinweis:   {database.note or 'nicht lesbar'}")
                lines.append("")
                continue

            tables: list[dict[str, object]] = []
            if database.readable_path is None:
                lines.append("    Hinweis:   Kein lesbarer Pfad zur Datenbank")
                lines.append("")
                continue
            try:
                with open_readonly(database.readable_path) as connection:
                    schemas = describe_database(connection)
            except NotASQLiteDatabase as error:
                lines.append(f"    Hinweis:   {error}")
                lines.append("")
                continue

            lines.append("")
            for name, schema in sorted(schemas.items()):
                rows = (
                    reports.plural(schema.row_count, "Zeile", "Zeilen")
                    if schema.row_count is not None
                    else "Zeilenzahl unbekannt"
                )
                lines.append(f"    Tabelle {name}  ({rows})")
                for column in schema.columns:
                    kind = schema.column_types.get(column) or "?"
                    marker = " PK" if column in schema.primary_key else ""
                    lines.append(f"        {column:<32} {kind}{marker}")
                for source, target, target_column in schema.foreign_keys:
                    lines.append(f"        FK {source} -> {target}.{target_column}")
                lines.append("")
                tables.append(
                    {
                        "name": name,
                        "columns": list(schema.columns),
                        "column_types": schema.column_types,
                        "primary_key": list(schema.primary_key),
                        "foreign_keys": [list(fk) for fk in schema.foreign_keys],
                        "row_count": schema.row_count,
                    }
                )

            schema_list.append(
                {
                    "app": app.profile_slug,
                    "basename": database.basename,
                    "domain": database.domain,
                    "role": database.role,
                    "confidence": database.confidence,
                    "tables": tables,
                }
            )

    if not found:
        print("Es wurden keine lesbaren App-Datenbanken im Backup gefunden.", file=sys.stderr)
        if report.is_partial:
            print(
                "Das Backup ist verschluesselt und konnte nicht geoeffnet werden.",
                file=sys.stderr,
            )
        return EXIT_ERROR

    payload["databases"] = schema_list
    _emit("\n".join(lines), payload, arguments.json_path)
    return EXIT_OK


def _parse_categories(value: str | None) -> frozenset[MediaCategory] | None:
    """Wandelt `--types image,video` in Kategorien um."""
    if not value:
        return None
    known = {category.value: category for category in MediaCategory}
    selected: set[MediaCategory] = set()
    unknown: list[str] = []
    for part in value.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name in known:
            selected.add(known[name])
        else:
            unknown.append(name)
    if unknown:
        raise SystemExit(
            f"Unbekannte Kategorien: {', '.join(unknown)}. "
            f"Moeglich: {', '.join(sorted(known))}"
        )
    return frozenset(selected) or None


def _command_extract(arguments: argparse.Namespace) -> int:
    output_dir = arguments.output.expanduser()
    try:
        require_non_cloud_path(
            output_dir, purpose="Ausgabeverzeichnis", allow=arguments.allow_cloud_output
        )
    except CloudSyncedPathError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR

    categories = _parse_categories(arguments.types)
    options = ExtractOptions(
        include_thumbnails=arguments.thumbnails,
        organize_by_chat=arguments.organize_by_chat,
        hardlinks=arguments.hardlinks,
        deduplicate=arguments.deduplicate,
        dry_run=arguments.dry_run,
        categories=categories,
    )

    backup = _resolve_backup(arguments)
    try:
        with _open_session(backup, arguments) as session:
            outcome = Extractor(
                session=session,
                output_dir=output_dir,
                options=options,
                app_slug=arguments.app,
                bundle_id=arguments.bundle_id,
            ).run()
    except WrongPasswordError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR
    except DecryptionError as error:
        print(f"Fehler bei der Entschluesselung: {error}", file=sys.stderr)
        return EXIT_ERROR
    except (ExtractionBlocked, AnalysisBlocked, ManifestSchemaError) as error:
        diagnostics = getattr(error, "diagnostics", {})
        print(reports.render_diagnostics_text(str(error), diagnostics), file=sys.stderr)
        return EXIT_DIAGNOSTICS
    except OutputGuardError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR

    if arguments.dry_run:
        _emit(reports.render_dry_run_text(outcome), None, None)
        return EXIT_OK

    payload = export_manifest.build(
        outcome.result,
        app=outcome.profile_slug,
        backup_udid=backup.udid,
        tool_version=__version__,
    )
    manifest_path = export_manifest.write(payload, output_dir)
    report_path = output_dir / "reports" / "extraction-report.json"
    reports.write_json(payload, report_path)

    text = reports.render_extraction_text(outcome)
    print(text)
    print(f"\nExport-Manifest: {manifest_path}", file=sys.stderr)
    print(f"Bericht:         {report_path}", file=sys.stderr)

    if outcome.result.integrity_errors:
        return EXIT_ERROR
    return EXIT_OK


def _command_verify(arguments: argparse.Namespace) -> int:
    path = arguments.manifest.expanduser()
    if path.is_dir():
        path = path / export_manifest.MANIFEST_NAME

    try:
        manifest = export_manifest.load(path)
    except InvalidManifest as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR

    if manifest.dry_run:
        print(
            "Dieses Manifest stammt aus einem Probelauf. Es enthaelt keine "
            "Inhaltshashes,\ndeshalb ist keine Pruefung moeglich.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    result = verify_export(manifest)
    print(reports.render_verify_text(result))

    if arguments.json_path is not None:
        target = arguments.json_path.expanduser()
        reports.write_json(reports.verify_to_dict(result), target)
        print(f"JSON-Bericht geschrieben: {target}", file=sys.stderr)

    return EXIT_OK if result.is_intact else EXIT_ERROR


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if getattr(arguments, "command", None) is None:
        parser.print_help()
        return EXIT_USAGE

    configure_logging(
        verbose=getattr(arguments, "verbose", False),
        show_paths=getattr(arguments, "show_paths", False),
    )

    try:
        return int(arguments.handler(arguments))
    except SystemExit as error:
        # `SystemExit` kann einen Exitcode ODER eine Meldung tragen. Eine
        # Meldung gehoert nach stderr, nicht durch int() gedreht.
        code = error.code
        if code is None:
            return EXIT_OK
        if isinstance(code, int):
            return code
        print(f"Fehler: {code}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return EXIT_ERROR
    except CloudSyncedPathError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
