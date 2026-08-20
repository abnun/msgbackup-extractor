"""Gemeinsame pytest-Fixtures.

Alle Backups hier sind synthetisch. Es werden nie echte private Daten geladen.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from msgbackup_extractor.analysis import AnalysisReport, Analyzer
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.session import BackupSession
from tests.support.backup_builder import (
    BackupFile,
    BuiltBackup,
    build_backup,
    core_data_database,
)

#: Realistische Datei-Signaturen fuer die Medienerkennung.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"
PDF = b"%PDF-1.7\n%\xc3\xa4\xc3\xb6\n"
SQLITE = b"SQLite format 3\x00"
ZIP = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"

TEST_PASSWORD = "korrekt-horse-batterie-klammer"

THREEMA_BUNDLE_ID = "ch.threema.iapp"
THREEMA_GROUP_DOMAIN = "AppDomainGroup-group.ch.threema.iapp"
THREEMA_APP_DOMAIN = f"AppDomain-{THREEMA_BUNDLE_ID}"


def sample_files() -> list[BackupFile]:
    """Ein Satz Dateien, der alle interessanten Faelle abdeckt."""
    modified = datetime(2025, 3, 14, 18, 42, 11)
    return [
        BackupFile(THREEMA_APP_DOMAIN, "Documents/img/photo1.jpg", JPEG + b"A" * 500,
                   last_modified=modified),
        BackupFile(THREEMA_APP_DOMAIN, "Documents/img/photo2.png", PNG + b"B" * 300,
                   last_modified=modified),
        BackupFile(THREEMA_APP_DOMAIN, "Documents/img/photo3.heic", HEIC + b"C" * 400),
        BackupFile(THREEMA_APP_DOMAIN, "Documents/video/clip.mp4", MP4 + b"D" * 900),
        BackupFile(THREEMA_APP_DOMAIN, "Documents/audio/voice.m4a", M4A + b"E" * 200),
        BackupFile(THREEMA_APP_DOMAIN, "Documents/docs/handbuch.pdf", PDF + b"F" * 250),
        # Echter Core-Data-Store, damit die Datenbankklassifikation getestet wird.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/ThreemaData.sqlite", core_data_database()),
        # SQLite-Signatur ohne gueltigen Inhalt - muss als unlesbar gemeldet werden.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/kaputt.sqlite", SQLITE + b"G" * 600),
        BackupFile(THREEMA_GROUP_DOMAIN, "Library/Shared/shared.jpg", JPEG + b"H" * 150),
        # Endung widerspricht dem Inhalt - muss als Mismatch erkannt werden.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/img/getarnt.txt", PNG + b"I" * 100),
        # Verzeichniseintrag, keine Nutzdatei.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/img", b"", flags=2, mode=0o40755),
        # Eintrag ohne Nutzdatei auf der Platte.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/fehlt.jpg", JPEG + b"J" * 50,
                   omit_payload=True),
        # Abgeschnittene Nutzdatei.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/abgeschnitten.mp4", MP4 + b"K" * 900,
                   truncate_payload_to=32),
        # Kaputtes MBFile-Blob.
        BackupFile(THREEMA_APP_DOMAIN, "Documents/kaputte-metadaten.jpg", JPEG + b"L" * 80,
                   corrupt_metadata=True),
        # Fremde App - darf nicht als Threema gelten.
        BackupFile("AppDomain-com.apple.Maps", "Documents/karte.png", PNG + b"M" * 60),
        # Systemdomain.
        BackupFile(
            "HomeDomain", "Library/Preferences/com.apple.test.plist", b"bplist00" + b"N" * 40
        ),
    ]


@pytest.fixture
def plain_backup(tmp_path: Path) -> BuiltBackup:
    """Unverschluesseltes Backup mit installiertem Threema."""
    return build_backup(
        tmp_path / "plain",
        sample_files(),
        installed_applications=[THREEMA_BUNDLE_ID, "com.apple.Maps"],
        application_versions={THREEMA_BUNDLE_ID: "6.1.2"},
    )


@pytest.fixture
def encrypted_backup(tmp_path: Path) -> BuiltBackup:
    """Verschluesseltes Backup mit bekanntem Passwort."""
    return build_backup(
        tmp_path / "encrypted",
        sample_files(),
        password=TEST_PASSWORD,
        installed_applications=[THREEMA_BUNDLE_ID],
        application_versions={THREEMA_BUNDLE_ID: "6.1.2"},
    )


@pytest.fixture
def backup_without_threema(tmp_path: Path) -> BuiltBackup:
    """Backup ohne Threema - muss zu NOT_FOUND fuehren, nicht zu einer Annahme."""
    return build_backup(
        tmp_path / "no-threema",
        [
            BackupFile("AppDomain-com.apple.Maps", "Documents/karte.png", PNG + b"X" * 60),
            BackupFile("HomeDomain", "Library/Preferences/a.plist", b"bplist00" + b"Y" * 20),
        ],
        installed_applications=["com.apple.Maps"],
    )


# ---------------------------------------------------------------------------
# Analyse-Helfer
# ---------------------------------------------------------------------------


@contextmanager
def analysis_session(
    backup: BuiltBackup, *, password: str | None = None
) -> Iterator[BackupSession]:
    """Oeffnet eine Session auf einem synthetischen Backup.

    `password=None` bedeutet: nicht nach dem Passwort fragen. Bei einem
    verschluesselten Backup entsteht dadurch der Teilzugriff - genau der Fall,
    den der Teilbericht abdeckt.
    """
    provider = (lambda: password) if password is not None else None
    with BackupSession(AppleBackup(backup.path), password_provider=provider) as session:
        yield session


def analyze(
    backup: BuiltBackup, *, password: str | None = None, **kwargs: object
) -> AnalysisReport:
    """Fuehrt eine vollstaendige Analyse aus und gibt den Bericht zurueck.

    Achtung: `DatabaseReport.readable_path` verweist bei verschluesselten
    Backups auf eine Datei im Arbeitsverzeichnis der Session und ist nach der
    Rueckgabe nicht mehr gueltig. Wer den Pfad braucht, nutzt
    `analysis_session()` direkt.
    """
    with analysis_session(backup, password=password) as session:
        return Analyzer(session, **kwargs).run()  # type: ignore[arg-type]
