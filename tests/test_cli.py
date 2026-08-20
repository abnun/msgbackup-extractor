"""Tests fuer die Kommandozeilenschnittstelle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from msgbackup_extractor.cli import (
    EXIT_DIAGNOSTICS,
    EXIT_ERROR,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
)
from tests.conftest import TEST_PASSWORD, THREEMA_BUNDLE_ID, sample_files
from tests.support.backup_builder import UNKNOWN_SCHEMA, BuiltBackup, build_backup

# ---------------------------------------------------------------------------
# Grundgeruest
# ---------------------------------------------------------------------------


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USAGE
    assert "BEFEHL" in capsys.readouterr().out


def test_help_mentions_readonly_promise(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "niemals veraendert" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--version"])
    assert "msgx" in capsys.readouterr().out


def _all_option_strings() -> set[str]:
    """Alle Optionen des Hauptparsers und aller Unterbefehle."""
    import argparse

    parser = build_parser()
    options: set[str] = set()

    def collect(target: argparse.ArgumentParser) -> None:
        for action in target._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for subparser in action.choices.values():
                    collect(subparser)

    collect(parser)
    return options


def test_there_is_no_password_argument() -> None:
    """Passwoerter duerfen nie als Argument uebergeben werden koennen.

    Das ist eine harte Anforderung: ein Passwort in argv landet in der
    Shell-History und in der Prozessliste.
    """
    options = _all_option_strings()
    assert options, "Die Introspektion hat keine Optionen gefunden"
    forbidden = {
        option
        for option in options
        for marker in ("pass", "pw", "secret", "key", "credential", "kennwort")
        if marker in option.lower()
    }
    assert not forbidden, f"Verdaechtige Optionen: {sorted(forbidden)}"


def test_every_subcommand_offers_help() -> None:
    assert "--help" in _all_option_strings()


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def test_analyze_succeeds(plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", "--backup", str(plain_backup.path)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Messenger Backup Analyzer" in output
    assert THREEMA_BUNDLE_ID in output


def test_analyze_writes_json(
    plain_backup: BuiltBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "bericht.json"
    assert main(["analyze", "--backup", str(plain_backup.path), "--json", str(target)]) == EXIT_OK
    capsys.readouterr()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["apps"][0]["detection"]["bundle_id"] == THREEMA_BUNDLE_ID


def test_analyze_reports_go_to_stdout_and_logs_to_stderr(
    plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    """Der Bericht muss umleitbar bleiben."""
    main(["analyze", "--backup", str(plain_backup.path), "--verbose"])
    captured = capsys.readouterr()
    assert "Messenger Backup Analyzer" in captured.out
    assert "Messenger Backup Analyzer" not in captured.err


def test_metadata_only_avoids_the_password_prompt(
    encrypted_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    """--metadata-only darf nicht nach dem Passwort fragen."""
    assert main([
        "analyze", "--backup", str(encrypted_backup.path), "--metadata-only",
    ]) == EXIT_OK
    captured = capsys.readouterr()
    assert "Eingeschraenkte Analyse" in captured.out
    assert "Teilbericht" in captured.err
    # Es darf keine Passwortabfrage stattgefunden haben.
    assert "Passwort des verschluesselten Backups" not in captured.err


def test_analyze_of_encrypted_backup_with_password(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TEST_PASSWORD)
    assert main(["analyze", "--backup", str(encrypted_backup.path)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Eingeschraenkte Analyse" not in output
    assert "Gefundene Formate" in output
    assert "JPEG" in output


def test_password_never_appears_in_any_output(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Das Passwort darf in keinem Ausgabekanal auftauchen."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TEST_PASSWORD)
    main(["analyze", "--backup", str(encrypted_backup.path), "--verbose"])
    captured = capsys.readouterr()
    assert TEST_PASSWORD not in captured.out
    assert TEST_PASSWORD not in captured.err


def test_wrong_password_gives_a_clear_error(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "voellig-falsch")
    assert main(["analyze", "--backup", str(encrypted_backup.path)]) == EXIT_ERROR
    error_output = capsys.readouterr().err
    assert "Passwort ist falsch" in error_output
    # Die Meldung muss sagen, welches Passwort gemeint ist.
    assert "Finder" in error_output


def test_empty_password_is_rejected(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    with pytest.raises(ValueError, match="kein Passwort"):
        main(["analyze", "--backup", str(encrypted_backup.path)])
    capsys.readouterr()


def test_analyze_with_app_filter(
    plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["analyze", "--backup", str(plain_backup.path), "--app", "threema"]) == EXIT_OK
    assert "Threema" in capsys.readouterr().out


def test_analyze_rejects_unknown_app(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["analyze", "--backup", "/tmp", "--app", "telegram"])
    assert error.value.code == EXIT_USAGE
    assert "threema" in capsys.readouterr().err


def test_analyze_without_backup_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert main(["analyze"]) == EXIT_ERROR
    assert "Keine Backups gefunden" in capsys.readouterr().err


def test_analyze_rejects_non_backup_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["analyze", "--backup", str(tmp_path)]) == EXIT_ERROR
    assert "Apple-Backup" in capsys.readouterr().err


def test_analyze_emits_diagnostics_for_unknown_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = build_backup(
        tmp_path / "b", [], schema=UNKNOWN_SCHEMA, installed_applications=[THREEMA_BUNDLE_ID]
    )
    assert main(["analyze", "--backup", str(backup.path)]) == EXIT_DIAGNOSTICS
    error_output = capsys.readouterr().err
    assert "Diagnosebericht" in error_output
    assert "SomethingElse" in error_output


def test_analyze_include_schema_in_json(
    plain_backup: BuiltBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "b.json"
    main([
        "analyze", "--backup", str(plain_backup.path),
        "--json", str(target), "--include-schema",
    ])
    capsys.readouterr()
    assert "Files" in json.loads(target.read_text(encoding="utf-8"))["manifest_schema"]


def test_analyze_no_media_inspection(
    plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([
        "analyze", "--backup", str(plain_backup.path), "--no-media-inspection",
    ]) == EXIT_OK
    assert "Gefundene Formate" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


def test_database_prints_schema(
    plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["database", "--backup", str(plain_backup.path)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Datenbankschemata" in output
    assert "ZMESSAGE" in output
    assert "Z_PK" in output


def test_database_prints_no_row_contents(
    plain_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    """Schema ja, Inhalte nein."""
    main(["database", "--backup", str(plain_backup.path)])
    assert "platzhalter-" not in capsys.readouterr().out


def test_database_without_password_reports_the_reason(
    encrypted_backup: BuiltBackup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([
        "database", "--backup", str(encrypted_backup.path), "--metadata-only",
    ]) == EXIT_ERROR
    assert "verschluesselt" in capsys.readouterr().err


def test_database_of_encrypted_backup_with_password(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verschluesselte Datenbanken werden fuer den Schema-Dump entschluesselt."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TEST_PASSWORD)
    assert main(["database", "--backup", str(encrypted_backup.path)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "ZMESSAGE" in output
    assert "Z_PRIMARYKEY" in output


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------


def test_backups_lists_found_backups(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "Backup"
    build_backup(root, sample_files(), udid="AAA", installed_applications=[])
    assert main(["backups", "--root", str(root)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "AAA" in output
    assert "Test iPhone" in output


def test_backups_explains_how_to_create_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backups", "--root", str(tmp_path / "leer")]) == EXIT_ERROR
    assert "Finder" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# extract / verify: noch nicht implementiert, aber ehrlich
# ---------------------------------------------------------------------------


def test_extract_says_it_is_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract", "--backup", "/tmp", "--output", "/tmp/out"]) == EXIT_NOT_IMPLEMENTED
    assert "noch nicht implementiert" in capsys.readouterr().err


def test_extract_refuses_cloud_output_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "Library/Mobile Documents/com~apple~CloudDocs/Export"
    target.mkdir(parents=True)
    assert main(["extract", "--backup", "/tmp", "--output", str(target)]) == EXIT_ERROR
    assert "iCloud Drive" in capsys.readouterr().err


def test_extract_cloud_output_can_be_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "Dropbox/Export"
    target.mkdir(parents=True)
    assert main([
        "extract", "--backup", "/tmp", "--output", str(target), "--allow-cloud-output",
    ]) == EXIT_NOT_IMPLEMENTED
    capsys.readouterr()


def test_verify_says_it_is_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", "--manifest", "/tmp/m.json"]) == EXIT_NOT_IMPLEMENTED
    assert "noch nicht implementiert" in capsys.readouterr().err
