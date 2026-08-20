"""Der gefuehrte Ablauf.

Geprueft wird, welche Befehle der Assistent zusammensetzt - nicht, was diese
Befehle tun. Das steht in den Tests der Befehle selbst. Genau darum gibt es den
Assistenten in dieser Form: er hat keine eigene Logik, die gepruefft werden
muesste.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from msgbackup_extractor.assistant import (
    EXIT_ABORTED,
    EXIT_OK,
    Assistant,
    describe_backups,
)
from msgbackup_extractor.core.backup import BackupAccessError
from tests.support.backup_builder import build_backup


class Recorder:
    """Nimmt die Befehlszeilen auf und liefert vorgegebene Exitcodes."""

    def __init__(self, codes: list[int] | None = None) -> None:
        self.commands: list[list[str]] = []
        self._codes = list(codes or [])

    def __call__(self, argv):
        self.commands.append(list(argv))
        return self._codes.pop(0) if self._codes else 0


def run_assistant(
    answers: list[str],
    *,
    backups: tuple[Path, ...] = (),
    codes: list[int] | None = None,
    tmp_path: Path | None = None,
    opened: list[Path] | None = None,
) -> tuple[int, Recorder, str]:
    runner = Recorder(codes)
    output = io.StringIO()
    assistant = Assistant(
        stdin=io.StringIO("\n".join(answers) + "\n" if answers else ""),
        stdout=output,
        runner=runner,
        home=tmp_path or Path("/nirgendwo"),
        finder=lambda: backups,
        opener=(opened.append if opened is not None else lambda _p: None),
    )
    return assistant.run(), runner, output.getvalue()


# ---------------------------------------------------------------------------
# Backups beschreiben
# ---------------------------------------------------------------------------


def test_describe_backups_reads_the_plists_without_a_password(tmp_path: Path) -> None:
    """Geraet und Verschluesselung stehen unverschluesselt im Backup."""
    gebaut = build_backup(tmp_path / "b", [], password="egal")

    (choice,) = describe_backups((gebaut.path,))

    assert choice.path == gebaut.path
    assert "verschluesselt" in choice.detail
    assert "unverschluesselt" not in choice.detail


def test_describe_backups_keeps_an_unreadable_entry_visible(tmp_path: Path) -> None:
    """Ein unlesbares Backup darf nicht einfach aus der Liste fallen."""
    kaputt = tmp_path / "keinbackup"
    kaputt.mkdir()

    (choice,) = describe_backups((kaputt,))

    assert choice.path == kaputt
    assert "nicht lesbar" in choice.detail


# ---------------------------------------------------------------------------
# Der Ablauf
# ---------------------------------------------------------------------------


def test_happy_path_composes_analyze_dry_run_and_extract(tmp_path: Path) -> None:
    backup = tmp_path / "Backup" / "geraet"
    backup.mkdir(parents=True)
    ziel = tmp_path / "export"

    code, runner, text = run_assistant(
        ["1", "1", str(ziel), "j", "n"], backups=(backup,), tmp_path=tmp_path
    )

    assert code == EXIT_OK
    basis = ["extract", "--backup", str(backup), "--output", str(ziel), "--app", "threema"]
    assert runner.commands == [
        ["analyze", "--backup", str(backup)],
        [*basis, "--dry-run"],
        basis,
    ]
    # Der Assistent soll den Befehl zeigen, nicht nur ausfuehren.
    assert "msgx analyze --backup" in text
    assert "Naechstes Mal genuegt" in text


def test_declining_the_real_run_writes_nothing(tmp_path: Path) -> None:
    """Der Probelauf darf laufen, der echte Lauf nur nach Zustimmung."""
    backup = tmp_path / "b"
    backup.mkdir()

    code, runner, text = run_assistant(
        ["1", "1", str(tmp_path / "out"), "n"], backups=(backup,), tmp_path=tmp_path
    )

    assert code == EXIT_OK
    assert [c[0] for c in runner.commands] == ["analyze", "extract"]
    assert "--dry-run" in runner.commands[1]
    assert "Es wurde nichts geschrieben" in text


def test_a_failing_analysis_stops_before_any_extract(tmp_path: Path) -> None:
    """Sonst wuerde auf einer gescheiterten Analyse weitergebaut."""
    backup = tmp_path / "b"
    backup.mkdir()

    code, runner, text = run_assistant(
        ["1", "1", str(tmp_path / "out"), "j", "j"],
        backups=(backup,),
        codes=[3],
        tmp_path=tmp_path,
    )

    assert code == 3
    assert [c[0] for c in runner.commands] == ["analyze"]
    assert "nichts geschrieben" in text


def test_a_failing_dry_run_stops_before_the_real_run(tmp_path: Path) -> None:
    backup = tmp_path / "b"
    backup.mkdir()

    code, runner, _ = run_assistant(
        ["1", "1", str(tmp_path / "out"), "j"],
        backups=(backup,),
        codes=[0, 3],
        tmp_path=tmp_path,
    )

    assert code == 3
    assert len(runner.commands) == 2
    assert "--dry-run" in runner.commands[1]


def test_end_of_input_aborts_without_running_anything(tmp_path: Path) -> None:
    """In einer Pipe darf der Assistent nicht haengen bleiben."""
    code, runner, text = run_assistant([], backups=(tmp_path,), tmp_path=tmp_path)

    assert code == EXIT_ABORTED
    assert runner.commands == []
    assert "Abgebrochen" in text


def test_a_typed_path_wins_over_the_numbered_list(tmp_path: Path) -> None:
    gefunden = tmp_path / "gefunden"
    gefunden.mkdir()
    eigener = tmp_path / "woanders"

    _, runner, _ = run_assistant(
        [str(eigener), "1", str(tmp_path / "out"), "n"],
        backups=(gefunden,),
        tmp_path=tmp_path,
    )

    assert runner.commands[0] == ["analyze", "--backup", str(eigener)]


def test_an_unreadable_backup_root_still_allows_a_manual_path(tmp_path: Path) -> None:
    """Fehlende Rechte duerfen den Ablauf nicht beenden."""

    def finder():
        raise BackupAccessError("kein Leserecht")

    runner = Recorder()
    output = io.StringIO()
    eigener = tmp_path / "handeingabe"
    code = Assistant(
        stdin=io.StringIO(f"{eigener}\n1\n{tmp_path / 'out'}\nn\n"),
        stdout=output,
        runner=runner,
        home=tmp_path,
        finder=finder,
        opener=lambda _p: None,
    ).run()

    assert code == EXIT_OK
    assert "kein Leserecht" in output.getvalue()
    assert runner.commands[0] == ["analyze", "--backup", str(eigener)]


def test_the_result_page_is_only_opened_when_it_exists(tmp_path: Path) -> None:
    backup = tmp_path / "b"
    backup.mkdir()
    ziel = tmp_path / "out"
    ziel.mkdir()
    (ziel / "index.html").write_text("<html></html>", encoding="utf-8")
    geoeffnet: list[Path] = []

    run_assistant(
        ["1", "1", str(ziel), "j", "j", "j"],
        backups=(backup,),
        tmp_path=tmp_path,
        opened=geoeffnet,
    )

    assert geoeffnet == [ziel / "index.html"]


def test_no_page_no_open(tmp_path: Path) -> None:
    backup = tmp_path / "b"
    backup.mkdir()
    geoeffnet: list[Path] = []

    run_assistant(
        ["1", "1", str(tmp_path / "leer"), "j", "j"],
        backups=(backup,),
        tmp_path=tmp_path,
        opened=geoeffnet,
    )

    assert geoeffnet == []


@pytest.mark.parametrize("antwort", ["2", "whatsapp", "WhatsApp"])
def test_the_messenger_can_be_chosen_by_number_or_name(
    tmp_path: Path, antwort: str
) -> None:
    backup = tmp_path / "b"
    backup.mkdir()

    _, runner, _ = run_assistant(
        ["1", antwort, str(tmp_path / "out"), "n"], backups=(backup,), tmp_path=tmp_path
    )

    assert "whatsapp" in runner.commands[1]


def test_the_assistant_never_composes_a_password_argument(tmp_path: Path) -> None:
    """Es gibt die Option nicht - und sie darf auch nicht erfunden werden."""
    backup = tmp_path / "b"
    backup.mkdir()

    _, runner, text = run_assistant(
        ["1", "1", str(tmp_path / "out"), "j", "j"], backups=(backup,), tmp_path=tmp_path
    )

    zusammen = " ".join(teil for befehl in runner.commands for teil in befehl)
    for verboten in ("--password", "--passwort", "--pass", "-p"):
        assert verboten not in zusammen
    assert "passwort" not in text.lower() or "eingetippt" in text.lower()
