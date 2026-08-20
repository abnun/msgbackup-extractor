"""Nachtraegliche Pruefung eines Exports anhand seines Manifests.

`msgx verify --manifest PFAD` liest ausschliesslich das Manifest und die
exportierten Dateien. Das Backup wird dafuer nicht gebraucht - die Pruefung
funktioniert also auch auf einer Kopie des Exports, Jahre spaeter, auf einem
anderen Rechner.

Geprueft wird je Eintrag: existiert die Datei, stimmt ihre Groesse, stimmt ihr
SHA-256, und existieren die zusaetzlichen Verknuepfungen.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from msgbackup_extractor.core.hashing import compare, hash_file
from msgbackup_extractor.core.logging_setup import get_logger
from msgbackup_extractor.extract.export_manifest import LoadedManifest, ManifestFile

logger = get_logger("verify")


class VerifyStatus(enum.StrEnum):
    """Ergebnis der Pruefung eines Eintrags."""

    OK = "ok"
    MISSING = "missing"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNREADABLE = "unreadable"
    #: Eintraege ohne erwartete Datei, z.B. fehlgeschlagene oder Probelaeufe.
    SKIPPED = "skipped"
    #: Datei da und korrekt, aber eine Verknuepfung fehlt.
    LINK_MISSING = "link_missing"


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    """Pruefergebnis eines Eintrags."""

    output_path: str | None
    status: VerifyStatus
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    expected_size: int | None = None
    actual_size: int | None = None
    missing_links: tuple[str, ...] = ()

    @property
    def is_ok(self) -> bool:
        return self.status in (VerifyStatus.OK, VerifyStatus.SKIPPED)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Gesamtergebnis."""

    manifest: LoadedManifest
    files: tuple[VerifiedFile, ...]

    def count(self, status: VerifyStatus) -> int:
        return sum(1 for f in self.files if f.status is status)

    @property
    def checked(self) -> int:
        return sum(1 for f in self.files if f.status is not VerifyStatus.SKIPPED)

    @property
    def problems(self) -> tuple[VerifiedFile, ...]:
        return tuple(f for f in self.files if not f.is_ok)

    @property
    def is_intact(self) -> bool:
        return not self.problems


def _verify_entry(entry: ManifestFile, output_dir: Path) -> VerifiedFile:
    if not entry.expects_file:
        return VerifiedFile(output_path=entry.output_path, status=VerifyStatus.SKIPPED)

    assert entry.output_path is not None
    path = output_dir / entry.output_path
    if not path.is_file():
        return VerifiedFile(
            output_path=entry.output_path,
            status=VerifyStatus.MISSING,
            expected_sha256=entry.sha256,
            expected_size=entry.size,
        )

    actual_size = path.stat().st_size
    if entry.size is not None and actual_size != entry.size:
        return VerifiedFile(
            output_path=entry.output_path,
            status=VerifyStatus.SIZE_MISMATCH,
            expected_sha256=entry.sha256,
            expected_size=entry.size,
            actual_size=actual_size,
        )

    try:
        actual = hash_file(path)
    except OSError:
        return VerifiedFile(
            output_path=entry.output_path,
            status=VerifyStatus.UNREADABLE,
            expected_sha256=entry.sha256,
            expected_size=entry.size,
            actual_size=actual_size,
        )

    if not compare(actual, entry.sha256 or ""):
        return VerifiedFile(
            output_path=entry.output_path,
            status=VerifyStatus.HASH_MISMATCH,
            expected_sha256=entry.sha256,
            actual_sha256=actual,
            expected_size=entry.size,
            actual_size=actual_size,
        )

    missing_links = tuple(
        link for link in entry.link_paths if not (output_dir / link).exists()
    )
    if missing_links:
        return VerifiedFile(
            output_path=entry.output_path,
            status=VerifyStatus.LINK_MISSING,
            expected_sha256=entry.sha256,
            actual_sha256=actual,
            expected_size=entry.size,
            actual_size=actual_size,
            missing_links=missing_links,
        )

    return VerifiedFile(
        output_path=entry.output_path,
        status=VerifyStatus.OK,
        expected_sha256=entry.sha256,
        actual_sha256=actual,
        expected_size=entry.size,
        actual_size=actual_size,
    )


def verify(manifest: LoadedManifest) -> VerifyResult:
    """Prueft alle Eintraege eines Manifests."""
    results = tuple(_verify_entry(entry, manifest.output_dir) for entry in manifest.files)
    logger.debug("%d Eintraege geprueft", len(results))
    return VerifyResult(manifest=manifest, files=results)
