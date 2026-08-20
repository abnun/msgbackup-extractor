"""Tests fuer SHA-256-Berechnung und -Vergleich."""

from __future__ import annotations

import hashlib
from pathlib import Path

from msgbackup_extractor.core import hashing

# Bekannte Vektoren - nicht selbst berechnet, sondern die dokumentierten Werte.
SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_known_vectors() -> None:
    assert hashing.hash_bytes(b"") == SHA256_EMPTY
    assert hashing.hash_bytes(b"abc") == SHA256_ABC


def test_hash_file_matches_hash_bytes(tmp_path: Path) -> None:
    payload = b"x" * 4096 + b"tail"
    target = tmp_path / "data.bin"
    target.write_bytes(payload)
    assert hashing.hash_file(target) == hashing.hash_bytes(payload)


def test_hash_file_streams_across_chunk_boundaries(tmp_path: Path) -> None:
    """Ein kleiner chunk_size darf das Ergebnis nicht veraendern."""
    payload = bytes(range(256)) * 40
    target = tmp_path / "data.bin"
    target.write_bytes(payload)
    assert hashing.hash_file(target, chunk_size=7) == hashlib.sha256(payload).hexdigest()


def test_hash_chunks_equals_hash_of_concatenation() -> None:
    chunks = [b"eins", b"zwei", b"drei"]
    assert hashing.hash_chunks(chunks) == hashing.hash_bytes(b"".join(chunks))


def test_tee_hash_passes_data_through_and_hashes_it() -> None:
    chunks = [b"a" * 10, b"b" * 20, b"c" * 5]
    iterator, digest = hashing.tee_hash(chunks)
    forwarded = b"".join(iterator)
    assert forwarded == b"".join(chunks)
    assert digest.hexdigest == hashing.hash_bytes(forwarded)
    assert digest.byte_count == 35


def test_tee_hash_is_empty_before_iteration() -> None:
    _, digest = hashing.tee_hash([b"data"])
    assert digest.byte_count == 0
    assert digest.hexdigest == SHA256_EMPTY


def test_compare_is_case_insensitive() -> None:
    assert hashing.compare(SHA256_ABC, SHA256_ABC.upper())
    assert not hashing.compare(SHA256_ABC, SHA256_EMPTY)
