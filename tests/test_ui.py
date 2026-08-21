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
from tests.conftest import TEST_PASSWORD, ThreemaBackup, WhatsAppBackup, extract


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
    """Ein Verzeichnis ohne Export soll sagen, was es erwartet haette."""
    assert main(["ui", "--output", str(tmp_path)]) == EXIT_ERROR
    error = capsys.readouterr().err
    assert "kein Export gefunden" in error
    assert "export-manifest.json" in error


def test_cli_ui_reports_a_missing_manifest_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ui", "--output", str(tmp_path / "fehlt.json")]) == EXIT_ERROR
    assert "kein Verzeichnis" in capsys.readouterr().err


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


# ---------------------------------------------------------------------------
# Auswahl und Uebergabe an die CLI
# ---------------------------------------------------------------------------


def test_page_offers_selection_and_handover(export_dir: Path) -> None:
    """Die Seite muss Auswahl, Sammelleiste und Uebergabedialog enthalten."""
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    for element in ('id="tray"', 'id="tray-all"', 'id="tray-clear"', 'id="tray-hand"',
                    'id="hand"', 'id="hand-steps"', 'id="v-pick"'):
        assert element in html, f"{element} fehlt"
    # Auswahlknopf und Schrittinhalte entstehen im JavaScript, nicht als Markup.
    assert 'pick.className = "pick"' in html
    assert ".pick {" in html
    assert 'ta.id = "hand-list"' in html


def test_the_handover_is_always_a_complete_set_of_steps(export_dir: Path) -> None:
    """Der Dialog muss jeden auszufuehrenden Befehl zeigen.

    Passt die Auswahl nicht in eine Befehlszeile, wird sie auf mehrere Aufrufe
    verteilt - und dann muessen alle dastehen, nummeriert. Erst wenn selbst das
    unzumutbar viele werden, kommt die Liste ueber die Standardeingabe.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    for stueck in ("function batches(", "function planFor(", "function renderPlan(",
                   "function stepBlock(", "MAX_BEFEHLE", "Schritt ${++nummer}"):
        assert stueck in html, f"{stueck} fehlt"
    # Jeder Schritt ist zugeklappt und hat seinen eigenen Kopierknopf, damit
    # man einen Befehl kopieren kann, ohne ihn erst aufzuklappen.
    assert "details.step" in html
    assert 'zeile.className = "stepcopy"' in html
    # Die Zahl der Schritte steht ueber den Schritten.
    assert "Schritte, der Reihe nach" in html
    # Und der Dialog oeffnet oben, nicht dort, wo er zuletzt stand.
    assert "hand.scrollTop = 0" in html


def test_the_target_folder_is_editable_and_shell_safe(export_dir: Path) -> None:
    """Der Zielordner gehoert in ein Feld, nicht in den Text.

    Einen Ordner-Auswahldialog kann es nicht geben: der Browser gibt aus
    Datenschutzgruenden nur den Ordnernamen heraus, nicht den Pfad
    (`showDirectoryPicker` liefert einen Handle ohne Pfadfeld, gemessen). Also
    ein Eingabefeld - und dann muss der Wert shell-sicher in den Befehl.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert 'id="hand-target-input"' in html
    assert "function pfadFuerShell(" in html
    # Die Tilde darf NICHT gequotet werden, sonst expandiert sie nicht und der
    # Ordner heisst wirklich "~".
    assert '"~/" + shellQuote(pfad.slice(2))' in html
    assert "~-]+$/.test(pfad)" in html
    # Der Wert wird gemerkt, aber ein gesperrter Speicher darf nichts brechen.
    assert "msgx.target" in html
    assert "localStorage" in html


def test_the_page_knows_the_command_line_budget(export_dir: Path) -> None:
    """Ohne die Grenze koennte die Seite keinen Befehl bauen, der sicher passt."""
    index = _index(export_dir)
    assert isinstance(index["cmdmax"], int)
    assert index["cmdmax"] > 1000
    assert "DATA.cmdmax" in write_page(index, export_dir).read_text(encoding="utf-8")


