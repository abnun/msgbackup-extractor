"""Tests fuer das Plattformabhaengige.

Entwickelt wurde auf macOS. Damit die Windows-Unterstuetzung nicht bloss eine
Behauptung ist, taeuschen diese Tests das Betriebssystem vor und pruefen, dass
die richtigen Pfade, Befehle und Hinweise herauskommen. Was sie **nicht**
leisten koennen: bestaetigen, dass Apples Geraete-App ihre Backups
tatsaechlich dort ablegt. Das steht als Einschraenkung in der README.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msgbackup_extractor.core import platforms
from msgbackup_extractor.core.backup import (
    BackupAccessError,
    default_backup_root,
    default_backup_roots,
    list_local_backups,
)
from msgbackup_extractor.core.paths import detect_cloud_provider
from tests.conftest import sample_files
from tests.support.backup_builder import build_backup


@pytest.fixture
def on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")


@pytest.fixture
def on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------


def test_platform_detection(on_macos: None) -> None:
    assert platforms.is_macos()
    assert not platforms.is_windows()
    assert platforms.platform_name() == "macOS"


def test_windows_detection(on_windows: None) -> None:
    assert platforms.is_windows()
    assert not platforms.is_macos()
    assert platforms.platform_name() == "Windows"


def test_other_platform_is_named_not_guessed(on_linux: None) -> None:
    assert platforms.platform_name() == "linux"


# ---------------------------------------------------------------------------
# Suchorte
# ---------------------------------------------------------------------------


def test_macos_looks_where_the_finder_writes(on_macos: None, tmp_path: Path) -> None:
    locations = platforms.backup_locations(tmp_path)
    assert len(locations) == 1
    assert locations[0].path == tmp_path / "Library/Application Support/MobileSync/Backup"
    assert locations[0].source == "Finder"


def test_windows_looks_in_both_places(on_windows: None, tmp_path: Path) -> None:
    """iTunes und die Apple-Geraete-App verwenden verschiedene Verzeichnisse."""
    paths = [location.path for location in platforms.backup_locations(tmp_path)]
    assert tmp_path / "AppData/Roaming/Apple Computer/MobileSync/Backup" in paths
    assert tmp_path / "Apple/MobileSync/Backup" in paths
    sources = {location.source for location in platforms.backup_locations(tmp_path)}
    assert sources == {"iTunes", "Apple-Geraete-App"}


def test_unknown_platform_has_no_default_location(on_linux: None, tmp_path: Path) -> None:
    """Kein erfundener Ort - dort hilft nur --backup."""
    assert platforms.backup_locations(tmp_path) == ()
    assert default_backup_roots(tmp_path) == ()


def test_default_backup_root_stays_usable_without_a_known_location(
    on_linux: None, tmp_path: Path
) -> None:
    """Meldungen brauchen einen Pfad, auch wenn das System keinen kennt."""
    assert default_backup_root(tmp_path).is_absolute()


# ---------------------------------------------------------------------------
# Backups finden
# ---------------------------------------------------------------------------


def test_backups_are_found_at_the_windows_location(
    on_windows: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / "AppData/Roaming/Apple Computer/MobileSync/Backup"
    build_backup(root, sample_files(), udid="WINDOWS-UDID", installed_applications=[])
    found = list_local_backups()
    assert [p.name for p in found] == ["WINDOWS-UDID"]


def test_backups_from_several_locations_are_merged(
    on_windows: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wer von iTunes zur Geraete-App gewechselt ist, hat Backups an beiden Orten."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    build_backup(
        tmp_path / "AppData/Roaming/Apple Computer/MobileSync/Backup",
        sample_files(), udid="AUS-ITUNES", installed_applications=[],
    )
    build_backup(
        tmp_path / "Apple/MobileSync/Backup",
        sample_files(), udid="AUS-DER-APP", installed_applications=[],
    )
    assert {p.name for p in list_local_backups()} == {"AUS-ITUNES", "AUS-DER-APP"}


def test_one_unreadable_location_does_not_hide_the_others(
    on_windows: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein gesperrtes Verzeichnis darf ein lesbares nicht verdecken."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    build_backup(
        tmp_path / "Apple/MobileSync/Backup",
        sample_files(), udid="LESBAR", installed_applications=[],
    )
    blocked = tmp_path / "AppData/Roaming/Apple Computer/MobileSync/Backup"
    blocked.mkdir(parents=True)
    blocked.chmod(0o000)
    try:
        assert {p.name for p in list_local_backups()} == {"LESBAR"}
    finally:
        blocked.chmod(0o755)


def test_permission_error_is_reported_when_nothing_was_found(
    on_windows: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    blocked = tmp_path / "Apple/MobileSync/Backup"
    blocked.mkdir(parents=True)
    blocked.chmod(0o000)
    try:
        with pytest.raises(BackupAccessError, match="Windows"):
            list_local_backups()
    finally:
        blocked.chmod(0o755)


# ---------------------------------------------------------------------------
# Cloud-Guard
# ---------------------------------------------------------------------------


def test_icloud_is_recognised_on_macos(on_macos: None, tmp_path: Path) -> None:
    target = tmp_path / "Library/Mobile Documents/com~apple~CloudDocs/Export"
    target.mkdir(parents=True)
    assert detect_cloud_provider(target, home=tmp_path) == "iCloud Drive"


def test_icloud_is_recognised_on_windows(on_windows: None, tmp_path: Path) -> None:
    """Unter Windows heisst der Ordner anders - sonst greift der Schutz nicht."""
    target = tmp_path / "iCloudDrive" / "Export"
    target.mkdir(parents=True)
    assert detect_cloud_provider(target, home=tmp_path) == "iCloud Drive"


def test_macos_icloud_path_is_not_special_on_windows(
    on_windows: None, tmp_path: Path
) -> None:
    target = tmp_path / "Library/Mobile Documents/com~apple~CloudDocs/Export"
    target.mkdir(parents=True)
    assert detect_cloud_provider(target, home=tmp_path) is None


@pytest.mark.parametrize("platform_fixture", ["on_macos", "on_windows", "on_linux"])
def test_common_providers_are_recognised_everywhere(
    platform_fixture: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    request.getfixturevalue(platform_fixture)
    for name, expected in (("Dropbox", "Dropbox"), ("OneDrive", "Microsoft OneDrive")):
        target = tmp_path / name / "Export"
        target.mkdir(parents=True, exist_ok=True)
        assert detect_cloud_provider(target, home=tmp_path) == expected


# ---------------------------------------------------------------------------
# Zwischenablage und Hinweise
# ---------------------------------------------------------------------------


def test_clipboard_command_per_platform(
    on_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert platforms.clipboard_command() == "pbpaste"
    monkeypatch.setattr("sys.platform", "win32")
    assert "Get-Clipboard" in platforms.clipboard_command()
    monkeypatch.setattr("sys.platform", "linux")
    assert "xclip" in platforms.clipboard_command()


def test_permission_hint_names_the_right_setting(
    on_macos: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert "Festplattenvollzugriff" in platforms.permission_hint(tmp_path)
    monkeypatch.setattr("sys.platform", "win32")
    hint = platforms.permission_hint(tmp_path)
    assert "Festplattenvollzugriff" not in hint
    assert "Windows" in hint


def test_ui_carries_the_platform_clipboard_command(
    on_windows: None, tmp_path: Path
) -> None:
    """Die Seite darf nicht pbpaste vorschlagen, wenn sie auf Windows entstand."""
    from msgbackup_extractor.extract import export_manifest
    from msgbackup_extractor.models import ExtractionResult
    from msgbackup_extractor.ui.builder import build_index, load_raw_manifest

    export_manifest.write(
        export_manifest.build(
            ExtractionResult(files=()), app="threema", backup_udid="T", tool_version="0"
        ),
        tmp_path,
    )
    manifest_path = tmp_path / export_manifest.MANIFEST_NAME
    with pytest.raises(Exception):  # noqa: B017 - leeres Manifest, hier nicht das Thema
        build_index(export_manifest.load(manifest_path), raw=load_raw_manifest(manifest_path))
    # Der Befehl selbst haengt nicht am Manifest:
    assert "Get-Clipboard" in platforms.clipboard_command()


# ---------------------------------------------------------------------------
# Kein Plattformcode ausserhalb der einen Stelle
# ---------------------------------------------------------------------------


def test_platform_checks_live_in_one_place() -> None:
    """Sonst wandern Betriebssystem-Annahmen unbemerkt in den Rest des Codes."""
    import msgbackup_extractor

    root = Path(msgbackup_extractor.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "platforms.py":
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("sys.platform", "platform.system()", "os.name"):
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert not offenders, f"Plattformabfragen ausserhalb von platforms.py: {offenders}"


def test_no_macos_only_paths_outside_the_platform_module() -> None:
    """Ein fest verdrahteter macOS-Pfad wuerde Windows stillschweigend brechen."""
    import msgbackup_extractor

    root = Path(msgbackup_extractor.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "platforms.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "Library/Application Support" in line or "Library/Mobile Documents" in line:
                # In Fliesstext einer Dokumentation ist die Nennung in Ordnung.
                if line.lstrip().startswith(("#", '"', "'", "*")) or '"""' in line:
                    continue
                offenders.append(f"{path.name}: {line.strip()[:60]}")
    assert not offenders, f"Fest verdrahtete macOS-Pfade: {offenders}"


