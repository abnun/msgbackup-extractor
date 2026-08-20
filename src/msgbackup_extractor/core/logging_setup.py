"""Logging mit strukturellem Schutz gegen das Durchsickern von Inhalten.

Der Ansatz ist zweistufig:

1. **Struktur.** Aufrufer loggen Zaehler, IDs, Domains, Typen und Groessen.
   Nachrichteninhalte werden nirgends in eine Logzeile gegeben. Wer bewusst
   etwas Sensibles loggen will, markiert den Record mit `extra={"sensitive":
   True}` - solche Records werden verworfen, nicht ausgegeben.

2. **Backstop.** Ein Filter maskiert Muster, die typischerweise
   personenbezogen sind (E-Mail-Adressen, Telefonnummern, Threema-IDs) und
   Pfade, sofern `show_paths` nicht ausdruecklich gesetzt ist. Der Backstop
   ersetzt nicht die Disziplin aus Punkt 1 - er faengt Versehen ab.

Passwoerter erscheinen hier gar nicht, weil sie nie an den Logger gegeben
werden. Der Filter kennt zusaetzlich `password`-artige Schluesselwoerter.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Final

LOGGER_NAME: Final = "msgbackup_extractor"

MASK: Final = "[redacted]"

#: Muster, die immer maskiert werden - auch im Verbose-Modus.
_ALWAYS_MASK: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # E-Mail-Adressen
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), MASK),
    # Internationale Telefonnummern (mind. 7 Ziffern nach dem Plus)
    (re.compile(r"\+\d[\d\s/().-]{6,}\d"), MASK),
    # Als Wert hinter einem passwortartigen Schluesselwort
    (
        re.compile(
            r"(?i)\b(pass(?:word|phrase)|passwort|kennwort|secret|token|key)\b\s*[:=]\s*\S+"
        ),
        r"\1=" + MASK,
    ),
    # Threema-ID: genau acht Zeichen A-Z/0-9, explizit als solche benannt
    (re.compile(r"(?i)\bthreema[- ]?id\b\s*[:=]?\s*[A-Z0-9]{8}\b"), "threema-id=" + MASK),
)

#: Pfadmuster - nur maskiert, wenn `show_paths` False ist.
_PATH_PATTERN: Final = re.compile(
    r"(?:/|(?<=\s)|^)(?:[\w .~@%+-]+/){1,}[\w .~@%+-]*(?:\.\w{1,8})?"
)


class RedactionFilter(logging.Filter):
    """Maskiert personenbezogene Muster und verwirft sensibel markierte Records."""

    def __init__(self, *, show_paths: bool = False) -> None:
        super().__init__()
        self.show_paths = show_paths

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "sensitive", False):
            return False

        message = record.getMessage()
        cleaned = self.redact(message)
        if cleaned != message:
            # Args sind bereits eingearbeitet; erneutes Formatieren verhindern.
            record.msg = cleaned
            record.args = ()
        return True

    def redact(self, text: str) -> str:
        for pattern, replacement in _ALWAYS_MASK:
            text = pattern.sub(replacement, text)
        if not self.show_paths:
            text = _PATH_PATTERN.sub(self._mask_path, text)
        return text

    @staticmethod
    def _mask_path(match: re.Match[str]) -> str:
        """Ersetzt einen Pfad durch Platzhalter, behaelt aber die Endung.

        Die Endung bleibt sichtbar, weil sie fuer die Fehlersuche nuetzlich und
        fuer sich genommen nicht personenbezogen ist.
        """
        value = match.group(0)
        leading = "/" if value.startswith("/") else ""
        suffix = Path(value).suffix
        return f"{leading}{MASK}{suffix}"


def _formatter(verbose: bool) -> logging.Formatter:
    if verbose:
        return logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s:%(lineno)d  %(message)s",
            datefmt="%H:%M:%S",
        )
    return logging.Formatter(fmt="%(levelname)-8s %(message)s")


def configure_logging(
    *,
    verbose: bool = False,
    show_paths: bool = False,
    log_file: Path | None = None,
) -> logging.Logger:
    """Richtet den Paket-Logger ein und gibt ihn zurueck.

    Ausgabe geht nach stderr, damit stdout fuer Berichte frei bleibt.
    `log_file` erhaelt denselben Filter, aber niemals Klartextpfade: dort ist
    `show_paths` immer False, weil Dateilogs den Prozess ueberleben.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_formatter(verbose))
    console.addFilter(RedactionFilter(show_paths=show_paths))
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(_formatter(verbose=True))
        # Dateilogs nie mit Klartextpfaden.
        file_handler.addFilter(RedactionFilter(show_paths=False))
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Logger fuer ein Untermodul, z.B. `get_logger("manifest")`."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