def test_page_does_not_promise_a_browser_download(export_dir: Path) -> None:
    """Ein ZIP im Browser ist unmoeglich - die Seite darf es nicht andeuten.

    Auf einer `file://`-Seite sind `fetch` und `XHR` blockiert und ein Canvas
    mit lokalem Bild ist tainted. JavaScript kann die Bytes also nicht lesen.
    Ein Knopf, der einen Download verspricht und nichts liefert, waere
    schlimmer als kein Knopf.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "download=" not in html
    assert "createObjectURL" not in html
    assert "new Blob" not in html
    # Stattdessen wird der CLI-Weg genannt.
    assert "msgx collect" in html


def test_handover_command_uses_the_export_directory(export_dir: Path) -> None:
    """Der Befehl im Dialog muss auf das eigene Verzeichnis zeigen.

    Er wird zur Laufzeit aus `location.pathname` gebildet - die Seite liegt im
    Export, also ist es deren Elternverzeichnis.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "location.pathname" in html
    assert "--selection -" in html
    assert "--target" in html


def test_selection_survives_a_filter_change(export_dir: Path) -> None:
    """Die Auswahl wird nach Pfad gehalten, nicht nach Position im Filter.

    Sonst verliert man beim Wechsel des Chats die halbe Auswahl, ohne es zu
    merken.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "const selected = new Set()" in html
    assert "selected.has(it.p)" in html


def test_tray_appears_when_a_filter_is_set(export_dir: Path) -> None:
    """Ohne das waere "Alle im Filter auswaehlen" unerreichbar.

    Die Sammelleiste war zuerst nur bei bestehender Auswahl sichtbar - und
    genau darin steckte der Knopf, mit dem man eine Auswahl anlegt.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "function filterActive()" in html
    assert "chosen.length === 0 && !active" in html


def test_tray_names_the_active_filter(export_dir: Path) -> None:
    """Der Knopf tut je nach Filter etwas anderes - das muss dranstehen."""
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert 'id="tray-filter"' in html
    assert "function filterLabel()" in html


def test_actions_needing_a_selection_are_disabled_without_one(
    export_dir: Path,
) -> None:
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert 'document.getElementById("tray-clear").disabled = chosen.length === 0' in html
    assert 'document.getElementById("tray-hand").disabled = chosen.length === 0' in html


def test_resetting_filters_keeps_the_selection(export_dir: Path) -> None:
    """Filter zuruecksetzen darf nicht die Auswahl loeschen.

    Beides sind eigene Knoepfe; wer den einen drueckt, will nicht das andere.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    # Der Reset fasst nur die Filterknoepfe in der Seitenleiste an.
    assert 'document.querySelectorAll(\'aside [aria-pressed="true"]\')' in html


def test_facet_counts_and_filtering_share_one_predicate(export_dir: Path) -> None:
    """Zwei getrennte Implementierungen wuerden auseinanderlaufen.

    Dann zeigt die Seitenleiste Zahlen, die zum Ergebnis nicht passen - und man
    glaubt eher der Zahl als der Ansicht.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "function makePredicate(over)" in html
    assert "filtered = items.filter(makePredicate({}))" in html
    assert "function countWith(over)" in html
    assert "const passes = makePredicate(over)" in html


def test_facet_counts_are_updated_on_every_filter_change(export_dir: Path) -> None:
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "function updateFacets()" in html
    # apply() ruft es auf, also greift es bei jeder Aenderung.
    assert "    updateFacets();\n    renderStatus();" in html


def test_a_facet_group_does_not_constrain_itself(export_dir: Path) -> None:
    """Sonst zeigten alle nicht gewaehlten Jahre 0 und man kaeme nicht mehr weg.

    Die Zahl einer Option beantwortet: was bliebe, wenn ich *diese* waehle -
    unter Beruecksichtigung der anderen Gruppen, nicht der eigenen.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "years: new Set([year])" in html
    assert "kinds: new Set([k])" in html
    assert "chats: new Set([index])" in html


def test_special_flags_count_cumulatively(export_dir: Path) -> None:
    """Besonderheiten wirken verknuepfend, nicht alternativ."""
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "flags: new Set([...state.flags, key])" in html


def test_options_leading_nowhere_are_disabled(export_dir: Path) -> None:
    """Eine Option mit 0 Treffern soll nicht in eine leere Ansicht fuehren.

    Gewaehlte Optionen bleiben bedienbar, sonst koennte man sie nicht abwaehlen.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "facet.button.disabled = n === 0 && !pressed" in html


