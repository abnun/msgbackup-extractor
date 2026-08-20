"""Tests fuer das lokale UI.

Geprueft wird der Index (die Datengrundlage) und die erzeugte Seite. Fuer die
Seite gilt dasselbe Sicherheitsmodell wie fuer den Rest: sie darf nichts
nachladen.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from msgbackup_extractor.cli import EXIT_ERROR, EXIT_OK, main
from msgbackup_extractor.extract import export_manifest
from msgbackup_extractor.extract.planner import ExtractOptions
from msgbackup_extractor.models import ExtractionResult
from msgbackup_extractor.ui.builder import (
    INDEX_PLACEHOLDER,
    PAGE_NAME,
    UiBuildError,
    build_index,
    load_raw_manifest,
    render_page,
    write_page,
)
from tests.conftest import TEST_PASSWORD, ThreemaBackup, extract


def _export(target: ThreemaBackup, output: Path, **kwargs: object) -> Path:
    """Fuehrt eine Extraktion aus und schreibt das Manifest wie die CLI."""
    outcome = extract(target, output, **kwargs)  # type: ignore[arg-type]
    payload = export_manifest.build(
        outcome.result, app="threema", backup_udid="TEST", tool_version="0"
    )
    export_manifest.write(payload, output)
    return output


def _index(output: Path) -> dict:
    manifest_path = output / export_manifest.MANIFEST_NAME
    return build_index(export_manifest.load(manifest_path), raw=load_raw_manifest(manifest_path))


@pytest.fixture
def export_dir(threema_backup: ThreemaBackup, tmp_path: Path) -> Path:
    return _export(threema_backup, tmp_path / "export")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def test_index_contains_originals_not_thumbnails_as_entries(export_dir: Path) -> None:
    """Ein Vorschaubild ist kein eigener Eintrag, sondern die Kachel des Originals."""
    index = _index(export_dir)
    counts = index["counts"]
    assert counts["entries"] == counts["originals"] + counts["preview_only"]
    assert counts["thumbnails"] > 0


def test_previews_are_attached_to_their_original(export_dir: Path) -> None:
    index = _index(export_dir)
    with_preview = [i for i in index["items"] if i.get("v")]
    assert with_preview
    for item in with_preview:
        assert (export_dir / item["v"]).is_file()


def test_thumbnail_without_original_becomes_its_own_marked_entry(
    export_dir: Path,
) -> None:
    """Ein Vorschaubild ohne Original ist oft alles, was uebrig ist."""
    index = _index(export_dir)
    orphans = [i for i in index["items"] if i.get("o") == 1]
    assert index["counts"]["preview_only"] == len(orphans)


def test_every_path_in_the_index_exists(export_dir: Path) -> None:
    index = _index(export_dir)
    for item in index["items"]:
        assert (export_dir / item["p"]).is_file(), item["p"]
        if item.get("v"):
            assert (export_dir / item["v"]).is_file(), item["v"]


def test_chats_are_sorted_by_size(export_dir: Path) -> None:
    """Die Filterleiste soll die grossen Chats zuerst zeigen."""
    index = _index(export_dir)
    assert index["chats"]
    counts = [
        sum(1 for i in index["items"] if i.get("c") == position)
        for position in range(len(index["chats"]))
    ]
    assert counts == sorted(counts, reverse=True)


def test_entries_are_newest_first_with_undated_last(export_dir: Path) -> None:
    index = _index(export_dir)
    stamps = [i.get("t") for i in index["items"]]
    dated = [s for s in stamps if s is not None]
    assert dated == sorted(dated, reverse=True)
    # Undatierte stehen hinten, nicht dazwischen.
    first_undated = next((n for n, s in enumerate(stamps) if s is None), len(stamps))
    assert all(s is None for s in stamps[first_undated:])


def test_app_internals_get_their_own_kind(export_dir: Path) -> None:
    """Plists und Datenbanken sind keine Nutzmedien und duerfen die Galerie
    nicht dominieren - ausgeblendet werden sie aber nicht."""
    index = _index(export_dir)
    internal = [i for i in index["items"] if i["k"] == "internal"]
    assert internal
    for item in internal:
        assert item["p"].startswith(("metadata/", "databases/"))
    assert index["counts"]["internal"] == len(internal)


def test_file_timestamps_do_not_land_on_the_timeline(export_dir: Path) -> None:
    """Ein Dateidatum sagt, wann das Backup schrieb - nicht wann der Inhalt entstand.

    Auf einer Zeitachse wuerde es alle betroffenen Dateien am Backup-Tag
    zusammenklumpen und die neuesten echten Medien verdraengen.
    """
    index = _index(export_dir)
    file_dated = [i for i in index["items"] if i.get("f")]
    assert file_dated, "Der Test prueft nichts ohne dateidatierte Eintraege"
    for item in file_dated:
        assert "t" not in item, "Dateidatum darf nicht als Nachrichtendatum gelten"


def test_message_timestamps_are_kept(export_dir: Path) -> None:
    index = _index(export_dir)
    dated = [i for i in index["items"] if i.get("t")]
    assert dated
    for item in dated:
        assert "f" not in item


def test_counts_are_consistent_with_the_items(export_dir: Path) -> None:
    index = _index(export_dir)
    items, counts = index["items"], index["counts"]
    assert counts["entries"] == len(items)
    assert counts["without_chat"] == sum(1 for i in items if "c" not in i)
    assert counts["without_date"] == sum(1 for i in items if "t" not in i)
    assert counts["item_bytes"] == sum(i.get("s", 0) for i in items)


def test_dry_run_manifest_is_refused(tmp_path: Path) -> None:
    payload = export_manifest.build(
        ExtractionResult(files=(), dry_run=True),
        app="threema", backup_udid="TEST", tool_version="0",
    )
    export_manifest.write(payload, tmp_path)
    manifest_path = tmp_path / export_manifest.MANIFEST_NAME
    with pytest.raises(UiBuildError, match="Probelauf"):
        build_index(export_manifest.load(manifest_path), raw=load_raw_manifest(manifest_path))


def test_empty_manifest_is_refused(tmp_path: Path) -> None:
    payload = export_manifest.build(
        ExtractionResult(files=()), app="threema", backup_udid="T", tool_version="0"
    )
    export_manifest.write(payload, tmp_path)
    manifest_path = tmp_path / export_manifest.MANIFEST_NAME
    with pytest.raises(UiBuildError, match="keine exportierten Dateien"):
        build_index(export_manifest.load(manifest_path), raw=load_raw_manifest(manifest_path))


def test_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UiBuildError, match="keine Datei"):
        load_raw_manifest(tmp_path / "fehlt.json")


def test_invalid_json_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "export-manifest.json"
    target.write_text("{kein json", encoding="utf-8")
    with pytest.raises(UiBuildError, match="kein gueltiges JSON"):
        load_raw_manifest(target)


# ---------------------------------------------------------------------------
# Seite
# ---------------------------------------------------------------------------


def test_page_is_self_contained(export_dir: Path) -> None:
    """Kein CDN, keine externen Fonts, kein Netzzugriff."""
    page = write_page(_index(export_dir), export_dir)
    html = page.read_text(encoding="utf-8")
    for marker in (
        "http://", "https://", "//cdn", "fonts.googleapis", "fonts.gstatic",
        "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "eval(",
    ):
        assert marker not in html, f"{marker} in der Seite gefunden"
    assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", html)


def test_page_embeds_the_index(export_dir: Path) -> None:
    """Der Index muss eingebettet sein: fetch von file:// scheitert an CORS."""
    index = _index(export_dir)
    page = write_page(index, export_dir)
    html = page.read_text(encoding="utf-8")
    assert INDEX_PLACEHOLDER not in html
    match = re.search(r"const DATA = (.*?);\n</script>", html, re.S)
    assert match is not None
    embedded = json.loads(match.group(1))
    assert len(embedded["items"]) == len(index["items"])


