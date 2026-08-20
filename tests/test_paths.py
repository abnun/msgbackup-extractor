"""Tests fuer Pfadsicherheit: Traversal, Output-Guard, Cloud-Guard."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from msgbackup_extractor.core.paths import (
    CloudSyncedPathError,
    OutputGuard,
    OutputGuardError,
    detect_cloud_provider,
    require_non_cloud_path,
    sanitize_component,
    sanitize_relative_path,
    unique_path,
)

# ---------------------------------------------------------------------------
# Sanitisierung einzelner Komponenten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal.jpg", "normal.jpg"),
        ("Max Mustermann", "Max Mustermann"),
        ("mit/slash", "mit_slash"),
        ("mit\\backslash", "mit_backslash"),
        ("mit:colon", "mit_colon"),
        ("mit\x00nul", "mit_nul"),
        ("mit\nnewline", "mit_newline"),
        ("frage?.jpg", "frage_.jpg"),
        ("stern*.jpg", "stern_.jpg"),
        ("  raender  ", "raender"),
        ("...", "unbenannt"),
        ("", "unbenannt"),
        (".", "unbenannt"),
        ("..", "unbenannt"),
        ("con", "unbenannt"),
        ("LPT1", "unbenannt"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_component_normalises_unicode() -> None:
    """APFS speichert NFD; ohne Normalisierung entstehen scheinbare Duplikate."""
    nfd = "Müller.jpg"  # u + Kombinierendes Trema
    nfc = "Müller.jpg"  # vorkomponiertes ue
    assert sanitize_component(nfd) == sanitize_component(nfc)


def test_sanitize_component_shortens_long_names_but_keeps_extension() -> None:
    result = sanitize_component("A" * 500 + ".jpg")
    assert len(result) <= 200
    assert result.endswith(".jpg")


# ---------------------------------------------------------------------------
# Traversal-Abwehr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Documents/img/a.jpg", "Documents/img/a.jpg"),
        ("../../etc/passwd", "etc/passwd"),
        ("/etc/passwd", "etc/passwd"),
        ("a/../../b", "a/b"),
        ("./a/./b", "a/b"),
        ("..", "unbenannt"),
        ("", "unbenannt"),
        ("/", "unbenannt"),
        ("Documents\\Windows\\a.jpg", "Documents/Windows/a.jpg"),
        ("a/\x00/b", "a/_/b"),
    ],
)
def test_sanitize_relative_path_defuses_traversal(raw: str, expected: str) -> None:
    assert sanitize_relative_path(raw) == PurePosixPath(expected)


def test_sanitized_paths_are_always_relative() -> None:
    for raw in ("/absolut/pfad", "../../ausbruch", "//doppelt//"):
        assert not sanitize_relative_path(raw).is_absolute()


# ---------------------------------------------------------------------------
# Output-Guard
# ---------------------------------------------------------------------------


def test_guard_resolves_inside_root(tmp_path: Path) -> None:
    guard = OutputGuard(root=tmp_path / "out")
    target = guard.resolve("media/images/a.jpg")
    assert target == (tmp_path / "out" / "media/images/a.jpg").resolve()


def test_guard_rejects_absolute_targets(tmp_path: Path) -> None:
    guard = OutputGuard(root=tmp_path / "out")
    with pytest.raises(OutputGuardError, match="Absoluter Zielpfad"):
        guard.resolve("/etc/passwd")


def test_guard_rejects_traversal_out_of_root(tmp_path: Path) -> None:
    guard = OutputGuard(root=tmp_path / "out")
    with pytest.raises(OutputGuardError, match="verlassen"):
        guard.resolve("../ausserhalb.txt")


def test_guard_rejects_writes_into_the_backup(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    guard = OutputGuard(root=tmp_path / "out", forbidden_roots=(backup,))
    # Ein Symlink, der ins Backup zeigt, darf nicht als Umweg dienen.
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "link").symlink_to(backup)
    with pytest.raises(OutputGuardError):
        guard.resolve("link/datei.jpg")


def test_guard_refuses_output_inside_backup(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    with pytest.raises(OutputGuardError, match="innerhalb des Backups"):
        OutputGuard(root=backup / "export", forbidden_roots=(backup,))


def test_guard_refuses_output_that_contains_the_backup(tmp_path: Path) -> None:
    backup = tmp_path / "daten" / "backup"
    backup.mkdir(parents=True)
    with pytest.raises(OutputGuardError, match="enthaelt das Backup"):
        OutputGuard(root=tmp_path / "daten", forbidden_roots=(backup,))


def test_guard_prepare_creates_parent_directories(tmp_path: Path) -> None:
    guard = OutputGuard(root=tmp_path / "out")
    target = guard.prepare("media/images/tief/a.jpg")
    assert target.parent.is_dir()
    assert not target.exists()


def test_guard_root_equals_target_is_allowed(tmp_path: Path) -> None:
    """Das Wurzelverzeichnis selbst ist ein gueltiges Ziel (z.B. fuer das Manifest)."""
    guard = OutputGuard(root=tmp_path / "out")
    assert guard.resolve("export-manifest.json").parent == (tmp_path / "out").resolve()


# ---------------------------------------------------------------------------
# Kollisionen
# ---------------------------------------------------------------------------


def test_unique_path_returns_input_when_free(tmp_path: Path) -> None:
    target = tmp_path / "a.jpg"
    assert unique_path(target) == target


def test_unique_path_appends_counter_instead_of_overwriting(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"erste")
    assert unique_path(tmp_path / "a.jpg").name == "a-1.jpg"
    (tmp_path / "a-1.jpg").write_bytes(b"zweite")
    assert unique_path(tmp_path / "a.jpg").name == "a-2.jpg"


# ---------------------------------------------------------------------------
# Cloud-Guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "provider"),
    [
        ("Library/Mobile Documents/com~apple~CloudDocs/Projekt", "iCloud Drive"),
        ("Dropbox/Export", "Dropbox"),
        ("OneDrive/Export", "Microsoft OneDrive"),
        ("OneDrive - Musterfirma/Export", "Microsoft OneDrive"),
        ("Google Drive/Meine Ablage", "Google Drive"),
        ("Nextcloud/x", "Nextcloud"),
    ],
)
def test_cloud_providers_are_detected(tmp_path: Path, relative: str, provider: str) -> None:
    target = tmp_path / relative
    target.mkdir(parents=True)
    assert detect_cloud_provider(target, home=tmp_path) == provider


def test_local_paths_are_not_flagged(tmp_path: Path) -> None:
    target = tmp_path / "messenger-extract" / "export"
    target.mkdir(parents=True)
    assert detect_cloud_provider(target, home=tmp_path) is None


def test_require_non_cloud_path_raises_for_icloud(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    home = tmp_path
    target = home / "Library/Mobile Documents/com~apple~CloudDocs/Export"
    target.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(CloudSyncedPathError, match="iCloud Drive"):
        require_non_cloud_path(target, purpose="Ausgabeverzeichnis")


def test_require_non_cloud_path_can_be_overridden(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    home = tmp_path
    target = home / "Dropbox/Export"
    target.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    require_non_cloud_path(target, purpose="Ausgabeverzeichnis", allow=True)


def test_error_message_names_the_flag() -> None:
    """Die Meldung muss dem Nutzer sagen, wie er es bewusst erzwingen kann."""
    home = Path.home()
    with pytest.raises(CloudSyncedPathError) as error:
        require_non_cloud_path(home / "Dropbox/x", purpose="Ausgabeverzeichnis")
    assert "--allow-cloud-output" in str(error.value)
