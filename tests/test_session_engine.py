from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.okok_scale.assignment import is_registration_armed, match_person
from custom_components.okok_scale.const import DEFAULT_MATCH_TOLERANCE_KG
from custom_components.okok_scale.csv_logger import (
    append_row,
    read_last_weight_kg,
    reassign_session,
)
from custom_components.okok_scale.models import Person


def make_person(id: str, name: str, ref_weight_kg: float | None, sex: str = "male") -> Person:
    return Person(id=id, name=name, sex=sex, age_years=40, height_cm=178, ref_weight_kg=ref_weight_kg)


class TestRegistrationArming:
    def test_is_registration_armed_within_window(self) -> None:
        assert is_registration_armed(armed_at=1000.0, now=1050.0, window_seconds=120) is True

    def test_is_registration_armed_expired(self) -> None:
        assert is_registration_armed(armed_at=1000.0, now=1200.0, window_seconds=120) is False

    def test_is_registration_armed_when_nothing_pending(self) -> None:
        assert is_registration_armed(armed_at=None, now=1000.0) is False

    def test_pending_person_wins_even_if_weight_is_far_off(self) -> None:
        """Armed registration bypasses nearest-neighbour matching entirely."""
        people = [make_person("me", "Me", ref_weight_kg=61.5)]
        # 78 kg is nowhere near "me"'s ref, but a registration is armed for
        # a brand new person -> must still go to the pending person.
        result = match_person(78.0, people, pending_person_id="wife")
        assert result == "wife"


class TestNearestNeighbourMatching:
    def test_routes_close_weight_to_known_ref(self) -> None:
        people = [make_person("me", "Me", ref_weight_kg=61.5), make_person("wife", "Wife", ref_weight_kg=None)]
        assert match_person(61.9, people) == "me"

    def test_routes_far_weight_to_unseeded_person(self) -> None:
        people = [make_person("me", "Me", ref_weight_kg=61.5), make_person("wife", "Wife", ref_weight_kg=None)]
        assert match_person(78.0, people) == "wife"

    def test_within_tolerance_still_goes_to_known_person(self) -> None:
        people = [make_person("me", "Me", ref_weight_kg=61.5), make_person("wife", "Wife", ref_weight_kg=None)]
        # exactly at the tolerance boundary -> still counts as "close enough"
        weight = 61.5 + DEFAULT_MATCH_TOLERANCE_KG
        assert match_person(weight, people) == "me"

    def test_two_known_people_pick_nearest(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=61.5),
            make_person("wife", "Wife", ref_weight_kg=70.0),
        ]
        assert match_person(69.0, people) == "wife"
        assert match_person(63.0, people) == "me"

    def test_nobody_has_a_ref_yet_uses_first_unseeded(self) -> None:
        people = [make_person("me", "Me", ref_weight_kg=None), make_person("wife", "Wife", ref_weight_kg=None)]
        assert match_person(61.9, people) == "me"

    def test_no_people_registered_returns_none(self) -> None:
        assert match_person(61.9, []) is None

    def test_far_weight_with_no_unseeded_person_falls_back_to_nearest(self) -> None:
        """No bootstrap target available -> best-effort nearest match, not None."""
        people = [make_person("me", "Me", ref_weight_kg=61.5)]
        assert match_person(78.0, people) == "me"


class TestCsvReassignment:
    def test_reassign_moves_rows_and_refs_update(self, tmp_path: Path) -> None:
        me_csv = tmp_path / "me.csv"
        wife_csv = tmp_path / "wife.csv"

        # "me" mistakenly received the wife's 78 kg session, on top of an
        # earlier, correctly-assigned session of their own.
        append_row(
            me_csv,
            {
                "time": "2026-07-19T08:00:00",
                "session_id": "sess-1",
                "weight_kg": 61.9,
                "impedance": 6000,
                "bmi": 19.5,
                "body_fat_pct": 16.4,
                "lean_mass_kg": 51.7,
                "body_water_pct": 63.1,
            },
        )
        append_row(
            me_csv,
            {
                "time": "2026-07-19T09:00:00",
                "session_id": "sess-2",
                "weight_kg": 78.0,
                "impedance": 5200,
                "bmi": 24.6,
                "body_fat_pct": 20.0,
                "lean_mass_kg": 62.4,
                "body_water_pct": 55.0,
            },
        )

        moved = reassign_session(
            me_csv,
            wife_csv,
            "sess-2",
            target_height_cm=165,
            target_age_years=38,
            target_sex="female",
        )

        assert len(moved) == 1
        assert moved[0]["weight_kg"] == 78.0
        # Body composition must be recomputed for the *wife*, not "me".
        assert moved[0]["body_fat_pct"] == pytest.approx(37.9, abs=0.2)

        # "me"'s CSV now ends with their own earlier, correct session.
        assert read_last_weight_kg(me_csv) == pytest.approx(61.9)
        # "wife"'s CSV now has the moved session as her only/last row.
        assert read_last_weight_kg(wife_csv) == pytest.approx(78.0)

    def test_reassign_nonexistent_session_is_a_noop(self, tmp_path: Path) -> None:
        me_csv = tmp_path / "me.csv"
        wife_csv = tmp_path / "wife.csv"
        append_row(
            me_csv,
            {
                "time": "2026-07-19T08:00:00",
                "session_id": "sess-1",
                "weight_kg": 61.9,
                "impedance": 6000,
                "bmi": 19.5,
                "body_fat_pct": 16.4,
                "lean_mass_kg": 51.7,
                "body_water_pct": 63.1,
            },
        )
        moved = reassign_session(
            me_csv, wife_csv, "no-such-session", target_height_cm=165, target_age_years=38, target_sex="female"
        )
        assert moved == []
        assert read_last_weight_kg(me_csv) == pytest.approx(61.9)
        assert read_last_weight_kg(wife_csv) is None