def test_embedded_json_cannot_break_out_of_the_script(export_dir: Path) -> None:
    """Ein `</script>` im Inhalt wuerde die Seite zerreissen."""
    index = _index(export_dir)
    index["items"].append({"p": "media/x.jpg", "k": "image", "n": "</script><script>x"})
    html = render_page(index)
    assert "</script><script>x" not in html
    assert "<\\/script>" in html
    # Entscheidend ist die schliessende Sequenz: nur `</script` beendet einen
    # Script-Block. Ein literales `<script>` im Text ist dagegen harmlos.
    assert html.count("</script>") == 2


def test_page_declares_utf8_and_no_referrer(export_dir: Path) -> None:
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert 'charset="utf-8"' in html
    assert 'name="referrer" content="no-referrer"' in html


def test_page_is_written_next_to_the_manifest(export_dir: Path) -> None:
    page = write_page(_index(export_dir), export_dir)
    assert page == export_dir / PAGE_NAME
    assert page.is_file()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_ui_generates_the_page(
    export_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ui", "--output", str(export_dir)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Lokale Ansicht erzeugt" in output
    assert (export_dir / PAGE_NAME).is_file()


def test_cli_ui_accepts_the_manifest_path(
    export_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = export_dir / export_manifest.MANIFEST_NAME
    assert main(["ui", "--output", str(manifest)]) == EXIT_OK
    capsys.readouterr()
    assert (export_dir / PAGE_NAME).is_file()


def test_cli_ui_reports_a_missing_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ui", "--output", str(tmp_path)]) == EXIT_ERROR
    assert "keine Datei" in capsys.readouterr().err


def test_cli_ui_works_for_an_encrypted_backup(
    encrypted_threema_backup: ThreemaBackup, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _export(encrypted_threema_backup, tmp_path / "export", password=TEST_PASSWORD)
    assert main(["ui", "--output", str(output)]) == EXIT_OK
    capsys.readouterr()
    index = _index(output)
    for item in index["items"]:
        assert (output / item["p"]).is_file()


def test_cli_ui_regenerates_without_re_extracting(
    export_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Das UI ist aus dem Manifest wiederherstellbar - Iterationen sind billig."""
    main(["ui", "--output", str(export_dir)])
    first = (export_dir / PAGE_NAME).read_bytes()
    main(["ui", "--output", str(export_dir)])
    capsys.readouterr()
    assert (export_dir / PAGE_NAME).read_bytes() == first


def test_ui_without_chat_assignment_still_works(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ohne Chat-Struktur darf das UI nicht scheitern."""
    output = _export(
        threema_backup, tmp_path / "export", options=ExtractOptions(organize_by_chat=False)
    )
    assert main(["ui", "--output", str(output)]) == EXIT_OK
    capsys.readouterr()
    assert (output / PAGE_NAME).is_file()


def test_ui_without_thumbnails_still_works(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _export(
        threema_backup, tmp_path / "export", options=ExtractOptions(include_thumbnails=False)
    )
    assert main(["ui", "--output", str(output)]) == EXIT_OK
    capsys.readouterr()
    index = _index(output)
    assert index["counts"]["thumbnails"] == 0
    assert index["counts"]["preview_only"] == 0
    # Bilder dienen sich selbst als Kachel.
    images = [i for i in index["items"] if i["k"] == "image"]
    assert images and all(i.get("v") == i["p"] for i in images)