# ---------------------------------------------------------------------------
# Mehrere Messenger auf einer Seite
# ---------------------------------------------------------------------------


@pytest.fixture
def combined_root(
    threema_backup: ThreemaBackup, whatsapp_backup: WhatsAppBackup, tmp_path: Path
) -> Path:
    """Ein Verzeichnis mit zwei Exporten, wie `~/messenger-extract/export`."""
    from msgbackup_extractor.core.backup import AppleBackup
    from msgbackup_extractor.core.session import BackupSession
    from msgbackup_extractor.extraction import Extractor

    root = tmp_path / "export"
    _export(threema_backup, root / "threema")
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        outcome = Extractor(
            session=session, output_dir=root / "whatsapp", app_slug="whatsapp"
        ).run()
    payload = export_manifest.build(
        outcome.result, app="whatsapp", backup_udid="TEST", tool_version="0"
    )
    export_manifest.write(payload, root / "whatsapp")
    return root


def _combined(root: Path) -> dict:
    from msgbackup_extractor.ui.builder import build_combined_index, discover_exports

    return build_combined_index(
        discover_exports(root), export_manifest.load, load_raw_manifest
    )


def test_exports_are_discovered(combined_root: Path) -> None:
    from msgbackup_extractor.ui.builder import discover_exports

    found = discover_exports(combined_root)
    assert {ref.directory for ref in found} == {"threema", "whatsapp"}


def test_messenger_labels_come_from_the_profiles(combined_root: Path) -> None:
    """Aus dem Slug "whatsapp" wuerde sonst "Whatsapp"."""
    index = _combined(combined_root)
    assert index["messengers"] == ["Threema", "WhatsApp"]


def test_combined_paths_are_prefixed_and_resolve(combined_root: Path) -> None:
    index = _combined(combined_root)
    assert index["items"]
    for item in index["items"]:
        assert item["p"].split("/", 1)[0] in {"threema", "whatsapp"}
        assert (combined_root / item["p"]).is_file(), item["p"]
        if item.get("v"):
            assert (combined_root / item["v"]).is_file(), item["v"]


def test_every_item_names_its_messenger(combined_root: Path) -> None:
    index = _combined(combined_root)
    for item in index["items"]:
        assert item["g"] in range(len(index["messengers"]))


def test_chats_carry_their_messenger(combined_root: Path) -> None:
    """Chatnamen koennen sich wiederholen - sonst verschmelzen zwei zu einem."""
    index = _combined(combined_root)
    assert len(index["chat_messengers"]) == len(index["chats"])
    assert set(index["chat_messengers"]) == set(range(len(index["messengers"])))


def test_chat_indices_are_remapped_correctly(combined_root: Path) -> None:
    """Ein Chat muss zum Messenger seiner Medien passen."""
    index = _combined(combined_root)
    for item in index["items"]:
        if "c" in item:
            assert index["chat_messengers"][item["c"]] == item["g"]


def test_combined_items_are_globally_sorted(combined_root: Path) -> None:
    index = _combined(combined_root)
    dated = [i["t"] for i in index["items"] if "t" in i]
    assert dated == sorted(dated, reverse=True)


def test_combined_counts_add_up(combined_root: Path) -> None:
    index = _combined(combined_root)
    assert index["counts"]["entries"] == len(index["items"])
    assert index["counts"]["messengers"] == 2
    assert sum(s["entries"] for s in index["sources"]) == len(index["items"])


def test_page_has_a_messenger_switch(combined_root: Path) -> None:
    page = write_page(_combined(combined_root), combined_root)
    html = page.read_text(encoding="utf-8")
    assert 'id="switch"' in html
    assert "function buildSwitch()" in html
    # Der Schalter wirkt ausschliessend, nicht als Mehrfachfilter.
    assert "state.messengers.clear();" in html


