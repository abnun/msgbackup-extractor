"""Tests fuer die Messenger-Erkennung.

Kernanforderung: nichts raten. Ein Bundle Identifier gilt nur als erkannt, wenn
er in den Backup-Metadaten tatsaechlich vorkommt; Mehrdeutigkeit fuehrt zu
AMBIGUOUS statt zu einer Auswahl.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msgbackup_extractor.apps.base import (
    DatabaseCandidate,
    domain_identifier,
    domain_kind,
)
from msgbackup_extractor.apps.registry import (
    available_profiles,
    detect_all,
    detected_profiles,
    get_profile,
    profile_slugs,
)
from msgbackup_extractor.apps.threema import ThreemaProfile
from msgbackup_extractor.core.backup import AppleBackup
from msgbackup_extractor.models import DetectionStatus, ManifestEntry
from tests.conftest import PNG, THREEMA_BUNDLE_ID
from tests.support.backup_builder import BackupFile, BuiltBackup, build_backup


def _info(tmp_path: Path, bundle_ids: list[str], *, name: str = "b"):
    backup = build_backup(
        tmp_path / name,
        [BackupFile("AppDomain-x", "Documents/a.png", PNG)],
        installed_applications=bundle_ids,
    )
    return AppleBackup(backup.path).info()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_threema_is_registered() -> None:
    assert "threema" in profile_slugs()
    assert isinstance(get_profile("threema"), ThreemaProfile)


def test_get_profile_is_case_insensitive() -> None:
    assert get_profile("THREEMA").slug == "threema"


def test_unknown_profile_lists_alternatives() -> None:
    with pytest.raises(KeyError, match="threema"):
        get_profile("telegram")


def test_profiles_are_sorted_by_slug() -> None:
    slugs = [profile.slug for profile in available_profiles()]
    assert slugs == sorted(slugs)


# ---------------------------------------------------------------------------
# Domain-Hilfen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "kind"),
    [
        ("AppDomain-ch.threema.iapp", "app"),
        ("AppDomainGroup-group.ch.threema", "group"),
        ("AppDomainPlugin-ch.threema.iapp.ShareExtension", "plugin"),
        ("HomeDomain", "unknown"),
        ("CameraRollDomain", "unknown"),
    ],
)
def test_domain_kind(domain: str, kind: str) -> None:
    assert domain_kind(domain) == kind


def test_domain_identifier_strips_prefix() -> None:
    assert domain_identifier("AppDomain-ch.threema.iapp") == "ch.threema.iapp"
    assert domain_identifier("AppDomainGroup-group.ch.threema") == "group.ch.threema"
    assert domain_identifier("HomeDomain") == "HomeDomain"


# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------


def test_confirmed_detection(plain_backup: BuiltBackup) -> None:
    result = ThreemaProfile().detect(AppleBackup(plain_backup.path).info())
    assert result.status is DetectionStatus.CONFIRMED
    assert result.bundle_id == THREEMA_BUNDLE_ID
    assert result.bundle_version == "6.1.2"
    assert result.is_confirmed


def test_not_found_when_app_absent(backup_without_threema: BuiltBackup) -> None:
    result = ThreemaProfile().detect(AppleBackup(backup_without_threema.path).info())
    assert result.status is DetectionStatus.NOT_FOUND
    assert result.bundle_id is None
    assert result.reason is not None and "ch.threema." in result.reason


def test_variants_are_found_via_namespace_not_hardcoded_ids(tmp_path: Path) -> None:
    """Eine unbekannte Variante wird ueber den Namensraum gefunden."""
    info = _info(tmp_path, ["ch.threema.eine-ganz-neue-variante"])
    result = ThreemaProfile().detect(info)
    assert result.status is DetectionStatus.CONFIRMED
    assert result.bundle_id == "ch.threema.eine-ganz-neue-variante"


def test_multiple_variants_are_ambiguous_not_guessed(tmp_path: Path) -> None:
    info = _info(tmp_path, ["ch.threema.iapp", "ch.threema.work.iapp"])
    result = ThreemaProfile().detect(info)
    assert result.status is DetectionStatus.AMBIGUOUS
    assert result.bundle_id is None
    assert set(result.candidates) == {"ch.threema.iapp", "ch.threema.work.iapp"}
    assert "--bundle-id" in (result.reason or "")


def test_similar_but_foreign_bundle_id_is_not_matched(tmp_path: Path) -> None:
    """`ch.threemafake.app` liegt nicht im Namensraum `ch.threema.`."""
    info = _info(tmp_path, ["ch.threemafake.app", "com.example.threema"])
    assert ThreemaProfile().detect(info).status is DetectionStatus.NOT_FOUND


def test_detect_all_puts_hits_first(plain_backup: BuiltBackup) -> None:
    results = detect_all(AppleBackup(plain_backup.path).info())
    assert results[0][1].status is DetectionStatus.CONFIRMED


def test_detected_profiles_filters_misses(backup_without_threema: BuiltBackup) -> None:
    assert detected_profiles(AppleBackup(backup_without_threema.path).info()) == ()


# ---------------------------------------------------------------------------
# Domain-Zuordnung
# ---------------------------------------------------------------------------


def test_matches_app_group_and_plugin_domains() -> None:
    domains = (
        "AppDomain-ch.threema.iapp",
        "AppDomainGroup-group.ch.threema.iapp",
        "AppDomainPlugin-ch.threema.iapp.ShareExtension",
        "AppDomain-com.apple.Maps",
        "HomeDomain",
        "CameraRollDomain",
    )
    matched = ThreemaProfile().match_domains(THREEMA_BUNDLE_ID, domains)
    assert {m.domain for m in matched} == {
        "AppDomain-ch.threema.iapp",
        "AppDomainGroup-group.ch.threema.iapp",
        "AppDomainPlugin-ch.threema.iapp.ShareExtension",
    }
    assert {m.kind for m in matched} == {"app", "group", "plugin"}


def test_foreign_domains_are_never_matched() -> None:
    domains = ("AppDomain-com.apple.Maps", "HomeDomain", "AppDomain-ch.threemafake.app")
    assert ThreemaProfile().match_domains(THREEMA_BUNDLE_ID, domains) == ()


def test_no_domains_yields_empty_tuple() -> None:
    assert ThreemaProfile().match_domains(THREEMA_BUNDLE_ID, ()) == ()


# ---------------------------------------------------------------------------
# Datenbankklassifikation
# ---------------------------------------------------------------------------


def _candidate(tables: tuple[str, ...]) -> DatabaseCandidate:
    entry = ManifestEntry(
        file_id="a" * 40,
        domain="AppDomain-ch.threema.iapp",
        relative_path="Documents/ThreemaData.sqlite",
        kind=None,
    )
    return DatabaseCandidate(entry=entry, tables=tables)


def test_core_data_store_with_entities_is_classified_confidently() -> None:
    roles = ThreemaProfile().classify_databases(
        (_candidate(("Z_METADATA", "Z_PRIMARYKEY", "ZCONVERSATION", "ZMESSAGE", "ZCONTACT")),)
    )
    assert roles[0].role == "messages"
    assert roles[0].confidence == "high"
    assert "Z_METADATA" in roles[0].reason


def test_core_data_store_without_recognisable_entities_is_unknown() -> None:
    roles = ThreemaProfile().classify_databases(
        (_candidate(("Z_METADATA", "Z_PRIMARYKEY", "ZFOO", "ZBAR")),)
    )
    assert roles[0].role == "unknown"
    assert "keine der Entitaeten" in roles[0].reason


def test_non_core_data_with_hints_is_medium_confidence() -> None:
    roles = ThreemaProfile().classify_databases(
        (_candidate(("messages", "conversations")),)
    )
    assert roles[0].role in {"messages", "conversations"}
    assert roles[0].confidence == "medium"


def test_unrecognisable_database_is_unknown_with_reason() -> None:
    roles = ThreemaProfile().classify_databases((_candidate(("foo", "bar")),))
    assert roles[0].role == "unknown"
    assert "foo" in roles[0].reason


def test_database_without_schema_is_unknown() -> None:
    roles = ThreemaProfile().classify_databases((_candidate(()),))
    assert roles[0].role == "unknown"
    assert "nicht eingelesen" in roles[0].reason


def test_link_media_returns_nothing_until_implemented() -> None:
    """Bis das echte Schema bekannt ist, wird nichts zugeordnet."""
    assert ThreemaProfile().link_media(None, ()) == ()  # type: ignore[arg-type]
