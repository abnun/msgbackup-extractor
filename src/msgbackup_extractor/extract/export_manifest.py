"""Lesen und Schreiben von `export-manifest.json`.

Das Manifest ist die Nachweisebene des Exports: es sagt, was aus welcher Quelle
entstanden ist, wie gross es ist, welchen SHA-256 es hat und ob die
Integritaetspruefung bestanden wurde. `msgx verify` prueft spaeter allein daraus.

Es ist ausserdem die Datengrundlage eines spaeteren UI. Deshalb enthaelt es
Chat, Zeitstempel, Originaldateiname und die Verknuepfung Vorschau zu Original -
damit das UI die App-Datenbank nicht erneut lesen muss.

Was **nicht** hineinkommt: Nachrichtentexte, Kontaktdaten ausser dem
Chat-Anzeigenamen, Schluesselmaterial.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from msgbackup_extractor.models import ExtractedFile, ExtractionResult, FileOutcome, SourceKind

MANIFEST_NAME: Final = "export-manifest.json"
MANIFEST_VERSION: Final = 1


def _entry_to_dict(entry: ExtractedFile) -> dict[str, Any]:
    return {
        "outcome": entry.outcome.value,
        "source_kind": entry.source_kind.value,
        "source_file_id": entry.source_file_id,
        "source_domain": entry.source_domain,
        "source_table": entry.source_table,
        "source_row_id": entry.source_row_id,
        "output_path": entry.output_path,
        "link_paths": list(entry.link_paths),
        "size": entry.size,
        "sha256": entry.sha256,
        "media_type": entry.media_type,
        "detection_method": entry.detection_method,
        "extension_mismatch": entry.extension_mismatch,
        "chat_name": entry.chat_name,
        "chat_id": entry.chat_id,
        "original_filename": entry.original_filename,
        "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
        "timestamp_source": (
            entry.timestamp_source.value if entry.timestamp_source else None
        ),
        "is_thumbnail": entry.is_thumbnail,
        "thumbnail_of": entry.thumbnail_of,
        "duplicate_of": entry.duplicate_of,
        "integrity_ok": entry.integrity_ok,
        "error": entry.error,
    }


def build(
    result: ExtractionResult,
    *,
    app: str,
    backup_udid: str,
    tool_version: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Baut die Manifest-Struktur.

    Der Backup-Pfad steht bewusst **nicht** darin: das Manifest soll neben dem
    Export weitergegeben werden koennen, ohne den Ablageort des Originalbackups
    zu verraten. Die Geraete-ID genuegt zur Zuordnung.
    """
    counts = {outcome.value: result.count(outcome) for outcome in FileOutcome}
    return {
        "manifest_version": MANIFEST_VERSION,
        "tool": "msgbackup-extractor",
        "tool_version": tool_version,
        "app": app,
        "backup_udid": backup_udid,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "dry_run": result.dry_run,
        "summary": {
            "total": len(result.files),
            "successful": result.successful,
            "failed": result.failed,
            "integrity_errors": result.integrity_errors,
            "total_bytes": result.total_bytes,
            "outcomes": counts,
        },
        "files": [_entry_to_dict(entry) for entry in result.files],
    }


def write(payload: dict[str, Any], output_dir: Path) -> Path:
    """Schreibt das Manifest in das Ausgabeverzeichnis."""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / MANIFEST_NAME
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


class InvalidManifest(ValueError):
    """Die Datei ist kein verwertbares Export-Manifest."""


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """Ein Eintrag, wie er aus dem Manifest gelesen wird."""

    output_path: str | None
    sha256: str | None
    size: int | None
    outcome: str
    link_paths: tuple[str, ...] = ()
    is_thumbnail: bool = False
    source_kind: str = SourceKind.EXTERNAL_FILE.value

    @property
    def expects_file(self) -> bool:
        """Sollte zu diesem Eintrag eine Datei auf der Platte liegen?"""
        return (
            self.output_path is not None
            and self.sha256 is not None
            and self.outcome
            in (FileOutcome.EXTRACTED.value, FileOutcome.DUPLICATE.value)
        )


@dataclass(frozen=True, slots=True)
class LoadedManifest:
    """Das gelesene Manifest."""

    path: Path
    output_dir: Path
    app: str | None
    backup_udid: str | None
    generated_at: str | None
    dry_run: bool
    files: tuple[ManifestFile, ...]


def load(path: Path) -> LoadedManifest:
    """Liest ein Export-Manifest.

    Raises:
        InvalidManifest: Wenn die Datei fehlt, kein JSON ist oder die erwartete
            Struktur nicht hat.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InvalidManifest(f"Es gibt keine Datei {path.name}.") from error
    except json.JSONDecodeError as error:
        raise InvalidManifest(f"{path.name} ist kein gueltiges JSON: {error}") from error

    if not isinstance(raw, dict) or "files" not in raw:
        raise InvalidManifest(
            f"{path.name} sieht nicht wie ein Export-Manifest aus; "
            "der Schluessel 'files' fehlt."
        )
    if not isinstance(raw["files"], list):
        raise InvalidManifest(f"'files' in {path.name} ist keine Liste.")

    version = raw.get("manifest_version")
    if version is not None and version > MANIFEST_VERSION:
        raise InvalidManifest(
            f"Das Manifest hat Version {version}, dieses Programm kennt "
            f"hoechstens {MANIFEST_VERSION}. Bitte eine neuere Version verwenden."
        )

    files: list[ManifestFile] = []
    for index, entry in enumerate(raw["files"]):
        if not isinstance(entry, dict):
            raise InvalidManifest(f"Eintrag {index} in 'files' ist kein Objekt.")
        files.append(
            ManifestFile(
                output_path=entry.get("output_path"),
                sha256=entry.get("sha256"),
                size=entry.get("size"),
                outcome=str(entry.get("outcome", "")),
                link_paths=tuple(entry.get("link_paths") or ()),
                is_thumbnail=bool(entry.get("is_thumbnail")),
                source_kind=str(entry.get("source_kind", SourceKind.EXTERNAL_FILE.value)),
            )
        )

    return LoadedManifest(
        path=path,
        output_dir=path.parent,
        app=raw.get("app"),
        backup_udid=raw.get("backup_udid"),
        generated_at=raw.get("generated_at"),
        dry_run=bool(raw.get("dry_run")),
        files=tuple(files),
    )
