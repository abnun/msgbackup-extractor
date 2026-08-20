"""Streamendes SHA-256.

Die Hashes dienen zwei Zwecken: Integritaetspruefung (Quelle gegen Ziel) und
Duplikaterkennung. Beides erfordert, dass grosse Videos nie vollstaendig in den
Speicher gelesen werden.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, Final

#: 1 MiB - Kompromiss aus Syscall-Anzahl und Speicherbedarf.
CHUNK_SIZE: Final = 1024 * 1024

ALGORITHM: Final = "sha256"


def hash_bytes(data: bytes | bytearray | memoryview) -> str:
    """SHA-256 als Hex-String."""
    return hashlib.sha256(data).hexdigest()


def hash_stream(stream: BinaryIO, *, chunk_size: int = CHUNK_SIZE) -> str:
    """SHA-256 eines offenen Binaerstreams ab der aktuellen Position."""
    digest = hashlib.sha256()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    """SHA-256 einer Datei. Oeffnet ausschliesslich lesend."""
    with path.open("rb") as handle:
        return hash_stream(handle, chunk_size=chunk_size)


def hash_chunks(chunks: Iterable[bytes]) -> str:
    """SHA-256 ueber eine Folge von Bloecken."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def tee_hash(chunks: Iterable[bytes]) -> tuple[Iterator[bytes], "_Digest"]:
    """Laesst Bloecke durch und hasht sie im Vorbeigehen.

    So wird der Quellhash beim Schreiben gebildet, ohne die Daten ein zweites
    Mal zu lesen. Der Hex-Wert steht erst zur Verfuegung, wenn der Iterator
    erschoepft ist.
    """
    digest = _Digest()

    def _iterator() -> Iterator[bytes]:
        for chunk in chunks:
            digest.update(chunk)
            yield chunk

    return _iterator(), digest


class _Digest:
    """Veraenderlicher Hash-Akkumulator fuer `tee_hash`."""

    __slots__ = ("_digest", "_bytes")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._bytes = 0

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self._bytes += len(chunk)

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    @property
    def byte_count(self) -> int:
        return self._bytes


def compare(source_hash: str, destination_hash: str) -> bool:
    """Vergleicht zwei Hex-Digests unabhaengig von Gross-/Kleinschreibung."""
    import hmac

    return hmac.compare_digest(source_hash.lower(), destination_hash.lower())
