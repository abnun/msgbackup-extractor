"""Pfadsicherheit: Sanitisierung, Output-Guard, Cloud-Guard, Namensschema.

Zwei Gefahren werden hier abgewehrt:

1. **Path Traversal.** `relativePath` stammt aus dem Backup und ist damit
   nicht vertrauenswuerdig. Werte wie `../../etc/passwd`, absolute Pfade,
   NUL-Bytes oder Namen wie `..` duerfen nicht dazu fuehren, dass irgendetwas
   ausserhalb von `--output` geschrieben wird.

2. **Unbeabsichtigter Cloud-Upload.** Liegt das Ausgabeverzeichnis in einem
   Sync-Container, laedt das Betriebssystem die extrahierten Daten hoch. Das
   Programm selbst kommuniziert nicht, das Ergebnis waere aber dasselbe.
   Deshalb wird das verweigert, statt es nur zu erwaehnen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

MAX_COMPONENT_LENGTH: Final = 200

#: Zeichen, die in Dateinamen auf macOS/APFS Probleme machen oder als
#: Trennzeichen missverstanden werden koennen.
_UNSAFE_CHARS: Final = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")

#: Namen, die auf keinem der ueblichen Dateisysteme als Komponente taugen.
_RESERVED_NAMES: Final = frozenset(
    {"", ".", "..", "con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_PLACEHOLDER: Final = "unbenannt"


# ---------------------------------------------------------------------------
# Cloud-Sync-Erkennung
# ---------------------------------------------------------------------------

#: Relative Pfade unterhalb von $HOME, die als synchronisiert gelten.
_CLOUD_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("Library/Mobile Documents", "iCloud Drive"),
    ("Library/CloudStorage", "macOS Cloud Storage (Provider-Mount)"),
    ("Dropbox", "Dropbox"),
    ("OneDrive", "Microsoft OneDrive"),
    ("Google Drive", "Google Drive"),
    ("pCloud Drive", "pCloud"),
    ("Sync", "Sync.com"),
    ("Nextcloud", "Nextcloud"),
    ("ownCloud", "ownCloud"),
    ("Seafile", "Seafile"),
    ("MEGA", "MEGA"),
)


class CloudSyncedPathError(ValueError):
    """Der Pfad liegt in einem Cloud-Sync-Container."""


def detect_cloud_provider(path: Path, *, home: Path | None = None) -> str | None:
    """Gibt den Namen des Sync-Anbieters zurueck, oder None.

    Die Pruefung ist rein pfadbasiert und damit offline. Sie erkennt die
    ueblichen Ablagen, kann aber keinen beliebig konfigurierten Sync-Ordner
    kennen - das ist eine Grenze, die in der README steht.
    """
    home = home or Path.home()
    try:
        resolved = path.expanduser().resolve()
    except OSError:  # pragma: no cover - defektes Dateisystem
        resolved = path.expanduser().absolute()

    for marker, provider in _CLOUD_MARKERS:
        candidate = (home / marker).resolve() if (home / marker).exists() else home / marker
        if resolved == candidate or candidate in resolved.parents:
            return provider
        # Anbieter wie OneDrive haengen Firmennamen an: "OneDrive - Musterfirma"
        for parent in (resolved, *resolved.parents):
            if parent.parent == home and parent.name.startswith(marker):
                return provider
    return None


def require_non_cloud_path(path: Path, *, purpose: str, allow: bool = False) -> None:
    """Verweigert Cloud-Pfade, sofern nicht ausdruecklich erlaubt.

    Args:
        purpose: Wird in die Fehlermeldung uebernommen, z.B. "Ausgabeverzeichnis".
        allow: Bei True wird nur nicht geprueft; die Warnung gibt der Aufrufer aus.
    """
    if allow:
        return
    provider = detect_cloud_provider(path)
    if provider is not None:
        raise CloudSyncedPathError(
            f"Das {purpose} liegt in {provider}. Damit wuerden die extrahierten "
            f"Daten vom Betriebssystem in die Cloud hochgeladen. Waehle ein "
            f"lokales Verzeichnis, oder erzwinge es mit --allow-cloud-output."
        )


# ---------------------------------------------------------------------------
# Sanitisierung
# ---------------------------------------------------------------------------


def sanitize_component(name: str, *, placeholder: str = _PLACEHOLDER) -> str:
    """Macht einen einzelnen Pfadbestandteil dateisystemsicher.

    Normalisiert auf NFC (APFS speichert NFD, was zu verwirrenden Duplikaten
    fuehrt), entfernt Steuerzeichen und Trennzeichen, kuerzt auf eine
    vertretbare Laenge und ersetzt reservierte Namen.
    """
    normalized = unicodedata.normalize("NFC", name)
    cleaned = _UNSAFE_CHARS.sub("_", normalized).strip().strip(".")
    if cleaned.lower() in _RESERVED_NAMES:
        return placeholder
    if len(cleaned) > MAX_COMPONENT_LENGTH:
        stem = PurePosixPath(cleaned).stem[: MAX_COMPONENT_LENGTH - 20]
        suffix = PurePosixPath(cleaned).suffix[:20]
        cleaned = f"{stem}{suffix}"
    return cleaned or placeholder


def sanitize_relative_path(relative_path: str) -> PurePosixPath:
    """Wandelt einen Backup-`relativePath` in einen unbedenklichen Relativpfad.

    Absolute Pfade verlieren ihren Anker, `..` wird verworfen, jede Komponente
    wird einzeln saniert. Das Ergebnis kann nie aus dem Zielverzeichnis
    herausfuehren.
    """
    parts: list[str] = []
    for raw in PurePosixPath(relative_path.replace("\\", "/")).parts:
        if raw in ("/", ".", ".."):
            continue
        component = sanitize_component(raw)
        if component:
            parts.append(component)
    return PurePosixPath(*parts) if parts else PurePosixPath(_PLACEHOLDER)


# ---------------------------------------------------------------------------
# Output-Guard
# ---------------------------------------------------------------------------


class OutputGuardError(ValueError):
    """Ein Schreibziel liegt ausserhalb des erlaubten Bereichs."""


@dataclass(slots=True)
class OutputGuard:
    """Waechter ueber allem, was geschrieben wird.

    Jeder Schreibpfad muss durch `resolve()` gehen. Das Ergebnis liegt garantiert
    innerhalb von `root` und garantiert nicht innerhalb von `forbidden_roots`
    (dort steht der Backup-Pfad).
    """

    root: Path
    forbidden_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        self.root = self.root.expanduser().resolve()
        self.forbidden_roots = tuple(
            p.expanduser().resolve() for p in self.forbidden_roots if p is not None
        )
        for forbidden in self.forbidden_roots:
            if self.root == forbidden or forbidden in self.root.parents:
                raise OutputGuardError(
                    "Das Ausgabeverzeichnis liegt innerhalb des Backups. Der Export "
                    "muss in ein davon getrenntes Verzeichnis gehen."
                )
            if self.root in forbidden.parents:
                raise OutputGuardError(
                    "Das Ausgabeverzeichnis enthaelt das Backup. Waehle ein "
                    "Verzeichnis, das das Backup nicht umschliesst."
                )

    def resolve(self, relative: str | PurePosixPath | Path) -> Path:
        """Loest einen relativen Zielpfad innerhalb von `root` auf."""
        candidate = Path(str(relative))
        if candidate.is_absolute():
            raise OutputGuardError(f"Absoluter Zielpfad ist nicht erlaubt: {candidate.name}")

        target = (self.root / candidate).resolve()
        if target != self.root and self.root not in target.parents:
            raise OutputGuardError(
                "Der Zielpfad wuerde das Ausgabeverzeichnis verlassen "
                f"(Komponente: {candidate.name})"
            )
        for forbidden in self.forbidden_roots:
            if target == forbidden or forbidden in target.parents:
                raise OutputGuardError("Der Zielpfad liegt im Backup-Verzeichnis")
        return target

    def prepare(self, relative: str | PurePosixPath | Path) -> Path:
        """Wie `resolve()`, legt aber zusaetzlich das Elternverzeichnis an."""
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


def unique_path(target: Path) -> Path:
    """Haengt bei Kollision `-1`, `-2`, ... an, statt zu ueberschreiben.

    Ueberschreiben waere Datenverlust: zwei Backup-Eintraege koennen nach der
    Sanitisierung denselben Namen ergeben, ohne denselben Inhalt zu haben.
    """
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for counter in range(1, 10_000):
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
    raise OutputGuardError(f"Zu viele Namenskollisionen fuer {target.name}")
