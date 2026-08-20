"""Registrierung und Auswahl der Messenger-Profile."""

from __future__ import annotations

from msgbackup_extractor.apps.base import AppProfile
from msgbackup_extractor.apps.signal import SignalProfile
from msgbackup_extractor.apps.threema import ThreemaProfile
from msgbackup_extractor.apps.whatsapp import WhatsAppProfile
from msgbackup_extractor.models import BackupInfo, DetectionResult, DetectionStatus

#: Alle bekannten Profile.
_PROFILES: tuple[type[AppProfile], ...] = (
    SignalProfile,
    ThreemaProfile,
    WhatsAppProfile,
)


def available_profiles() -> tuple[AppProfile, ...]:
    """Alle registrierten Profile, alphabetisch nach Slug."""
    return tuple(sorted((cls() for cls in _PROFILES), key=lambda p: p.slug))


def profile_slugs() -> tuple[str, ...]:
    return tuple(profile.slug for profile in available_profiles())


def get_profile(slug: str) -> AppProfile:
    """Profil nach Slug. Wirft `KeyError` mit hilfreicher Meldung."""
    for profile in available_profiles():
        if profile.slug == slug.lower():
            return profile
    raise KeyError(
        f"Unbekannter Messenger {slug!r}. Verfuegbar: {', '.join(profile_slugs())}"
    )


def detect_all(info: BackupInfo) -> tuple[tuple[AppProfile, DetectionResult], ...]:
    """Laesst jedes Profil das Backup pruefen. Reihenfolge: Treffer zuerst."""
    results = [(profile, profile.detect(info)) for profile in available_profiles()]
    order = {
        DetectionStatus.CONFIRMED: 0,
        DetectionStatus.AMBIGUOUS: 1,
        DetectionStatus.NOT_FOUND: 2,
    }
    return tuple(sorted(results, key=lambda item: (order[item[1].status], item[0].slug)))


def detected_profiles(
    info: BackupInfo,
) -> tuple[tuple[AppProfile, DetectionResult], ...]:
    """Nur die Profile, die tatsaechlich etwas gefunden haben."""
    return tuple(
        (profile, result)
        for profile, result in detect_all(info)
        if result.status is not DetectionStatus.NOT_FOUND
    )
