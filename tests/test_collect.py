"""Tests fuer das Einsammeln einer Auswahl.

Warum die CLI das macht und nicht der Browser: JavaScript auf einer
`file://`-Seite darf lokale Dateien anzeigen, ihre Bytes aber nicht lesen
(`fetch` und `XHR` blockiert, `canvas` tainted). Das UI liefert deshalb nur die
Liste der Pfade.
"""

from __future__ import annotations

import io
from pathlib import Path, PurePosixPath

import pytest

from msgbackup_extractor.cli import EXIT_ERROR, EXIT_OK, main
from msgbackup_extractor.core.paths import OutputGuardError
from msgbackup_extractor.extract import export_manifest
from msgbackup_extractor.extract.collect import (
    CollectOptions,
    Collector,
    CollectOutcome,
    parse_selection,
)
from tests.conftest import ThreemaBackup, extract


@pytest.fixture
def export_dir(threema_backup: ThreemaBackup, tmp_path: Path) -> Path:
    output = tmp_path / "export"
    outcome = extract(threema_backup, output)
    payload = export_manifest.build(
        outcome.result, app="threema", backup_udid="TEST", tool_version="0"
    )
    export_manifest.write(payload, output)
    return output


def _manifest(export_dir: Path):
    return export_manifest.load(export_dir / export_manifest.MANIFEST_NAME)


def _some_paths(export_dir: Path, count: int = 3) -> list[str]:
    manifest = _manifest(export_dir)
    return [
        entry.output_path
        for entry in manifest.files
        if entry.expects_file and entry.output_path
    ][:count]


# ---------------------------------------------------------------------------
# Auswahlliste lesen
# ---------------------------------------------------------------------------


def test_parse_selection_reads_one_path_per_line() -> None:
    assert parse_selection("a/b.jpg\nc/d.mp4\n") == ["a/b.jpg", "c/d.mp4"]


def test_parse_selection_ignores_blanks_and_comments() -> None:
    text = "\n# Kommentar\na/b.jpg\n\n// noch einer\nc/d.mp4\n   \n"
    assert parse_selection(text) == ["a/b.jpg", "c/d.mp4"]


def test_parse_selection_strips_quotes_and_dot_slash() -> None:
    assert parse_selection('"a/b.jpg"\n./c/d.mp4\n') == ["a/b.jpg", "c/d.mp4"]


def test_parse_selection_removes_duplicates_but_keeps_order() -> None:
    assert parse_selection("b.jpg\na.jpg\nb.jpg\n") == ["b.jpg", "a.jpg"]


def test_parse_selection_of_empty_text() -> None:
    assert parse_selection("\n\n  \n") == []


# ---------------------------------------------------------------------------
# Einsammeln
# ---------------------------------------------------------------------------


def test_collects_selected_files(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir)
    result = Collector(manifest=_manifest(export_dir), target_dir=target).run(paths)
    assert result.collected == len(paths)
    assert not result.problems
    for entry in result.files:
        assert entry.target is not None
        assert (target / entry.target).is_file()


def test_collected_content_is_identical(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir)
    Collector(manifest=_manifest(export_dir), target_dir=target).run(paths)
    for relative in paths:
        name = Path(relative).name
        assert (target / name).read_bytes() == (export_dir / relative).read_bytes()


def test_hardlinks_cost_no_extra_space(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir)
    Collector(manifest=_manifest(export_dir), target_dir=target).run(paths)
    for relative in paths:
        assert (target / Path(relative).name).stat().st_ino == (
            export_dir / relative
        ).stat().st_ino


def test_copies_when_hardlinks_are_disabled(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir, 1)
    Collector(
        manifest=_manifest(export_dir),
        target_dir=target,
        options=CollectOptions(hardlinks=False),
    ).run(paths)
    collected = target / Path(paths[0]).name
    assert collected.stat().st_ino != (export_dir / paths[0]).stat().st_ino
    assert collected.read_bytes() == (export_dir / paths[0]).read_bytes()


def test_flat_by_default(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir)
    Collector(manifest=_manifest(export_dir), target_dir=target).run(paths)
    assert all(p.is_file() for p in target.iterdir())