def test_switch_is_hidden_for_a_single_messenger(export_dir: Path) -> None:
    """Bei einem Messenger waere ein Umschalter sinnlos."""
    page = write_page(_index(export_dir), export_dir)
    html = page.read_text(encoding="utf-8")
    assert 'class="switch" id="switch"' in html
    assert "if (messengers.length < 2) return;" in html


def test_messenger_is_part_of_the_filter_predicate(combined_root: Path) -> None:
    html = write_page(_combined(combined_root), combined_root).read_text(encoding="utf-8")
    assert "const groups = over.messengers ?? state.messengers;" in html
    assert "groups.size && !(it.g !== undefined && groups.has(it.g))" in html


def test_cli_ui_builds_a_combined_page(
    combined_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["ui", "--output", str(combined_root)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Threema" in output and "WhatsApp" in output
    assert (combined_root / PAGE_NAME).is_file()


def test_combined_page_is_still_self_contained(combined_root: Path) -> None:
    html = write_page(_combined(combined_root), combined_root).read_text(encoding="utf-8")
    for marker in ("http://", "https://", "fetch(", "XMLHttpRequest", "download="):
        assert marker not in html


# ---------------------------------------------------------------------------
# Automatisches Auffrischen nach dem Export
# ---------------------------------------------------------------------------


def test_generated_pages_are_recognisable(export_dir: Path) -> None:
    """Vor dem Ueberschreiben muss klar sein, dass die Datei von uns ist."""
    from msgbackup_extractor.ui.builder import is_generated_page

    page = write_page(_index(export_dir), export_dir)
    assert is_generated_page(page)


def test_a_foreign_index_is_not_recognised(tmp_path: Path) -> None:
    """Eine fremde index.html zu ersetzen waere Datenverlust."""
    from msgbackup_extractor.ui.builder import is_generated_page

    foreign = tmp_path / PAGE_NAME
    foreign.write_text("<!doctype html><h1>Meine Seite</h1>", encoding="utf-8")
    assert not is_generated_page(foreign)
    assert not is_generated_page(tmp_path / "gibt-es-nicht.html")


def test_extract_creates_the_page_automatically(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "export" / "threema"
    assert main([
        "extract", "--backup", str(threema_backup.path), "--output", str(output),
    ]) == EXIT_OK
    captured = capsys.readouterr()
    assert (output / PAGE_NAME).is_file()
    assert "Ansicht:" in captured.err


def test_no_ui_skips_the_page(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "export" / "threema"
    assert main([
        "extract", "--backup", str(threema_backup.path), "--output", str(output),
        "--no-ui",
    ]) == EXIT_OK
    capsys.readouterr()
    assert not (output / PAGE_NAME).exists()


def test_extract_does_not_create_an_overview_out_of_nowhere(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Eine neue Datei ausserhalb von --output waere ein Bruch der Zusage."""
    parent = tmp_path / "export"
    output = parent / "threema"
    main(["extract", "--backup", str(threema_backup.path), "--output", str(output)])
    capsys.readouterr()
    assert not (parent / PAGE_NAME).exists()


def test_extract_refreshes_an_existing_overview(
    threema_backup: ThreemaBackup,
    whatsapp_backup: WhatsAppBackup,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Liegt schon eine Uebersicht, wird sie mitgezogen - sonst veraltet sie."""
    from msgbackup_extractor.core.backup import AppleBackup
    from msgbackup_extractor.core.session import BackupSession
    from msgbackup_extractor.extraction import Extractor

    parent = tmp_path / "export"

    # Zwei Exporte anlegen und die Uebersicht einmal erzeugen.
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        outcome = Extractor(
            session=session, output_dir=parent / "whatsapp", app_slug="whatsapp"
        ).run()
    export_manifest.write(
        export_manifest.build(
            outcome.result, app="whatsapp", backup_udid="T", tool_version="0"
        ),
        parent / "whatsapp",
    )
    main(["extract", "--backup", str(threema_backup.path), "--output", str(parent / "threema")])
    capsys.readouterr()
    main(["ui", "--output", str(parent)])
    capsys.readouterr()
    overview = parent / PAGE_NAME
    assert overview.is_file()
    before = overview.stat().st_mtime_ns

    # Erneuter Export: die Uebersicht muss mitgezogen werden.
    overview.write_text(
        overview.read_text(encoding="utf-8").replace("</body>", "<!--alt--></body>"),
        encoding="utf-8",
    )
    main(["extract", "--backup", str(threema_backup.path), "--output", str(parent / "threema")])
    error = capsys.readouterr().err
    assert "<!--alt-->" not in overview.read_text(encoding="utf-8")
    assert str(overview) in error
    assert overview.stat().st_mtime_ns != before


def test_extract_points_at_the_overview_command_when_none_exists(
    threema_backup: ThreemaBackup,
    whatsapp_backup: WhatsAppBackup,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ohne bestehende Uebersicht wird nur der Befehl genannt, nichts geschrieben."""
    from msgbackup_extractor.core.backup import AppleBackup
    from msgbackup_extractor.core.session import BackupSession
    from msgbackup_extractor.extraction import Extractor

    parent = tmp_path / "export"
    with BackupSession(AppleBackup(whatsapp_backup.path)) as session:
        outcome = Extractor(
            session=session, output_dir=parent / "whatsapp", app_slug="whatsapp"
        ).run()
    export_manifest.write(
        export_manifest.build(
            outcome.result, app="whatsapp", backup_udid="T", tool_version="0"
        ),
        parent / "whatsapp",
    )

    main(["extract", "--backup", str(threema_backup.path), "--output", str(parent / "threema")])
    error = capsys.readouterr().err
    assert "weitere Exporte daneben" in error
    assert "msgx ui --output" in error
    assert not (parent / PAGE_NAME).exists()


def test_dry_run_writes_no_page(
    threema_backup: ThreemaBackup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "export" / "threema"
    main([
        "extract", "--backup", str(threema_backup.path), "--output", str(output),
        "--dry-run",
    ])
    capsys.readouterr()
    assert not (output / PAGE_NAME).exists()


# ---------------------------------------------------------------------------
# Aufloesung der Kacheln
# ---------------------------------------------------------------------------


def test_index_carries_both_widths(export_dir: Path) -> None:
    """Ohne die echten Breiten kann der Browser nicht waehlen."""
    index = _index(export_dir)
    with_both = [i for i in index["items"] if i.get("w") and i.get("vw")]
    assert with_both, "Kein Eintrag mit Original- und Vorschaubreite"


def test_page_offers_both_resolutions_per_tile(export_dir: Path) -> None:
    """srcset statt einer Regel im Generator: der Browser entscheidet besser.

    Die von den Messengern gespeicherten Vorschaubilder sind teils winzig - am
    echten Export bei WhatsApp im Median 100 Pixel breit. In einer
    200-CSS-Pixel-Kachel waere das auf einem Retina-Display eine vierfache
    Hochskalierung.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "img.srcset =" in html
    assert "img.sizes = TILE_SIZES" in html
    # Nur wenn das Original tatsaechlich groesser ist, gibt es etwas zu waehlen.
    assert "it.w > it.vw" in html


def test_too_small_previews_are_counted(export_dir: Path) -> None:
    """Die Zahl gehoert in den Bericht - sonst merkt niemand, dass es sie gibt."""
    index = _index(export_dir)
    assert "preview_too_small" in index["counts"]
    expected = sum(
        1
        for i in index["items"]
        if i.get("vw") and i.get("w") and i["vw"] < 400 and i["w"] > i["vw"]
    )
    assert index["counts"]["preview_too_small"] == expected


# ---------------------------------------------------------------------------
# Kopieren ohne Terminal
# ---------------------------------------------------------------------------


def test_the_page_can_copy_without_a_terminal(export_dir: Path) -> None:
    """Der Weg ohne Kommandozeile, samt seiner Waechter.

    Gemessen: eine als Datei geoeffnete Seite darf lesen und schreiben, wenn
    der Mensch den Ordner freigibt. Die Sperre betrifft den automatischen
    Zugriff, nicht den erlaubten.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    for stueck in ('id="hand-direct"', 'id="hd-export"', 'id="hd-target"',
                   'id="hd-run"', 'id="hd-stop"', "showDirectoryPicker",
                   "createWritable", "requestPermission"):
        assert stueck in html, f"{stueck} fehlt"


def test_the_browser_copy_verifies_against_the_manifest(export_dir: Path) -> None:
    """Sonst waere es der schwaechere Zwilling von collect --verify.

    Die Hashes kommen aus dem Manifest im freigegebenen Ordner - der Index
    traegt keine -, und geprueft wird die ZURUECKGELESENE Datei. Den eigenen
    Puffer zu hashen waere Selbstbestaetigung.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "hashKarteLaden" in html
    assert "export-manifest.json" in html
    assert "const zurueck = await (await ziel.getFile()).arrayBuffer();" in html
    assert "Kopie weicht ab" in html
    # Auch die Quelle wird gegen das Manifest geprueft, nicht nur die Kopie.
    assert "Quelle weicht vom Manifest ab" in html


def test_the_browser_copy_refuses_a_target_inside_the_export(export_dir: Path) -> None:
    """Dieselbe Regel wie in der Kommandozeile, mit dem passenden Werkzeug.

    resolve() liefert einen relativen Pfad, wenn das Ziel ein Nachfahre des
    Exports ist - und braucht dafuer keine Pfade zu kennen.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "await HD.exportDir.resolve(HD.targetDir)" in html
    assert "liegt im Export" in html


def test_the_browser_copy_does_not_overwrite_and_can_repeat(export_dir: Path) -> None:
    """Belegte Namen kommen aus dem Zielordner, wie bei collect."""
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "for await (const [name, h] of HD.targetDir.entries())" in html
    assert "lagen schon dort" in html


def test_the_browser_copy_states_what_it_cannot_do(export_dir: Path) -> None:
    """Zwei echte Nachteile - und die gehoeren in den Dialog, nicht ins Kleingedruckte.

    Der Cloud-Waechter braucht den Pfad, den der Browser nicht herausgibt.
    Hardlinks gibt es nicht, also entstehen echte Kopien.
    """
    html = write_page(_index(export_dir), export_dir).read_text(encoding="utf-8")
    assert "per Hardlink wären es 0 Bytes" in html
    assert "in eine Cloud synchronisiert, kann hier niemand prüfen" in html
    # Und der Weg ueber die Kommandozeile bleibt sichtbar daneben stehen.
    assert "Oder über die Kommandozeile" in html


def test_building_the_overview_also_refreshes_the_export_pages(
    export_dir: Path, tmp_path: Path
) -> None:
    """Sonst tragen die Einzelseiten nach einer Aenderung den alten Stand.

    Genau das ist passiert: die Uebersicht hatte den neuen Kopierweg, die
    Einzelseiten nicht - und verhielten sich anders. Eine stille Abweichung.
    """
    from msgbackup_extractor.cli import main

    # Eine Einzelseite anlegen, damit sie erneuert werden kann.
    assert main(["ui", "--output", str(export_dir)]) == 0
    seite = export_dir / "index.html"
    original = seite.read_text(encoding="utf-8")
    seite.write_text(
        original.replace("<body>", "<body><!-- veralteter Stand -->", 1), encoding="utf-8"
    )

    # Die Uebersicht im Elternverzeichnis bauen.
    assert main(["ui", "--output", str(export_dir.parent)]) == 0

    assert "veralteter Stand" not in seite.read_text(encoding="utf-8")


def test_a_foreign_page_inside_the_output_is_left_alone(
    export_dir: Path,
) -> None:
    """Eine fremde index.html zu ersetzen waere Datenverlust."""
    from msgbackup_extractor.cli import main

    fremd = "<html><body>von Hand geschrieben</body></html>"
    (export_dir / "index.html").write_text(fremd, encoding="utf-8")

    assert main(["ui", "--output", str(export_dir.parent)]) == 0

    assert (export_dir / "index.html").read_text(encoding="utf-8") == fremd
