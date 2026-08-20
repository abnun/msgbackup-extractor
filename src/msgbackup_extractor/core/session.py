"""Backup-Session: buendelt Oeffnen, Passwort, Keybag und Manifest-Zugriff.

Diese Schicht existiert, damit `analyze`, `database` und spaeter `extract`
denselben Weg zum lesbaren Manifest nehmen - egal ob das Backup verschluesselt
ist oder nicht.

Bei einem verschluesselten Backup wird die Manifest.db in ein temporaeres
Arbeitsverzeichnis **ausserhalb des Backups** entschluesselt und beim Verlassen
der Session wieder geloescht. Das Backup selbst wird nur gelesen.

Das Passwort wird ausschliesslich hier erfragt, ausschliesslich ueber `getpass`,
und nur dann, wenn es tatsaechlich gebraucht wird.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.core.encryption import (
    BackupKeys,
    DecryptionError,
    decrypt_manifest_database,
)
from msgbackup_extractor.core.keybag import Keybag, KeybagParseError, parse_keybag
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.core.secure_memory import transient_password

logger = get_logger("session")

MANIFEST_WORK_NAME = "Manifest-decrypted.db"


class PasswordProvider(Protocol):
    """Liefert das Backup-Passwort.

    Bewusst als Protokoll: die Produktion nutzt `getpass`, Tests uebergeben eine
    Funktion. Ein CLI-Argument gibt es nicht und soll es nicht geben.
    """

    def __call__(self) -> str: ...


def interactive_password() -> str:
    """Fragt das Passwort interaktiv ab. Kein Echo, keine Speicherung."""
    with transient_password("Passwort des verschluesselten Backups: ") as password:
        return password


class PasswordRequired(RuntimeError):
    """Das Backup ist verschluesselt, aber es steht kein Passwort zur Verfuegung."""

    def __init__(self) -> None:
        super().__init__(
            "Das Backup ist verschluesselt. Fuer die vollstaendige Analyse ist "
            "das Backup-Passwort noetig."
        )


@dataclass(slots=True)
class ManifestAccess:
    """Wie das Manifest erreichbar ist."""

    path: Path
    #: True, wenn die Datei fuer den Zugriff entschluesselt werden musste.
    was_decrypted: bool
    #: Grund, falls kein Zugriff moeglich war.
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.unavailable_reason is None


class BackupSession:
    """Zugriff auf ein Backup, notfalls mit Entschluesselung.

        with BackupSession(backup, password_provider=interactive_password) as session:
            if session.manifest.is_available:
                with ManifestReader(session.manifest.path) as reader:
                    ...

    Ohne `password_provider` wird bei einem verschluesselten Backup nicht nach
    dem Passwort gefragt; die Session liefert dann einen Teilzugriff mit
    Begruendung. Das ist der Modus fuer den Teilbericht.
    """

    def __init__(
        self,
        backup: AppleBackup,
        *,
        password_provider: PasswordProvider | None = None,
        work_dir: Path | None = None,
    ) -> None:
        """
        Args:
            work_dir: Verzeichnis fuer die entschluesselte Manifest.db. Ohne
                Angabe wird ein temporaeres Verzeichnis angelegt und beim
                Verlassen geloescht. Es liegt nie im Backup.
        """
        self.backup = backup
        self._password_provider = password_provider
        self._requested_work_dir = work_dir
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.work_dir: Path | None = None
        self.keybag: Keybag | None = None
        self.keys: BackupKeys | None = None
        self.manifest = ManifestAccess(
            path=backup.manifest_db_path, was_decrypted=False,
            unavailable_reason="Session nicht geoeffnet",
        )

    # -- Lebenszyklus -------------------------------------------------------

    def __enter__(self) -> Self:
        encryption = self.backup.encryption

        if not encryption.is_encrypted:
            self.manifest = ManifestAccess(
                path=self.backup.manifest_db_path, was_decrypted=False
            )
            return self

        if self._password_provider is None:
            self.manifest = ManifestAccess(
                path=self.backup.manifest_db_path,
                was_decrypted=False,
                unavailable_reason=(
                    "Das Backup ist verschluesselt. Ohne Passwort sind nur die "
                    "Metadaten aus Info.plist und Manifest.plist auswertbar."
                ),
            )
            return self

        try:
            self.keybag = parse_keybag(encryption.keybag or b"")
        except KeybagParseError as error:
            self.manifest = ManifestAccess(
                path=self.backup.manifest_db_path,
                was_decrypted=False,
                unavailable_reason=str(error),
            )
            return self

        # Passwort erst hier erfragen - nachdem klar ist, dass es gebraucht wird
        # und der Keybag ueberhaupt verwertbar ist.
        self.keys = BackupKeys.from_password(self.keybag, self._password_provider())

        if encryption.manifest_key is None:
            # Aeltere Backups: Nutzdaten verschluesselt, Manifest im Klartext.
            logger.debug("Kein ManifestKey vorhanden; Manifest.db gilt als unverschluesselt")
            self.manifest = ManifestAccess(
                path=self.backup.manifest_db_path, was_decrypted=False
            )
            return self

        work_dir = self.ensure_work_dir()
        try:
            decrypted = decrypt_manifest_database(
                self.backup.manifest_db_path,
                work_dir / MANIFEST_WORK_NAME,
                self.keys,
                encryption.manifest_key,
            )
        except DecryptionError as error:
            self.manifest = ManifestAccess(
                path=self.backup.manifest_db_path,
                was_decrypted=False,
                unavailable_reason=str(error),
            )
            return self

        self.manifest = ManifestAccess(path=decrypted, was_decrypted=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Loescht Schluessel und temporaere Dateien."""
        if self.keys is not None:
            self.keys.wipe()
            self.keys = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
            self.work_dir = None

    def ensure_work_dir(self) -> Path:
        """Legt bei Bedarf das Arbeitsverzeichnis an und gibt es zurueck.

        Es liegt nie im Backup. Ohne ausdruecklich uebergebenes `work_dir` ist
        es temporaer und wird beim Schliessen der Session geloescht.
        """
        self._ensure_work_dir()
        assert self.work_dir is not None
        return self.work_dir

    def _ensure_work_dir(self) -> None:
        if self.work_dir is not None:
            return
        if self._requested_work_dir is not None:
            self._requested_work_dir.mkdir(parents=True, exist_ok=True)
            self.work_dir = self._requested_work_dir
            return
        self._temporary = tempfile.TemporaryDirectory(prefix="msgbackup-")
        self.work_dir = Path(self._temporary.name)

    # -- Eigenschaften ------------------------------------------------------

    @property
    def is_encrypted(self) -> bool:
        return self.backup.is_encrypted

    @property
    def has_keys(self) -> bool:
        return self.keys is not None and bool(self.keys.available_classes)

    def __repr__(self) -> str:
        return (
            f"BackupSession(encrypted={self.is_encrypted}, "
            f"manifest_available={self.manifest.is_available}, "
            f"keys={self.has_keys})"
        )
