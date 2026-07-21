"""Round-trip tests for person_store's dict (de)serialization.

Regression coverage for a real bug: baseline_body_water_pct/
recent_body_water_history were added to models.Person but never wired
into _person_to_dict/_person_from_dict, so they silently reset to their
dataclass defaults on every Home Assistant restart or config-entry
reload, even though the equivalent body-fat fields persisted fine. These
two functions are plain dict transforms (no Store/HomeAssistant needed),
so they're tested directly here rather than through PersonStore itself.
"""

from __future__ import annotations

from custom_components.okok_scale.models import Person
from custom_components.okok_scale.person_store import _person_from_dict, _person_to_dict


def _full_person() -> Person:
    return Person(
        id="me",
        name="Me",
        sex="male",
        age_years=40,
        height_cm=178,
        activity_level="normal",
        created="2026-07-19T08:00:00",
        ref_weight_kg=61.9,
        ref_impedance=6000,
        baseline_body_fat_pct=16.4,
        recent_body_fat_history=[16.0, 16.2, 16.4, 16.6, 16.8],
        baseline_body_water_pct=58.0,
        recent_body_water_history=[57.5, 58.0, 58.5, 58.0, 58.0],
    )


def test_round_trip_preserves_every_field() -> None:
    original = _full_person()
    restored = _person_from_dict(_person_to_dict(original))
    assert restored == original


def test_round_trip_preserves_body_water_baseline_specifically() -> None:
    """The exact bug this file guards against: water baseline/history
    surviving a save-then-load cycle, same as body fat's already did."""
    original = _full_person()
    restored = _person_from_dict(_person_to_dict(original))
    assert restored.baseline_body_water_pct == 58.0
    assert restored.recent_body_water_history == [57.5, 58.0, 58.5, 58.0, 58.0]


def test_round_trip_with_no_history_yet() -> None:
    """A freshly-registered person has no baseline/history for either
    metric - must round-trip as None/[] , not crash or default oddly."""
    fresh = Person(id="me", name="Me", sex="male", age_years=40, height_cm=178)
    restored = _person_from_dict(_person_to_dict(fresh))
    assert restored.baseline_body_fat_pct is None
    assert restored.recent_body_fat_history == []
    assert restored.baseline_body_water_pct is None
    assert restored.recent_body_water_history == []


def test_from_dict_defaults_missing_water_fields_for_pre_upgrade_data() -> None:
    """A person saved to .storage before this bug was fixed has no
    baseline_body_water_pct/recent_body_water_history keys in their saved
    dict at all - loading that old data must default gracefully rather
    than KeyError."""
    legacy_dict = {
        "id": "me",
        "name": "Me",
        "sex": "male",
        "age_years": 40,
        "height_cm": 178,
        "baseline_body_fat_pct": 16.4,
        "recent_body_fat_history": [16.4],
    }
    restored = _person_from_dict(legacy_dict)
    assert restored.baseline_body_water_pct is None
    assert restored.recent_body_water_history == []
