"""Gemeinsames fuer Messenger, die Core Data verwenden.

Threema und WhatsApp legen ihre Daten beide in einem Core-Data-Store ab. Was
sie teilen, steht hier - vor allem die Zeitrechnung und das Vermessen von
Beziehungsrichtungen. Zwei Implementierungen derselben Sache wuerden
auseinanderlaufen, und bei der Zeitrechnung waere das besonders teuer: eine
verwechselte Epoche verschiebt alle Datumsangaben um 31 Jahre.

Was sie **nicht** teilen, ist die Ablage der Medien: Threema speichert Blobs in
der Datenbank oder in `_EXTERNAL_DATA`, WhatsApp echte Dateien mit Pfadangabe in
der Datenbank. Das bleibt Sache des jeweiligen Profils.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from msgbackup_extractor.core.logging_setup import get_logger

logger = get_logger("core-data")

#: Verwaltungstabellen, die jeden Core-Data-Store ausweisen.
CORE_DATA_MARKERS: Final = ("Z_METADATA", "Z_PRIMARYKEY")

#: Core Data zaehlt Zeitstempel ab 2001. MBFile im Apple-Backup zaehlt dagegen
#: ab 1970 - siehe `core/manifest.py`. Die beiden nicht zu verwechseln ist
#: wichtig: der Unterschied betraegt 31 Jahre.
APPLE_EPOCH: Final = datetime(2001, 1, 1, tzinfo=UTC)

#: Vor dem ersten iPhone gab es keine iOS-Nachrichten.
EARLIEST_PLAUSIBLE: Final = datetime(2007, 1, 1, tzinfo=UTC)


def quote(identifier: str) -> str:
    """Quotet einen Tabellen- oder Spaltennamen fuer SQL.

    Namen kommen aus der Datenbank und koennen Anfuehrungszeichen enthalten;
    PRAGMA und Spaltenlisten erlauben keine Parameterbindung.
    """
    return '"' + identifier.replace('"', '""') + '"'


def apple_datetime(value: object, *, now: datetime | None = None) -> datetime | None:
    """Core-Data-Zeitstempel (Sekunden seit 2001) in ein `datetime`.

    Die Obergrenze ist "jetzt" mit einem Tag Spielraum fuer Uhrenabweichungen:
    eine Nachricht kann nicht in der Zukunft gesendet worden sein. Genau diese
    Pruefung deckt eine verwechselte Epoche sofort auf, statt sie erst in
    Jahrzehnten sichtbar werden zu lassen.

    Unplausible Werte werden zu None - eine erfundene Zeit ist schlimmer als
    keine Zeit.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        stamp = APPLE_EPOCH + timedelta(seconds=float(value))
    except (OverflowError, ValueError, OSError):
        return None
    upper = (now or datetime.now(UTC)) + timedelta(days=1)
    if not (EARLIEST_PLAUSIBLE <= stamp <= upper):
        logger.debug("Unplausibler Core-Data-Zeitstempel verworfen: %s", stamp.isoformat())
        return None
    return stamp


def is_core_data(table_names: set[str]) -> bool:
    """Ist das ein Core-Data-Store? Nachweisbar an den Verwaltungstabellen."""
    upper = {name.upper() for name in table_names}
    return all(marker in upper for marker in CORE_DATA_MARKERS)


@dataclass(frozen=True, slots=True)
class Direction:
    """Ergebnis der Vermessung einer Beziehung."""

    from_table: str
    from_column: str
    to_table: str
    matched: int
    total: int

    @property
    def carries(self) -> bool:
        """Traegt diese Richtung den Fremdschluessel?"""
        return self.total > 0 and self.matched > 0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.matched == self.total

    @property
    def evidence(self) -> str:
        return f"{self.from_table}.{self.from_column} -> {self.to_table}.Z_PK"

    def __repr__(self) -> str:
        return f"Direction({self.evidence}, {self.matched}/{self.total})"


def measure_direction(
    connection: sqlite3.Connection,
    from_table: str,
    from_column: str,
    to_table: str,
) -> Direction:
    """Zaehlt, wie viele Werte einer Spalte auf einen Z_PK der Zieltabelle zeigen.

    Core Data legt den Fremdschluessel je Beziehung nur auf **einer** Seite ab,
    und welche das ist, unterscheidet sich zwischen den Entitaeten. Am echten
    Backup ist `ZIMAGEDATA.ZMESSAGE` (Threema) zu 100 % verwaist, waehrend bei
    WhatsApp beide Richtungen tragen. Deshalb wird gemessen statt angenommen.
    """
    matched, total = connection.execute(
        f"SELECT COUNT(b.Z_PK), COUNT(a.{quote(from_column)}) "
        f"FROM {quote(from_table)} a "
        f"LEFT JOIN {quote(to_table)} b ON b.Z_PK = a.{quote(from_column)} "
        f"WHERE a.{quote(from_column)} IS NOT NULL"
    ).fetchone()
    return Direction(
        from_table=from_table,
        from_column=from_column,
        to_table=to_table,
        matched=matched or 0,
        total=total or 0,
    )


class SchemaView:
    """Bequemer, fehlertoleranter Blick auf ein vorgefundenes Schema."""

    __slots__ = ("_columns",)

    def __init__(self, schemas: dict[str, object]) -> None:
        self._columns: dict[str, set[str]] = {
            name.upper(): {c.upper() for c in getattr(schema, "columns", ())}
            for name, schema in schemas.items()
        }

    def has_table(self, table: str) -> bool:
        return table.upper() in self._columns

    def has(self, table: str, *columns: str) -> bool:
        available = self._columns.get(table.upper())
        if available is None:
            return False
        return all(column.upper() in available for column in columns)

    def present(self, table: str, columns: tuple[str, ...]) -> tuple[str, ...]:
        """Die Teilmenge der Spalten, die es tatsaechlich gibt."""
        return tuple(column for column in columns if self.has(table, column))

    @property
    def tables(self) -> set[str]:
        return set(self._columns)
