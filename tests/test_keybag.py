"""Tests fuer das TLV-Parsing des Backup-Keybags.

Reines Formatparsing - hier findet keine Kryptografie statt. Geprueft wird, dass
gueltige Keybags korrekt zerlegt werden und dass defekte Keybags eine
aussagekraeftige Meldung ergeben statt stiller Fehlinterpretation.
"""

from __future__ import annotations

import struct

import pytest

from msgbackup_extractor.core.keybag import (
    DOUBLE_PBKDF_VERSION,
    WRAPPED_KEY_LENGTH,
    KeybagParseError,
    iterate_tlv,
    parse_keybag,
)
from tests.support.backup_builder import DEFAULT_CLASSES, build_keybag


def tlv(tag: str, value: bytes | int) -> bytes:
    payload = struct.pack(">I", value) if isinstance(value, int) else value
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


# ---------------------------------------------------------------------------
# TLV-Zerlegung
# ---------------------------------------------------------------------------


def test_iterate_tlv_splits_elements() -> None:
    blob = tlv("VERS", 4) + tlv("SALT", b"s" * 20) + tlv("ITER", 10000)
    assert iterate_tlv(blob) == [
        ("VERS", b"\x00\x00\x00\x04"),
        ("SALT", b"s" * 20),
        ("ITER", b"\x00\x00\x27\x10"),
    ]


def test_iterate_tlv_accepts_empty_input() -> None:
    assert iterate_tlv(b"") == []


def test_iterate_tlv_accepts_zero_length_values() -> None:
    assert iterate_tlv(tlv("HMCK", b"")) == [("HMCK", b"")]


def test_iterate_tlv_rejects_truncated_header() -> None:
    with pytest.raises(KeybagParseError, match="Abgeschnittenes TLV-Element"):
        iterate_tlv(b"VERS\x00\x00")


def test_iterate_tlv_rejects_length_beyond_data() -> None:
    with pytest.raises(KeybagParseError, match="gibt 100 Byte an"):
        iterate_tlv(b"VERS" + struct.pack(">I", 100) + b"kurz")


def test_iterate_tlv_rejects_non_ascii_tag() -> None:
    with pytest.raises(KeybagParseError, match="Kein ASCII-Tag"):
        iterate_tlv(b"\xff\xfe\xfd\xfc" + struct.pack(">I", 0))


# ---------------------------------------------------------------------------
# Vollstaendiger Keybag
# ---------------------------------------------------------------------------


def test_parses_all_header_fields() -> None:
    fixture = build_keybag("geheim", iterations=1234, version=4)
    keybag = parse_keybag(fixture.blob)
    assert keybag.version == 4
    assert keybag.keybag_type == 1
    assert keybag.iterations == 1234
    assert keybag.double_iterations == 1234
    assert len(keybag.salt) == 20
    assert keybag.double_salt is not None and len(keybag.double_salt) == 20
    assert keybag.uuid is not None and len(keybag.uuid) == 16
    assert keybag.hmac_key is not None


def test_parses_all_class_keys() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    assert keybag.available_classes == DEFAULT_CLASSES
    for protection_class in DEFAULT_CLASSES:
        class_key = keybag.class_keys[protection_class]
        assert class_key.protection_class == protection_class
        assert len(class_key.wrapped_key) == WRAPPED_KEY_LENGTH


def test_header_uuid_is_not_mistaken_for_a_class() -> None:
    """Das erste UUID gehoert zum Kopf, nicht zum ersten Class-Block."""
    keybag = parse_keybag(build_keybag("geheim", classes=(1, 2, 3)).blob)
    assert keybag.available_classes == (1, 2, 3)
    assert keybag.uuid != keybag.class_keys[1].uuid


def test_double_derivation_is_detected() -> None:
    assert parse_keybag(build_keybag("g").blob).uses_double_derivation


def test_legacy_keybag_without_dpsl_uses_single_derivation() -> None:
    blob = (
        tlv("VERS", 2)
        + tlv("TYPE", 1)
        + tlv("UUID", b"u" * 16)
        + tlv("SALT", b"s" * 20)
        + tlv("ITER", 10000)
        + tlv("UUID", b"c" * 16)
        + tlv("CLAS", 3)
        + tlv("WRAP", 2)
        + tlv("WPKY", b"k" * WRAPPED_KEY_LENGTH)
    )
    keybag = parse_keybag(blob)
    assert not keybag.uses_double_derivation
    assert keybag.version < DOUBLE_PBKDF_VERSION
    assert keybag.available_classes == (3,)


