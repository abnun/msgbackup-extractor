"""Profil fuer Signal (iOS).

Dieses Profil kann in einem iOS-Backup mit hoher Wahrscheinlichkeit **nichts**
extrahieren, und genau das ist seine Aufgabe: es soll den Grund nennen, statt
dass ein leeres Ergebnis wie ein Fehler des Programms aussieht.

Am echten Backup vermessen: die App ist installiert und belegt fuenf Domains,
aber diese enthalten zusammen nur eine Handvoll Dateien mit wenigen Dutzend
Kilobyte - ausschliesslich Einstellungs-Plists,
WebKit-Caches, eine Lock-Datei und eine Textdatei. Es gibt keine
Nachrichtendatenbank und keine Mediendateien.

Der Grund liegt bei Signal, nicht beim Backup: die App schliesst ihr
Datenverzeichnis vom iOS-Backup aus. Wer seine Signal-Daten sichern will,
braucht Signals eigenen Ausfuehrungsweg (Uebertragung auf ein neues Geraet oder
ein Signal-eigenes Backup), nicht ein Apple-Backup.

Sollte Signal das aendern, greift dieses Profil dennoch: die erwarteten
Tabellennamen stehen als Kandidaten in `SIGNAL_TABLE_CANDIDATES`, und die
Erkennung laeuft ueber den Bundle-Namensraum, nicht ueber Pfade.
"""

from __future__ import annotations

from typing import Final

from msgbackup_extractor.apps.base import (
    AppProfile,
    DatabaseCandidate,
    DatabaseRole,
    MediaContext,
    MediaEnumeration,
)
from msgbackup_extractor.apps.core_data import SchemaView
from msgbackup_extractor.core.logging_setup import get_logger

logger = get_logger("signal")

#: Tabellen, die Signals Nachrichtendatenbank (GRDB) enthalten wuerde. Sie sind
#: **Kandidaten zur Pruefung**, keine Behauptung: in einem iOS-Backup war noch
#: keine davon vorhanden, weil die Datenbank nicht mitgesichert wird.
SIGNAL_TABLE_CANDIDATES: Final = ("model_TSInteraction", "model_TSThread")

#: Was in einem Backup von Signal tatsaechlich auftaucht.
_EXPECTED_LEFTOVERS: Final = (
    "Einstellungs-Plists",
    "WebKit-Caches",
    "eine Lock-Datei",
)

_EXPLANATION: Final = (
    "Signal schliesst sein Datenverzeichnis vom iOS-Backup aus. Im Backup "
    "liegen nur " + ", ".join(_EXPECTED_LEFTOVERS) + " - keine "
    "Nachrichtendatenbank und keine Medien. Das ist kein Fehler dieses "
    "Programms und laesst sich hier auch nicht umgehen: die Daten sind schlicht "
    "nicht enthalten. Signal-Daten uebertraegt man mit Signals eigenem Weg "
    "(Geraetewechsel oder Signal-Backup)."
)


class SignalProfile(AppProfile):
    """Erkennt Signal und erklaert, warum nichts zu holen ist."""

    name = "Signal"
    slug = "signal"

    #: Signal verwendet historisch den Namensraum von Open Whisper Systems.
    bundle_namespaces = ("org.whispersystems.",)

    known_bundle_ids = ("org.whispersystems.signal",)

    group_namespaces = ("group.org.whispersystems.signal",)

    def requires_tables(self) -> tuple[str, ...]:
        return SIGNAL_TABLE_CANDIDATES

    def enumerate_media(self, context: MediaContext) -> MediaEnumeration:
        """Prueft, ob Signals Datenbank ausnahmsweise doch vorliegt."""
        schema = SchemaView(context.schemas)
        found = [t for t in SIGNAL_TABLE_CANDIDATES if schema.has_table(t)]
        if len(found) == len(SIGNAL_TABLE_CANDIDATES):
            # Sollte Signal das Ausschliessen aufgeben, waere hier der Ort fuer
            # eine Zuordnung. Bis dahin nichts erfinden.
            return MediaEnumeration(
                notes=(
                    "Unerwartet: Signals Nachrichtentabellen sind vorhanden. "
                    "Eine Zuordnung ist noch nicht implementiert, weil dieser "
                    "Fall an keinem echten Backup vermessen werden konnte.",
                ),
                unsupported_reason=(
                    "Signals Nachrichtentabellen wurden gefunden, aber die "
                    "Zuordnung ist nicht implementiert. Bitte melden - dann "
                    "kann sie gegen echte Daten gebaut werden."
                ),
            )

        logger.debug("Signal-Datenbank nicht im Backup (erwartet)")
        return MediaEnumeration(unsupported_reason=_EXPLANATION)

    def classify_databases(
        self, candidates: tuple[DatabaseCandidate, ...]
    ) -> tuple[DatabaseRole, ...]:
        """Ordnet gefundene Datenbanken zu - bei Signal sind es fremde."""
        roles: list[DatabaseRole] = []
        for candidate in candidates:
            tables = {t.upper() for t in candidate.tables}
            expected = {t.upper() for t in SIGNAL_TABLE_CANDIDATES}
            if expected & tables:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="messages",
                        reason="Enthaelt Signals Nachrichtentabellen.",
                        confidence="high",
                    )
                )
            else:
                roles.append(
                    DatabaseRole(
                        candidate=candidate,
                        role="unknown",
                        reason=(
                            "Gehoert nicht zu Signals Nachrichtendaten. In "
                            "Signal-Domains liegen im Backup nur Datenbanken von "
                            "Apple-Frameworks, etwa WebKit-Statistiken."
                        ),
                        confidence="medium",
                    )
                )
        return tuple(roles)