def test_keep_structure_preserves_directories(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    paths = _some_paths(export_dir)
    Collector(
        manifest=_manifest(export_dir),
        target_dir=target,
        options=CollectOptions(keep_structure=True),
    ).run(paths)
    for relative in paths:
        assert (target / relative).is_file()


def test_the_same_file_under_two_paths_is_collected_once(
    export_dir: Path, tmp_path: Path
) -> None:
    """Medienpfad und Chat-Verknuepfung sind dieselbe Datei, nicht zwei.

    Frueher entstanden daraus `Vertrag.pdf` und `Vertrag-1.pdf` - zwei Kopien
    desselben Inodes. Das war als Schutz gegen Ueberschreiben gedacht, war aber
    an dieser Stelle keiner: es geht nichts verloren, wenn zwei Namen auf
    denselben Inhalt zeigen. Es entstand nur Doppeltes.
    """
    manifest = _manifest(export_dir)
    entry = next(e for e in manifest.files if e.expects_file and e.link_paths)
    assert entry.output_path is not None
    paths = [entry.output_path, entry.link_paths[0]]
    target = tmp_path / "auswahl"

    result = Collector(manifest=manifest, target_dir=target).run(paths)

    targets = {f.target for f in result.files if f.target}
    assert len(targets) == 1, "derselbe Inhalt gehoert einmal ins Ziel"
    assert (target / next(iter(targets))).is_file()
    assert len(list(target.iterdir())) == 1


def test_two_different_files_with_one_name_both_survive(
    export_dir: Path, tmp_path: Path
) -> None:
    """Die Eigenschaft, auf die es wirklich ankommt.

    Zwei verschiedene Dateien, die flach gesammelt denselben Namen haetten -
    keine darf die andere ersetzen.
    """
    manifest = _manifest(export_dir)
    zwei = [
        e for e in manifest.files if e.expects_file and e.output_path
    ][:2]
    assert len(zwei) == 2
    # Beide auf denselben Dateinamen umbenennen, damit sie kollidieren.
    for eintrag in zwei:
        alt_pfad = export_dir / eintrag.output_path
        neu_pfad = alt_pfad.parent / f"gleicher-name{alt_pfad.suffix}"
        if alt_pfad.exists() and not neu_pfad.exists():
            alt_pfad.rename(neu_pfad)
            object.__setattr__(
                eintrag,
                "output_path",
                str(PurePosixPath(eintrag.output_path).parent / neu_pfad.name),
            )
    target = tmp_path / "auswahl"

    result = Collector(manifest=manifest, target_dir=target).run(
        [e.output_path for e in zwei]
    )

    targets = {f.target for f in result.files if f.target}
    assert len(targets) == 2, "keine darf die andere ersetzen"
    assert all((target / name).is_file() for name in targets)


def test_a_second_run_into_the_same_target_keeps_both(
    export_dir: Path, tmp_path: Path
) -> None:
    """Der Fall, den mehrere Befehle in dasselbe Ziel ausloesen.

    Frueher startete die Namensvergabe je Lauf bei null, ein zweiter Lauf
    ueberschrieb also eine gleichnamige Datei des ersten. Genau das passiert,
    sobald eine grosse Auswahl auf mehrere Aufrufe verteilt wird.
    """
    manifest = _manifest(export_dir)
    eintraege = [e for e in manifest.files if e.expects_file and e.output_path][:2]
    assert len(eintraege) == 2
    target = tmp_path / "auswahl"

    Collector(manifest=manifest, target_dir=target).run([eintraege[0].output_path])
    vorher = sorted(p.name for p in target.iterdir())
    Collector(manifest=manifest, target_dir=target).run([eintraege[1].output_path])
    nachher = sorted(p.name for p in target.iterdir())

    assert len(nachher) == 2, f"aus {vorher} wurde {nachher}"


def test_the_same_selection_twice_makes_no_duplicates(
    export_dir: Path, tmp_path: Path
) -> None:
    """Denselben Befehl zweimal auszufuehren darf nichts verdoppeln."""
    manifest = _manifest(export_dir)
    paths = _some_paths(export_dir, 3)
    target = tmp_path / "auswahl"

    Collector(manifest=manifest, target_dir=target).run(paths)
    erste = sorted(p.name for p in target.iterdir())
    Collector(manifest=manifest, target_dir=target).run(paths)
    zweite = sorted(p.name for p in target.iterdir())

    assert erste == zweite


def test_link_paths_are_valid_selections(export_dir: Path, tmp_path: Path) -> None:
    """Auch ein Pfad aus der Chat-Struktur muss einsammelbar sein."""
    manifest = _manifest(export_dir)
    entry = next(e for e in manifest.files if e.expects_file and e.link_paths)
    result = Collector(manifest=manifest, target_dir=tmp_path / "a").run(
        [entry.link_paths[0]]
    )
    assert result.collected == 1


# ---------------------------------------------------------------------------
# Fehlerfaelle
# ---------------------------------------------------------------------------


def test_unknown_path_is_reported_not_silently_ignored(
    export_dir: Path, tmp_path: Path
) -> None:
    result = Collector(manifest=_manifest(export_dir), target_dir=tmp_path / "a").run(
        ["media/images/gibt-es-nicht.jpg"]
    )
    assert result.count(CollectOutcome.UNKNOWN) == 1
    assert result.problems


def test_missing_file_is_reported(export_dir: Path, tmp_path: Path) -> None:
    paths = _some_paths(export_dir, 1)
    (export_dir / paths[0]).unlink()
    result = Collector(manifest=_manifest(export_dir), target_dir=tmp_path / "a").run(paths)
    assert result.count(CollectOutcome.MISSING) == 1


def test_verify_detects_a_modified_file(export_dir: Path, tmp_path: Path) -> None:
    paths = _some_paths(export_dir, 1)
    victim = export_dir / paths[0]
    victim.write_bytes(victim.read_bytes() + b"manipuliert")
    result = Collector(
        manifest=_manifest(export_dir),
        target_dir=tmp_path / "a",
        options=CollectOptions(verify=True),
    ).run(paths)
    assert result.count(CollectOutcome.CORRUPT) == 1
    assert not list((tmp_path / "a").iterdir()) if (tmp_path / "a").exists() else True


def test_without_verify_a_modified_file_is_still_collected(
    export_dir: Path, tmp_path: Path
) -> None:
    """Ohne --verify wird nicht geprueft - das muss der Bericht ausweisen."""
    paths = _some_paths(export_dir, 1)
    victim = export_dir / paths[0]
    victim.write_bytes(victim.read_bytes() + b"x")
    result = Collector(manifest=_manifest(export_dir), target_dir=tmp_path / "a").run(paths)
    assert result.collected == 1
    assert not result.verified


def test_dry_run_writes_nothing(export_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "auswahl"
    result = Collector(
        manifest=_manifest(export_dir),
        target_dir=target,
        options=CollectOptions(dry_run=True),
    ).run(_some_paths(export_dir))
    assert result.collected == 3
    assert not target.exists() or not list(target.iterdir())


def test_target_inside_the_export_is_refused(export_dir: Path) -> None:
    with pytest.raises(OutputGuardError, match="innerhalb des Exports"):
        Collector(manifest=_manifest(export_dir), target_dir=export_dir / "drin").run(
            _some_paths(export_dir, 1)
        )


def test_export_is_not_modified(export_dir: Path, tmp_path: Path) -> None:
    from tests.test_readonly_guarantee import assert_unchanged, fingerprint

    before = fingerprint(export_dir)
    Collector(manifest=_manifest(export_dir), target_dir=tmp_path / "a").run(
        _some_paths(export_dir)
    )
    assert_unchanged(export_dir, before)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_collect_from_a_file(
    export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = tmp_path / "auswahl.txt"
    listing.write_text("\n".join(_some_paths(export_dir)) + "\n", encoding="utf-8")
    target = tmp_path / "ziel"
    assert main([
        "collect", "--output", str(export_dir), "--target", str(target),
        "--selection", str(listing),
    ]) == EXIT_OK
    assert "Auswahl eingesammelt" in capsys.readouterr().out
    assert len(list(target.iterdir())) == 3


def test_cli_collect_from_stdin(
    export_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Der Weg aus der Zwischenablage: pbpaste | msgx collect --selection -"""
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("\n".join(_some_paths(export_dir)) + "\n")
    )
    target = tmp_path / "ziel"
    assert main([
        "collect", "--output", str(export_dir), "--target", str(target),
        "--selection", "-",
    ]) == EXIT_OK
    capsys.readouterr()
    assert len(list(target.iterdir())) == 3


def test_cli_collect_reports_an_empty_selection(
    export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = tmp_path / "leer.txt"
    listing.write_text("\n# nur ein Kommentar\n", encoding="utf-8")
    assert main([
        "collect", "--output", str(export_dir), "--target", str(tmp_path / "z"),
        "--selection", str(listing),
    ]) == EXIT_ERROR
    assert "Auswahlliste ist leer" in capsys.readouterr().err


def test_cli_collect_reports_an_unreadable_selection(
    export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([
        "collect", "--output", str(export_dir), "--target", str(tmp_path / "z"),
        "--selection", str(tmp_path / "fehlt.txt"),
    ]) == EXIT_ERROR
    assert "nicht lesbar" in capsys.readouterr().err


def test_cli_collect_refuses_cloud_target(
    export_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "Library/Mobile Documents/com~apple~CloudDocs/Auswahl"
    target.mkdir(parents=True)
    listing = tmp_path / "a.txt"
    listing.write_text("media/x.jpg\n", encoding="utf-8")
    assert main([
        "collect", "--output", str(export_dir), "--target", str(target),
        "--selection", str(listing),
    ]) == EXIT_ERROR
    assert "iCloud Drive" in capsys.readouterr().err


def test_cli_collect_returns_error_on_problems(
    export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = tmp_path / "a.txt"
    listing.write_text("media/images/gibt-es-nicht.jpg\n", encoding="utf-8")
    assert main([
        "collect", "--output", str(export_dir), "--target", str(tmp_path / "z"),
        "--selection", str(listing),
    ]) == EXIT_ERROR
    assert "Nicht im Export" in capsys.readouterr().out


def test_cli_collect_writes_json_report(
    export_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    listing = tmp_path / "a.txt"
    listing.write_text("\n".join(_some_paths(export_dir)) + "\n", encoding="utf-8")
    report = tmp_path / "bericht.json"
    assert main([
        "collect", "--output", str(export_dir), "--target", str(tmp_path / "z"),
        "--selection", str(listing), "--json", str(report),
    ]) == EXIT_OK
    capsys.readouterr()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["report_type"] == "collection"
    assert payload["counts"]["collected"] == 3


def test_cli_collect_requires_target(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["collect", "--output", "/tmp", "--selection", "-"])
    assert "--target" in capsys.readouterr().err


def test_cli_collect_reports_a_missing_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([
        "collect", "--output", str(tmp_path), "--target", str(tmp_path / "z"),
        "--selection", "-",
    ]) == EXIT_ERROR
    assert "keine Datei" in capsys.readouterr().err
