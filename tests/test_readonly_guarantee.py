"""Die zentrale Zusage: das Backup wird niemals veraendert.

Dieser Test nimmt einen vollstaendigen Fingerabdruck des Backups - jede Datei
mit Inhalt, Groesse und mtime - fuehrt alle lesenden Operationen aus und
vergleicht danach erneut. Jede Abweichung, auch eine neue Journal-Datei oder
eine geaenderte mtime, laesst den Test fehlschlagen.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from msgbackup_extractor.cli import main
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.manifest import ManifestReader
from tests.conftest import TEST_PASSWORD, analyze
from tests.support.backup_builder import BuiltBackup

Fingerprint = dict[str, tuple[int, str, int]]


def fingerprint(root: Path) -> Fingerprint:
    """Vollstaendiger Zustand eines Verzeichnisbaums."""
    state: Fingerprint = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            state[relative + "/"] = (0, "", 0)
            continue
        data = path.read_bytes()
        stat = path.stat()
        state[relative] = (stat.st_size, hashlib.sha256(data).hexdigest(), stat.st_mtime_ns)
    return state


def assert_unchanged(root: Path, before: Fingerprint) -> None:
    after = fingerprint(root)
    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {name for name in set(before) & set(after) if before[name] != after[name]}
    assert not added, f"Neue Dateien im Backup: {sorted(added)}"
    assert not removed, f"Entfernte Dateien im Backup: {sorted(removed)}"
    assert not changed, f"Veraenderte Dateien im Backup: {sorted(changed)}"


# ---------------------------------------------------------------------------
# Einzelne Operationen
# ---------------------------------------------------------------------------


def test_opening_the_backup_changes_nothing(plain_backup: BuiltBackup) -> None:
    before = fingerprint(plain_backup.path)
    backup = AppleBackup(plain_backup.path)
    backup.info()
    backup.applications()
    backup.device_info()
    list(backup.payload_directories())
    assert_unchanged(plain_backup.path, before)


def test_reading_the_manifest_creates_no_journal_files(plain_backup: BuiltBackup) -> None:
    before = fingerprint(plain_backup.path)
    with ManifestReader(plain_backup.path / "Manifest.db") as reader:
        list(reader.entries())
        reader.statistics()
        reader.domain_names()
        reader.count()
    assert_unchanged(plain_backup.path, before)


def test_full_analysis_changes_nothing(plain_backup: BuiltBackup) -> None:
    before = fingerprint(plain_backup.path)
    analyze(plain_backup)
    assert_unchanged(plain_backup.path, before)


def test_analysis_of_encrypted_backup_changes_nothing(encrypted_backup: BuiltBackup) -> None:
    before = fingerprint(encrypted_backup.path)
    analyze(encrypted_backup)
    assert_unchanged(encrypted_backup.path, before)


def test_reading_app_databases_creates_no_wal(plain_backup: BuiltBackup) -> None:
    """Der kritische Fall: SQLite legt sonst -wal/-shm neben die Datenbank."""
    before = fingerprint(plain_backup.path)
    report = analyze(plain_backup)
    assert any(db.readable for app in report.apps for db in app.databases), (
        "Der Test prueft nichts, wenn keine Datenbank gelesen wurde"
    )
    assert_unchanged(plain_backup.path, before)
    assert not list(plain_backup.path.rglob("*-wal"))
    assert not list(plain_backup.path.rglob("*-shm"))
    assert not list(plain_backup.path.rglob("*-journal"))


# ---------------------------------------------------------------------------
# Ueber die CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["analyze", "database"])
def test_cli_commands_do_not_modify_the_backup(
    plain_backup: BuiltBackup, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = fingerprint(plain_backup.path)
    main([command, "--backup", str(plain_backup.path), "--verbose"])
    capsys.readouterr()
    assert_unchanged(plain_backup.path, before)


def test_cli_writes_json_outside_the_backup(
    plain_backup: BuiltBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = fingerprint(plain_backup.path)
    target = tmp_path / "berichte" / "analyse.json"
    main(["analyze", "--backup", str(plain_backup.path), "--json", str(target)])
    capsys.readouterr()
    assert target.is_file()
    assert_unchanged(plain_backup.path, before)


# ---------------------------------------------------------------------------
# Nachweis, dass der Test selbst greift
# ---------------------------------------------------------------------------


def test_the_check_detects_a_new_file(plain_backup: BuiltBackup) -> None:
    """Gegenprobe: der Fingerabdruck erkennt eine Veraenderung wirklich."""
    before = fingerprint(plain_backup.path)
    (plain_backup.path / "neu.txt").write_text("x")
    with pytest.raises(AssertionError, match="Neue Dateien"):
        assert_unchanged(plain_backup.path, before)


def test_the_check_detects_a_modified_file(plain_backup: BuiltBackup) -> None:
    before = fingerprint(plain_backup.path)
    target = plain_backup.path / "Status.plist"
    data = target.read_bytes()
    target.write_bytes(data + b"\x00")
    with pytest.raises(AssertionError, match="Veraenderte Dateien"):
        assert_unchanged(plain_backup.path, before)


def test_the_check_detects_a_touched_mtime(plain_backup: BuiltBackup) -> None:
    before = fingerprint(plain_backup.path)
    target = plain_backup.path / "Status.plist"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(AssertionError, match="Veraenderte Dateien"):
        assert_unchanged(plain_backup.path, before)


def test_full_analysis_of_encrypted_backup_with_password_changes_nothing(
    encrypted_backup: BuiltBackup,
) -> None:
    """Der schaerfste Fall: Manifest und Datenbanken werden entschluesselt.

    Alle Zwischenergebnisse muessen ausserhalb des Backups landen.
    """
    before = fingerprint(encrypted_backup.path)
    report = analyze(encrypted_backup, password=TEST_PASSWORD)
    assert report.manifest_available, "Der Test prueft nichts ohne gelesenes Manifest"
    assert any(
        db.readable for app in report.apps for db in app.databases
    ), "Der Test prueft nichts ohne entschluesselte Datenbank"
    assert_unchanged(encrypted_backup.path, before)


def test_cli_analyze_of_encrypted_backup_changes_nothing(
    encrypted_backup: BuiltBackup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TEST_PASSWORD)
    before = fingerprint(encrypted_backup.path)
    assert main(["analyze", "--backup", str(encrypted_backup.path), "--verbose"]) == 0
    capsys.readouterr()
    assert_unchanged(encrypted_backup.path, before)
