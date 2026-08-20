"""Tests fuer den read-only SQLite-Zugriff und die Schema-Introspektion.

Der wichtigste Test hier ist `test_no_side_files_are_created`: mit `mode=ro`
allein legt SQLite bei Bedarf Journal-Dateien neben der Originaldatei an. Das
waere eine Veraenderung des Backups.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from msgbackup_extractor.core import sqlite_ro
from msgbackup_extractor.core.sqlite_ro import NotASQLiteDatabase
from tests.support.backup_builder import BackupFile, BuiltBackup, build_backup


@pytest.fixture
def sample_db(tmp_path: Path) -> Path:
    """Eine kleine Datenbank mit Fremdschluesseln, WAL-Modus aktiv."""
    path = tmp_path / "chat.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE Conversations (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL
        );
        CREATE TABLE Messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER REFERENCES Conversations(id),
            body TEXT
        );
        CREATE VIEW RecentMessages AS SELECT * FROM Messages;
        INSERT INTO Conversations (id, title) VALUES (1, 'Familie');
        INSERT INTO Messages (id, conversation_id, body) VALUES (1, 1, 'geheim');
        INSERT INTO Messages (id, conversation_id, body) VALUES (2, 1, 'auch geheim');
        """
    )
    connection.commit()
    connection.close()
    return path


# ---------------------------------------------------------------------------
# Signaturpruefung
# ---------------------------------------------------------------------------


def test_looks_like_sqlite(sample_db: Path, tmp_path: Path) -> None:
    assert sqlite_ro.looks_like_sqlite(sample_db)
    other = tmp_path / "nicht.db"
    other.write_bytes(b"kein sqlite")
    assert not sqlite_ro.looks_like_sqlite(other)
    assert not sqlite_ro.looks_like_sqlite(tmp_path / "existiert-nicht.db")


def test_open_readonly_rejects_missing_file(tmp_path: Path) -> None:
    with (
        pytest.raises(NotASQLiteDatabase, match="existiert nicht"),
        sqlite_ro.open_readonly(tmp_path / "fehlt.db"),
    ):
        pass


def test_open_readonly_rejects_non_sqlite(tmp_path: Path) -> None:
    target = tmp_path / "muell.db"
    target.write_bytes(b"\x00\x01\x02" * 100)
    with (
        pytest.raises(NotASQLiteDatabase, match="SQLite-Signatur"),
        sqlite_ro.open_readonly(target),
    ):
        pass


def test_encrypted_manifest_gives_actionable_error(tmp_path: Path) -> None:
    """Die Meldung muss auf die noetige Entschluesselung hinweisen."""
    backup = build_backup(
        tmp_path / "enc",
        [BackupFile("AppDomain-x", "Documents/a.bin", b"x" * 10)],
        password="pw",
        installed_applications=["x"],
    )
    with (
        pytest.raises(NotASQLiteDatabase, match="entschluesselt"),
        sqlite_ro.open_readonly(backup.path / "Manifest.db"),
    ):
        pass


# ---------------------------------------------------------------------------
# Read-only-Verhalten
# ---------------------------------------------------------------------------


def test_uri_contains_readonly_and_immutable_flags(sample_db: Path) -> None:
    uri = sqlite_ro.readonly_uri(sample_db)
    assert "mode=ro" in uri
    assert "immutable=1" in uri


def test_uri_encodes_special_characters(tmp_path: Path) -> None:
    """`?` und `#` wuerden den Pfad sonst abschneiden, `%` ihn verfaelschen."""
    directory = tmp_path / "mit Leerzeichen & Zeichen ? # 100% Müller"
    directory.mkdir()
    path = directory / "a.db"
    path.write_bytes(sqlite_ro.SQLITE_MAGIC + b"\x00" * 100)

    uri = sqlite_ro.readonly_uri(path)
    assert " " not in uri
    assert "%20" in uri
    # Nach dem einen Fragezeichen, das die Flags einleitet, darf keines mehr stehen.
    assert uri.count("?") == 1
    assert "%3F" in uri and "%23" in uri and "%25" in uri
    assert uri.endswith("?mode=ro&immutable=1")