def test_wrap_flags_are_preserved() -> None:
    keybag = parse_keybag(build_keybag("g", classes=(3,)).blob)
    assert keybag.class_keys[3].wrap == 2
    assert keybag.class_keys[3].is_passcode_wrapped
    assert keybag.passcode_wrapped_classes == (3,)


def test_device_only_class_is_not_passcode_wrapped() -> None:
    """Klassen ohne Passcode-Wrap sind aus einem Backup heraus nicht entpackbar."""
    blob = (
        tlv("VERS", 4)
        + tlv("UUID", b"u" * 16)
        + tlv("SALT", b"s" * 20)
        + tlv("ITER", 1000)
        + tlv("UUID", b"c" * 16)
        + tlv("CLAS", 5)
        + tlv("WRAP", 1)  # nur Device-Key
        + tlv("WPKY", b"k" * WRAPPED_KEY_LENGTH)
    )
    keybag = parse_keybag(blob)
    assert not keybag.class_keys[5].is_passcode_wrapped
    assert keybag.passcode_wrapped_classes == ()


# ---------------------------------------------------------------------------
# Fehlerfaelle
# ---------------------------------------------------------------------------


def test_empty_keybag_is_rejected_with_explanation() -> None:
    with pytest.raises(KeybagParseError, match="keinen BackupKeyBag"):
        parse_keybag(b"")


def test_missing_salt_is_rejected() -> None:
    blob = tlv("VERS", 4) + tlv("ITER", 1000)
    with pytest.raises(KeybagParseError, match="SALT"):
        parse_keybag(blob)


def test_missing_iterations_is_rejected() -> None:
    blob = tlv("VERS", 4) + tlv("SALT", b"s" * 20)
    with pytest.raises(KeybagParseError, match="ITER"):
        parse_keybag(blob)


@pytest.mark.parametrize("iterations", [0, 200_000_000])
def test_implausible_iteration_count_is_rejected(iterations: int) -> None:
    blob = tlv("VERS", 4) + tlv("SALT", b"s" * 20) + tlv("ITER", iterations)
    with pytest.raises(KeybagParseError, match="Unplausible Iterationszahl"):
        parse_keybag(blob)


def test_keybag_without_class_keys_is_rejected() -> None:
    blob = tlv("VERS", 4) + tlv("SALT", b"s" * 20) + tlv("ITER", 1000)
    with pytest.raises(KeybagParseError, match="keine verwertbaren Klassenschluessel"):
        parse_keybag(blob)


def test_class_key_of_wrong_length_is_skipped_not_used() -> None:
    """Ein Schluessel falscher Laenge wird verworfen, nicht halb verwendet."""
    blob = (
        tlv("VERS", 4)
        + tlv("UUID", b"u" * 16)
        + tlv("SALT", b"s" * 20)
        + tlv("ITER", 1000)
        + tlv("UUID", b"c" * 16)
        + tlv("CLAS", 3)
        + tlv("WPKY", b"zu-kurz")
        + tlv("UUID", b"d" * 16)
        + tlv("CLAS", 4)
        + tlv("WPKY", b"k" * WRAPPED_KEY_LENGTH)
    )
    keybag = parse_keybag(blob)
    assert keybag.available_classes == (4,)


def test_non_integer_iter_field_is_rejected() -> None:
    blob = tlv("VERS", 4) + tlv("SALT", b"s" * 20) + tlv("ITER", b"\x01\x02")
    with pytest.raises(KeybagParseError, match="ITER sollte 4 Byte"):
        parse_keybag(blob)


# ---------------------------------------------------------------------------
# Kein Schluesselmaterial in Textausgaben
# ---------------------------------------------------------------------------


def test_repr_does_not_leak_key_material() -> None:
    keybag = parse_keybag(build_keybag("geheim").blob)
    text = repr(keybag)
    assert keybag.salt.hex() not in text
    for class_key in keybag.class_keys.values():
        assert class_key.wrapped_key.hex() not in text
    assert "classes=11" in text
