"""Gefuehrter Ablauf fuer den Doppelklick-Start (`msgx guide`).

Der Assistent fuehrt **nichts** selbst aus. Er stellt Fragen, baut daraus eine
Befehlszeile, zeigt sie an und laesst sie durch denselben Parser und dieselben
Handler laufen wie ein getippter Befehl. Das hat drei Gruende:

1. **Kein zweiter Codepfad.** Was der Assistent tut, ist genau das, was
   `msgx analyze` und `msgx extract` tun - einschliesslich aller Pruefungen,
   des Cloud-Waechters und der Berichte. Ein Assistent mit eigener
   Extraktionslogik waere eine zweite Wahrheit, die auseinanderlaufen kann.
2. **Kein Passwort im Argument.** Der Assistent kann keines uebergeben, weil es
   die Option nicht gibt. Nach dem Passwort fragt weiterhin `getpass` im
   laufenden Befehl, im Terminal, in dem der Assistent selbst laeuft.
3. **Er bringt das Werkzeug bei, statt es zu verstecken.** Jeder Schritt zeigt
   den Befehl, bevor er laeuft. Wer den Assistenten zweimal benutzt hat, kann
   ihn weglassen.

Der Assistent braucht ein Terminal. Ohne Eingabemoeglichkeit bricht er mit
einem Hinweis ab, statt auf eine Antwort zu warten, die nie kommt.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from .core import platforms
from .core.backup import (
    AppleBackup,
    BackupAccessError,
    NotABackupError,
    list_local_backups,
)

#: Messenger, die der Assistent zur Auswahl anbietet. Die Erkennung selbst
#: macht `analyze`; diese Liste ist nur die Auswahlhilfe danach.
KNOWN_APPS: Final = ("threema", "whatsapp")

#: Vorschlag fuer das Ausgabeverzeichnis. Bewusst nicht im Backup und bewusst
#: nicht in einem Cloud-Ordner; den Rest prueft der Waechter im echten Befehl.
DEFAULT_EXPORT_PARENT: Final = "messenger-extract/export"

EXIT_OK: Final = 0
EXIT_ABORTED: Final = 1
EXIT_NO_TERMINAL: Final = 2

#: Rueckgabewert eines Befehlslaufs: der Exitcode.
Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class BackupChoice:
    """Ein auswaehlbares Backup mit dem, was ohne Passwort lesbar ist."""

    path: Path
    label: str
    detail: str


def describe_backups(paths: Sequence[Path]) -> tuple[BackupChoice, ...]:
    """Liest Geraetename, iOS-Version und Verschluesselung aus den Plists.

    Beides ist auch bei einem verschluesselten Backup lesbar, es wird also
    kein Passwort gebraucht. Ein unlesbares Backup wird aufgefuehrt und als
    solches gekennzeichnet, statt zu verschwinden.
    """
    choices: list[BackupChoice] = []
    for path in paths:
        try:
            backup = AppleBackup(path)
            device = backup.device_info()
            label = device.device_name or "unbekanntes Geraet"
            state = "verschluesselt" if backup.is_encrypted else "unverschluesselt"
            detail = f"iOS {device.product_version or '?'}, {state}"
        except (NotABackupError, BackupAccessError) as error:
            label = path.name
            detail = f"nicht lesbar - {error}"
        choices.append(BackupChoice(path=path, label=label, detail=detail))
    return tuple(choices)


class Assistant:
    """Fragt, zeigt den Befehl, laesst ihn laufen.

    `runner` und die Stroeme sind einsetzbar, damit der Ablauf ohne echtes
    Backup und ohne Terminal geprueft werden kann.
    """

    def __init__(
        self,
        *,
        stdin: TextIO,
        stdout: TextIO,
        runner: Runner,
        home: Path | None = None,
        opener: Callable[[Path], None] | None = None,
        finder: Callable[[], Sequence[Path]] | None = None,
    ) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._runner = runner
        self._home = home if home is not None else Path.home()
        self._opener = opener if opener is not None else _open_with_system_viewer
        # Einsetzbar, damit der Ablauf ohne die Backups dieses Rechners
        # geprueft werden kann.
        self._finder = finder if finder is not None else list_local_backups

    # -- Ausgabe ----------------------------------------------------------

    def _say(self, text: str = "") -> None:
        print(text, file=self._stdout)

    def _rule(self, title: str) -> None:
        self._say()
        self._say(title)
        self._say("-" * len(title))

    # -- Eingabe ----------------------------------------------------------

    def _ask(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        self._stdout.write(f"{question}{suffix}: ")
        self._stdout.flush()
        answer = self._stdin.readline()
        if answer == "":
            # Eingabestrom zu Ende: kein Abbruch mit Fehler, sondern ein
            # sauberer Abbruch. Sonst haengt der Assistent in einer Pipe.
            raise _Aborted
        answer = answer.strip()
        return answer or default

    def _confirm(self, question: str, *, default: bool) -> bool:
        hint = "J/n" if default else "j/N"
        while True:
            answer = self._ask(f"{question} ({hint})").lower()
            if not answer:
                return default
            if answer in {"j", "ja", "y", "yes"}:
                return True
            if answer in {"n", "nein", "no"}:
                return False
            self._say("Bitte j oder n.")

    # -- Befehlsausfuehrung ------------------------------------------------

    def _run(self, argv: Sequence[str]) -> int:
        """Zeigt den Befehl und laesst ihn durch das echte CLI laufen."""
        self._say()
        self._say("Ich fuehre jetzt aus:")
        self._say("    msgx " + " ".join(shlex.quote(part) for part in argv))
        self._say()
        return self._runner(argv)

    # -- Schritte ----------------------------------------------------------

    def _greet(self) -> None:
        self._say("msgbackup-extractor")
        self._say("===================")
        self._say()
        self._say(
            "Holt Fotos, Videos, Sprachnachrichten und Dokumente aus einem\n"
            "lokalen iPhone-Backup in normale Ordner."
        )
        self._say()
        self._say(
            "Zwei Dinge vorweg:\n"
            "  * Das Backup wird nur gelesen und nie veraendert.\n"
            "  * Gedacht ist das fuer dein eigenes Backup."
        )
        self._say()
        self._say(
            "Jeder Schritt zeigt den Befehl, den er ausfuehrt. Du kannst den\n"
            "Assistenten also weglassen, sobald du sie kennst."
        )

    def _choose_backup(self) -> Path:
        self._rule("Schritt 1 von 4: Welches Backup?")
        try:
            found = self._finder()
        except BackupAccessError as error:
            self._say(f"Die Backups sind nicht lesbar: {error}")
            found = ()

        choices = describe_backups(found)
        if choices:
            self._say("Gefunden (neueste zuerst):")
            self._say()
            for number, choice in enumerate(choices, start=1):
                self._say(f"  {number}) {choice.label}")
                self._say(f"     {choice.detail}")
                self._say(f"     {choice.path}")
            self._say()
            answer = self._ask(
                "Nummer waehlen, oder einen Pfad eingeben", default="1"
            )
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return choices[int(answer) - 1].path
            return Path(answer).expanduser()

        self._say(
            "Kein Backup an den ueblichen Orten gefunden. Gesucht wurde auf "
            f"{platforms.platform_name()} unter:"
        )
        for location in platforms.backup_locations(self._home):
            self._say(f"    {location.path}")
        self._say()
        return Path(self._ask("Pfad zum Backup-Verzeichnis")).expanduser()

    def _choose_output(self, app: str) -> Path:
        self._rule("Schritt 3 von 4: Wohin damit?")
        default = self._home / DEFAULT_EXPORT_PARENT / app
        self._say(
            "Ein lokales Verzeichnis, das nicht in eine Cloud synchronisiert.\n"
            "Ein Cloud-Ordner wird abgelehnt, damit deine Nachrichten nicht\n"
            "hochgeladen werden."
        )
        self._say()
        return Path(self._ask("Ausgabeverzeichnis", default=str(default))).expanduser()

    def _choose_app(self) -> str:
        self._rule("Schritt 2 von 4: Welcher Messenger?")
        self._say("Der Bericht oben nennt, was im Backup steckt.")
        self._say()
        for number, name in enumerate(KNOWN_APPS, start=1):
            self._say(f"  {number}) {name}")
        self._say()
        answer = self._ask("Nummer oder Name", default="1")
        if answer.isdigit() and 1 <= int(answer) <= len(KNOWN_APPS):
            return KNOWN_APPS[int(answer) - 1]
        return answer.lower()

    def _open_result(self, output: Path) -> None:
        page = output / "index.html"
        if not page.exists():
            return
        self._say()
        if self._confirm("Die Ansicht jetzt oeffnen?", default=True):
            self._opener(page)
        else:
            self._say(f"Du findest sie unter: {page}")

    # -- Ablauf ------------------------------------------------------------

    def run(self) -> int:
        try:
            return self._flow()
        except _Aborted:
            self._say()
            self._say("Abgebrochen. Es wurde nichts geschrieben.")
            return EXIT_ABORTED
        except KeyboardInterrupt:
            self._say()
            self._say("Abgebrochen.")
            return EXIT_ABORTED

    def _flow(self) -> int:
        self._greet()
        backup = self._choose_backup()

        code = self._run(["analyze", "--backup", str(backup)])
        if code != EXIT_OK:
            self._say()
            self._say(
                "Die Analyse ist nicht durchgelaufen. Der Bericht oben nennt den\n"
                "Grund; es wurde nichts geschrieben."
            )
            return code

        app = self._choose_app()
        output = self._choose_output(app)

        self._rule("Schritt 4 von 4: Probelauf")
        self._say(
            "Der Probelauf schreibt nichts. Er zeigt, was der echte Lauf tun\n"
            "wuerde - und zwar aus demselben Plan, den der echte Lauf ausfuehrt."
        )
        base = ["extract", "--backup", str(backup), "--output", str(output), "--app", app]
        code = self._run([*base, "--dry-run"])
        if code != EXIT_OK:
            self._say()
            self._say("Der Probelauf ist nicht durchgelaufen. Es wurde nichts geschrieben.")
            return code

        self._say()
        if not self._confirm("Jetzt wirklich extrahieren?", default=False):
            self._say()
            self._say("Gut. Es wurde nichts geschrieben.")
            return EXIT_OK

        code = self._run(base)
        if code != EXIT_OK:
            self._say()
            self._say(
                "Der Lauf hat gemeldet, dass etwas nicht in Ordnung war. Der\n"
                "Bericht oben nennt jede betroffene Datei."
            )
            return code

        self._open_result(output)
        self._say()
        self._say("Fertig. Naechstes Mal genuegt:")
        self._say(
            f"    msgx extract --backup {shlex.quote(str(backup))} "
            f"--output {shlex.quote(str(output))}"
        )
        return EXIT_OK


class _Aborted(Exception):
    """Der Eingabestrom ist zu Ende oder der Anwender hat abgebrochen."""


def _open_with_system_viewer(path: Path) -> None:
    command = (*platforms.open_command(), str(path))
    # Lokaler Aufruf des Standardprogramms. Kein Netzzugriff, keine Shell.
    subprocess.run(command, check=False, shell=False)


def make_runner(extra: Sequence[str] = ()) -> Runner:
    """Ein Runner, der jede Befehlszeile durch den echten Parser schickt.

    `extra` sind Schalter, die an jeden Schritt angehaengt werden - damit
    `--verbose` auf dem Assistenten auch in den Schritten wirkt und nicht nur
    auf dem Assistenten selbst, der gar nichts ausgibt, was sie betrifft.
    """
    zusatz = tuple(extra)

    def runner(argv: Sequence[str]) -> int:
        from .cli import build_parser

        parser = build_parser()
        arguments = parser.parse_args([*argv, *zusatz])
        handler = getattr(arguments, "handler", None)
        if handler is None:  # pragma: no cover - nur bei Programmierfehler
            raise AssertionError(f"Kein Handler fuer {argv!r}")
        return int(handler(arguments))

    return runner


#: Bequemer Name fuer den Runner ohne Zusatzschalter.
def default_runner(argv: Sequence[str]) -> int:
    return make_runner()(argv)