def test_database_in_directory_with_special_characters_can_be_opened(
    tmp_path: Path,
) -> None:
    """Die Kodierung muss nicht nur huebsch aussehen, sondern funktionieren."""
    directory = tmp_path / "Ordner ? # 100% Müller"
    directory.mkdir()
    path = directory / "chat.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE T (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO T VALUES (7)")
    connection.commit()
    connection.close()

    with sqlite_ro.open_readonly(path) as readonly:
        assert readonly.execute("SELECT id FROM T").fetchone()[0] == 7


def test_writes_are_rejected(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO Conversations (id, title) VALUES (2, 'neu')")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DROP TABLE Messages")


def test_no_side_files_are_created(sample_db: Path) -> None:
    """Kernzusage: es entstehen keine -wal-, -shm- oder Journal-Dateien."""
    before = {p.name for p in sample_db.parent.iterdir()}
    with sqlite_ro.open_readonly(sample_db) as connection:
        connection.execute("SELECT COUNT(*) FROM Messages").fetchone()
        sqlite_ro.describe_database(connection)
    after = {p.name for p in sample_db.parent.iterdir()}
    assert after == before


def test_file_is_not_modified(sample_db: Path) -> None:
    before = (sample_db.read_bytes(), sample_db.stat().st_mtime_ns)
    with sqlite_ro.open_readonly(sample_db) as connection:
        sqlite_ro.describe_database(connection)
    assert (sample_db.read_bytes(), sample_db.stat().st_mtime_ns) == before


# ---------------------------------------------------------------------------
# Introspektion
# ---------------------------------------------------------------------------


def test_list_tables_excludes_internal_and_views(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        assert sqlite_ro.list_tables(connection) == ("Conversations", "Messages")
        assert "RecentMessages" in sqlite_ro.list_tables(connection, include_views=True)


def test_describe_table_reports_columns_and_types(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schema = sqlite_ro.describe_table(connection, "Messages")
    assert schema.columns == ("id", "conversation_id", "body")
    assert schema.column_types["conversation_id"] == "INTEGER"
    assert schema.primary_key == ("id",)
    assert schema.row_count == 2


def test_describe_table_reports_foreign_keys(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schema = sqlite_ro.describe_table(connection, "Messages")
    assert ("conversation_id", "Conversations", "id") in schema.foreign_keys


def test_describe_table_can_skip_row_count(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schema = sqlite_ro.describe_table(connection, "Messages", count_rows=False)
    assert schema.row_count is None


def test_table_schema_has_helper(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schema = sqlite_ro.describe_table(connection, "Messages")
    assert schema.has("id", "body")
    assert not schema.has("id", "gibt-es-nicht")


def test_describe_database_covers_all_tables(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schemas = sqlite_ro.describe_database(connection)
    assert set(schemas) == {"Conversations", "Messages"}


def test_find_table_is_case_insensitive(sample_db: Path) -> None:
    with sqlite_ro.open_readonly(sample_db) as connection:
        schemas = sqlite_ro.describe_database(connection)
    assert sqlite_ro.find_table(schemas, "messages") is not None
    assert sqlite_ro.find_table(schemas, "ZMESSAGE", "Messages") is not None


def test_find_table_returns_none_instead_of_guessing(sample_db: Path) -> None:
    """Kein Treffer heisst None - der Aufrufer erzeugt dann eine Diagnose."""
    with sqlite_ro.open_readonly(sample_db) as connection:
        schemas = sqlite_ro.describe_database(connection)
    assert sqlite_ro.find_table(schemas, "ZMESSAGE", "Chats") is None


def test_identifiers_with_quotes_are_handled(tmp_path: Path) -> None:
    """Tabellennamen kommen aus der Datenbank und koennen Quotes enthalten."""
    path = tmp_path / "odd.db"
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE "weird""name" (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()
    with sqlite_ro.open_readonly(path) as ro:
        schema = sqlite_ro.describe_table(ro, 'weird"name')
    assert schema.columns == ("id",)


def test_manifest_db_of_plain_backup_is_introspectable(plain_backup: BuiltBackup) -> None:
    with sqlite_ro.open_readonly(plain_backup.path / "Manifest.db") as connection:
        schemas = sqlite_ro.describe_database(connection)
    files = sqlite_ro.find_table(schemas, "Files")
    assert files is not None
    assert files.has("fileID", "domain", "relativePath", "flags", "file")
    assert files.row_count == len(plain_backup.files)