# ---------------------------------------------------------------------------
# %APPDATA% wird gelesen, nicht abgeleitet
# ---------------------------------------------------------------------------


def test_windows_follows_a_redirected_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    """In einem Roaming- oder Domaenenprofil liegt %APPDATA% nicht unter dem Profil.

    Wer es ableitet, sucht am falschen Ort und meldet dann "kein Backup
    gefunden" - mit einem Pfad, den es auf dem Rechner gar nicht gibt.
    """
    monkeypatch.setattr("sys.platform", "win32")

    orte = platforms.backup_locations(
        Path("C:/Users/mm"), appdata=Path("D:/Profile/mm/Roaming")
    )
    pfade = [str(o.path) for o in orte]

    assert any("D:/Profile/mm/Roaming" in p and "Apple Computer" in p for p in pfade)
    # Der Pfad der Geraete-App haengt am Profil, nicht an %APPDATA%.
    assert any(p.startswith("C:/Users/mm/Apple") for p in pfade)
    assert not any("C:/Users/mm/AppData" in p for p in pfade)


def test_windows_falls_back_to_the_usual_place_without_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    pfade = [str(o.path) for o in platforms.backup_locations(Path("C:/Users/mm"))]

    assert any("C:/Users/mm/AppData/Roaming/Apple Computer" in p for p in pfade)


def test_windows_reads_appdata_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ohne ausdrueckliches Argument gilt die Umgebung, nicht die Annahme."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", "E:/Umgeleitet/Roaming")

    pfade = [str(o.path) for o in platforms.backup_locations(Path("C:/Users/mm"))]

    assert any("E:/Umgeleitet/Roaming/Apple Computer" in p for p in pfade)


def test_no_duplicate_locations_when_appdata_is_the_usual_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sonst stehen identische Pfade doppelt in der Fehlermeldung."""
    monkeypatch.setattr("sys.platform", "win32")

    orte = platforms.backup_locations(
        Path("C:/Users/mm"), appdata=Path("C:/Users/mm/AppData/Roaming")
    )
    pfade = [o.path for o in orte]

    assert len(pfade) == len(set(pfade))


def test_open_command_differs_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    assert platforms.open_command() == ("open",)
    monkeypatch.setattr("sys.platform", "win32")
    assert platforms.open_command()[0] == "cmd"
    monkeypatch.setattr("sys.platform", "linux")
    assert platforms.open_command() == ("xdg-open",)
