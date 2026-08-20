"""Read-only-Zugriff auf SQLite-Datenbanken und Schema-Introspektion.

Der entscheidende Punkt ist `immutable=1`. Mit `mode=ro` allein legt SQLite bei
Bedarf weiterhin `-wal`-, `-shm`- oder Journal-Dateien neben der Originaldatei
an - das waere eine Veraenderung des Backups, obwohl kein Byte der Datenbank
selbst geschrieben wurde. `immutable=1` sagt SQLite zu, dass sich die Datei
nicht aendert, und unterbindet damit jedes Nebenprodukt.

Introspektion statt Annahmen: Der Aufrufer erfaehrt, welche Tabellen und Spalten
tatsaechlich da sind, und entscheidet danach. Eine erwartete Struktur wird
nirgends vorausgesetzt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.models import TableSchema

logger = get_logger("sqlite")

#: Tabellen, die SQLite selbst verwaltet.
_INTERNAL_TABLE_PREFIX = "sqlite_"


class NotASQLiteDatabase(ValueError):
    """Die Datei ist keine (lesbare) SQLite-Datenbank."""


SQLITE_MAGIC = b"SQLite format 3\x00"


def looks_like_sqlite(path: Path) -> bool:
    """Prueft die 16-Byte-Signatur, ohne die Datenbank zu oeffnen."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


#: Zeichen, die in einer SQLite-URI unkodiert stehen duerfen. Alles andere wird
#: prozentkodiert - insbesondere "?", "#" und "%", weil sie sonst als
#: URI-Syntax gelesen wuerden und den Pfad abschneiden.
_URI_SAFE: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/"
)


def _percent_encode(text: str) -> str:
    """Prozentkodierung fuer SQLite-URI-Pfade.

    Absichtlich handgeschrieben statt `urllib.parse.quote`: dieses Paket
    importiert bewusst kein Modul aus dem `urllib`-Namensraum, damit die Zusage
    "keine Netzwerkfunktionalitaet" von `tests/test_no_network.py` streng
    geprueft werden kann, ohne Ausnahmen zu definieren.
    """
    parts: list[str] = []
    for character in text:
        if character in _URI_SAFE:
            parts.append(character)
        else:
            parts.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
    return "".join(parts)


def readonly_uri(path: Path, *, immutable: bool = True) -> str:
    """Baut die read-only URI. Sonderzeichen im Pfad werden korrekt kodiert."""
    encoded = _percent_encode(str(path.expanduser().resolve()))
    flags = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"file:{encoded}?{flags}"


@contextmanager
def open_readonly(path: Path, *, immutable: bool = True) -> Iterator[sqlite3.Connection]:
    """Oeffnet eine SQLite-Datenbank strikt lesend.

    Args:
        immutable: Sollte nur dann False sein, wenn die Datenbank tatsaechlich
            nebenlaeufig veraendert werden koennte. Fuer Backupdateien nie.

    Raises:
        NotASQLiteDatabase: Wenn die Datei keine lesbare Datenbank ist.
    """
    if not path.is_file():
        raise NotASQLiteDatabase(f"Datei existiert nicht: {path.name}")
    if not looks_like_sqlite(path):
        raise NotASQLiteDatabase(
            f"{path.name} beginnt nicht mit der SQLite-Signatur. Bei einem "
            "verschluesselten Backup muss die Datei zuerst entschluesselt werden."
        )

    connection = sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        # Fruehe, guenstige Pruefung, dass die Datei wirklich lesbar ist.
        connection.execute("SELECT 1").fetchone()
        yield connection
    except sqlite3.DatabaseError as error:
        raise NotASQLiteDatabase(f"{path.name} ist nicht lesbar: {error}") from error
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Introspektion
# ---------------------------------------------------------------------------


def list_tables(connection: sqlite3.Connection, *, include_views: bool = False) -> tuple[str, ...]:
    """Alle Tabellennamen der Datenbank, ohne SQLite-interne."""
    kinds = ("table", "view") if include_views else ("table",)
    placeholders = ",".join("?" * len(kinds))
    rows = connection.execute(
        f"SELECT name FROM sqlite_master WHERE type IN ({placeholders}) ORDER BY name",
        kinds,
    ).fetchall()
    return tuple(r[0] for r in rows if not r[0].startswith(_INTERNAL_TABLE_PREFIX))


def _quote_identifier(name: str) -> str:
    """Quotet einen Tabellennamen fuer PRAGMA-Aufrufe.

    Tabellennamen kommen aus der Datenbank und koennen Anfuehrungszeichen
    enthalten; PRAGMA erlaubt keine Parameterbindung.
    """
    return '"' + name.replace('"', '""') + '"'


def describe_table(
    connection: sqlite3.Connection, name: str, *, count_rows: bool = True
) -> TableSchema:
    """Liest Spalten, Primaerschluessel und Fremdschluessel einer Tabelle."""
    quoted = _quote_identifier(name)

    columns: list[str] = []
    column_types: dict[str, str] = {}
    primary_key: list[tuple[int, str]] = []
    for row in connection.execute(f"PRAGMA table_info({quoted})"):
        columns.append(row["name"])
        column_types[row["name"]] = (row["type"] or "").upper()
        if row["pk"]:
            primary_key.append((row["pk"], row["name"]))

    foreign_keys: list[tuple[str, str, str]] = []
    for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"):
        foreign_keys.append((row["from"], row["table"], row["to"]))

    row_count: int | None = None
    if count_rows:
        try:
            row_count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        except sqlite3.DatabaseError as error:
            logger.debug("Zeilen von Tabelle %s nicht zaehlbar: %s", name, error)

    return TableSchema(
        name=name,
        columns=tuple(columns),
        column_types=column_types,
        primary_key=tuple(column for _, column in sorted(primary_key)),
        foreign_keys=tuple(foreign_keys),
        row_count=row_count,
    )


def describe_database(
    connection: sqlite3.Connection, *, count_rows: bool = True, include_views: bool = False
) -> dict[str, TableSchema]:
    """Vollstaendige Introspektion: Tabellenname -> Schema."""
    return {
        name: describe_table(connection, name, count_rows=count_rows)
        for name in list_tables(connection, include_views=include_views)
    }


def find_table(schemas: dict[str, TableSchema], *candidates: str) -> TableSchema | None:
    """Sucht eine Tabelle case-insensitiv unter mehreren moeglichen Namen.

    Gibt None zurueck, wenn keine passt - der Aufrufer erzeugt dann einen
    Diagnosebericht statt zu raten.
    """
    lowered = {name.lower(): schema for name, schema in schemas.items()}
    for candidate in candidates:
        schema = lowered.get(candidate.lower())
        if schema is not None:
            return schema
    return None
