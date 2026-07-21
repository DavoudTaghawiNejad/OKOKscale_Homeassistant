from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.okok_scale.assignment import is_registration_armed, match_person
from custom_components.okok_scale.csv_logger import (
    append_row,
    delete_csv,
    read_last_weight_kg,
    read_rows,
    reassign_session,
)
from custom_components.okok_scale.models import Person


def make_person(
    id: str,
    name: str,
    ref_weight_kg: float | None,
    ref_impedance: int | None = None,
    sex: str = "male",
) -> Person:
    return Person(
        id=id,
        name=name,
        sex=sex,
        age_years=40,
        height_cm=178,
        ref_weight_kg=ref_weight_kg,
        ref_impedance=ref_impedance,
    )


class TestRegistrationArming:
    def test_is_registration_armed_within_window(self) -> None:
        assert is_registration_armed(armed_at=1000.0, now=1050.0, window_seconds=120) is True

    def test_is_registration_armed_expired(self) -> None:
        assert is_registration_armed(armed_at=1000.0, now=1200.0, window_seconds=120) is False

    def test_is_registration_armed_when_nothing_pending(self) -> None:
        assert is_registration_armed(armed_at=None, now=1000.0) is False

    def test_pending_person_wins_even_if_measurement_is_far_off(self) -> None:
        """Armed registration bypasses midpoint-interval matching entirely."""
        people = [make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=500)]
        # 78 kg / 900 ohm is nowhere near "me"'s ref, but a registration is
        # armed for a brand new person -> must still go to the pending person.
        result = match_person(78.0, 900, people, pending_person_id="wife")
        assert result == "wife"


class TestMidpointIntervalMatching:
    def test_single_seeded_person_always_matches_regardless_of_value(self) -> None:
        people = [make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=500)]
        assert match_person(120.0, 50, people) == "me"

    def test_weight_and_impedance_agree_on_lower_person(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=500),
            make_person("wife", "Wife", ref_weight_kg=70.0, ref_impedance=600),
        ]
        # weight midpoint 65.75, impedance midpoint 550 - both sides agree "me".
        assert match_person(63.0, 520, people) == "me"

    def test_weight_and_impedance_agree_on_higher_person(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=500),
            make_person("wife", "Wife", ref_weight_kg=70.0, ref_impedance=600),
        ]
        assert match_person(69.0, 580, people) == "wife"

    def test_exact_midpoint_tie_goes_to_the_lower_person(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=60.0, ref_impedance=600),
            make_person("wife", "Wife", ref_weight_kg=70.0, ref_impedance=700),
        ]
        # weight midpoint 65.0, impedance midpoint 650 - landing exactly on both.
        assert match_person(65.0, 650, people) == "me"

    def test_three_seeded_people_middle_interval(self) -> None:
        people = [
            make_person("low", "Low", ref_weight_kg=50.0, ref_impedance=400),
            make_person("mid", "Mid", ref_weight_kg=65.0, ref_impedance=550),
            make_person("high", "High", ref_weight_kg=90.0, ref_impedance=800),
        ]
        # weight midpoints: 57.5, 77.5; impedance midpoints: 475, 675.
        assert match_person(66.0, 560, people) == "mid"
        assert match_person(48.0, 390, people) == "low"
        assert match_person(95.0, 900, people) == "high"

    def test_disagreement_falls_back_to_weight_times_impedance_between_the_two_candidates(self) -> None:
        # Impedance is inverted relative to weight (wife is heavier but has
        # lower impedance), so weight and impedance pick different people
        # for a measurement in between.
        people = [
            make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=600),
            make_person("wife", "Wife", ref_weight_kg=70.0, ref_impedance=500),
        ]
        # weight midpoint 65.75 -> 63.0 kg is in "me"'s (lower) interval.
        # impedance midpoint 550 -> 520 ohm is in "wife"'s (lower) interval,
        # since wife has the *lower* reference impedance. Disagreement.
        # products: me=61.5*600=36900, wife=70.0*500=35000, midpoint=35950.
        # measurement product = 63.0*520=32760 <= 35950 -> the lower one, wife.
        assert match_person(63.0, 520, people) == "wife"

    def test_unseeded_person_never_selected_while_any_seeded_person_exists(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=500),
            make_person("wife", "Wife", ref_weight_kg=None, ref_impedance=None),
        ]
        # Wildly far from "me", but there's no bootstrap-to-unseeded rule
        # anymore - "me" is the only seeded person, so their interval covers
        # everything.
        assert match_person(120.0, 50, people) == "me"

    def test_nobody_seeded_yet_uses_first_unseeded(self) -> None:
        people = [
            make_person("me", "Me", ref_weight_kg=None, ref_impedance=None),
            make_person("wife", "Wife", ref_weight_kg=None, ref_impedance=None),
        ]
        assert match_person(61.9, 6000, people) == "me"

    def test_partially_seeded_person_counts_as_unseeded(self) -> None:
        # Has a weight but no impedance yet (e.g. an unlocked final frame) -
        # can't build an interval from just one axis.
        people = [make_person("me", "Me", ref_weight_kg=61.5, ref_impedance=None)]
        assert match_person(61.9, 6000, people) == "me"

    def test_no_people_registered_returns_none(self) -> None:
        assert match_person(61.9, 6000, []) is None


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
                "body_fat_pct": 16.4,
                "body_fat_relative_pct": 100.0,
            },
        )
        append_row(
            me_csv,
            {
                "time": "2026-07-19T09:00:00",
                "session_id": "sess-2",
                "weight_kg": 78.0,
                "impedance": 5200,
                "body_fat_pct": 20.0,
                "body_fat_relative_pct": 122.0,
            },
        )

        moved = reassign_session(
            me_csv,
            wife_csv,
            "sess-2",
            target_height_cm=165,
            target_age_years=38,
            target_sex="female",
            target_baseline_body_fat_pct=30.0,
            target_baseline_body_water_pct=50.0,
        )

        assert len(moved) == 1
        assert moved[0]["weight_kg"] == 78.0
        # Body composition must be recomputed for the *wife*, not "me".
        assert moved[0]["body_fat_pct"] == pytest.approx(37.9, abs=0.2)
        # ... and body_fat_relative_pct against *her* baseline, not
        # whatever it happened to be under "me" (122.0 above).
        assert moved[0]["body_fat_relative_pct"] == pytest.approx(126.3, abs=0.5)
        # body_water_pct is likewise recomputed against the wife's own
        # height/sex (Sun 2003 needs both), while resistance_ohms - which
        # doesn't depend on the person - is just carried over unchanged.
        assert moved[0]["body_water_pct"] == pytest.approx(45.8)
        assert moved[0]["resistance_ohms"] == ""
        # ... and body_water_relative_pct against her own (separately
        # tracked) water baseline, 45.8 / 50.0 * 100.
        assert moved[0]["body_water_relative_pct"] == pytest.approx(91.6)

        # "me"'s CSV now ends with their own earlier, correct session.
        assert read_last_weight_kg(me_csv) == pytest.approx(61.9)
        # "wife"'s CSV now has the moved session as her only/last row.
        assert read_last_weight_kg(wife_csv) == pytest.approx(78.0)

    def test_reassign_without_a_target_baseline_yet(self, tmp_path: Path) -> None:
        """A brand new target person has no baseline yet - the moved row's
        relative pct must be None (unknown), not a division-by-nothing
        crash or a stale/wrong number."""
        me_csv = tmp_path / "me.csv"
        wife_csv = tmp_path / "wife.csv"
        append_row(
            me_csv,
            {
                "time": "2026-07-19T08:00:00",
                "session_id": "sess-1",
                "weight_kg": 78.0,
                "impedance": 5200,
                "body_fat_pct": 20.0,
                "body_fat_relative_pct": 100.0,
            },
        )
        moved = reassign_session(
            me_csv, wife_csv, "sess-1", target_height_cm=165, target_age_years=38, target_sex="female"
        )
        assert moved[0]["body_fat_relative_pct"] is None
        assert moved[0]["body_water_relative_pct"] is None

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
                "body_fat_pct": 16.4,
                "body_fat_relative_pct": 100.0,
            },
        )
        moved = reassign_session(
            me_csv, wife_csv, "no-such-session", target_height_cm=165, target_age_years=38, target_sex="female"
        )
        assert moved == []
        assert read_last_weight_kg(me_csv) == pytest.approx(61.9)
        assert read_last_weight_kg(wife_csv) is None


class TestCsvSchemaMigration:
    """resistance_ohms/body_water_pct were added to CSV_FIELDNAMES after
    people could already have real weighing history on disk under the
    older 6-column header. append_row must migrate such a file's header in
    place rather than silently misaligning every column a later
    csv.DictReader maps by name (see csv_logger._migrate_schema_if_needed).
    """

    def test_appending_to_an_old_schema_file_migrates_its_header(self, tmp_path: Path) -> None:
        me_csv = tmp_path / "me.csv"
        # A file exactly as an earlier version of this integration would
        # have written it - the pre-resistance_ohms/body_water_pct header.
        me_csv.write_text(
            "time,session_id,weight_kg,impedance,body_fat_pct,body_fat_relative_pct\n"
            "2026-07-19T08:00:00,sess-1,61.9,6000,16.4,100.0\n"
        )

        append_row(
            me_csv,
            {
                "time": "2026-07-20T08:00:00",
                "session_id": "sess-2",
                "weight_kg": 62.5,
                "impedance": 5900,
                "body_fat_pct": 16.1,
                "body_fat_relative_pct": 98.2,
                "resistance_ohms": 590.0,
                "body_water_pct": 58.4,
            },
        )

        rows = read_rows(me_csv)
        assert len(rows) == 2
        # The old row's data survives untouched, with the new columns
        # backfilled blank rather than misaligned into some other field.
        assert rows[0]["session_id"] == "sess-1"
        assert rows[0]["weight_kg"] == "61.9"
        assert rows[0]["body_fat_pct"] == "16.4"
        assert rows[0]["resistance_ohms"] == ""
        assert rows[0]["body_water_pct"] == ""
        # The new row is written under the migrated (correct) header.
        assert rows[1]["session_id"] == "sess-2"
        assert rows[1]["resistance_ohms"] == "590.0"
        assert rows[1]["body_water_pct"] == "58.4"

    def test_appending_to_an_already_current_schema_file_is_a_noop_migration(self, tmp_path: Path) -> None:
        """A file already on the current schema shouldn't be rewritten -
        just confirms the fast path (header comparison) doesn't itself
        corrupt anything."""
        me_csv = tmp_path / "me.csv"
        append_row(
            me_csv,
            {
                "time": "2026-07-19T08:00:00",
                "session_id": "sess-1",
                "weight_kg": 61.9,
                "impedance": 6000,
                "body_fat_pct": 16.4,
                "body_fat_relative_pct": 100.0,
                "resistance_ohms": 600.0,
                "body_water_pct": 58.0,
            },
        )
        append_row(
            me_csv,
            {
                "time": "2026-07-20T08:00:00",
                "session_id": "sess-2",
                "weight_kg": 62.0,
                "impedance": 5950,
                "body_fat_pct": 16.2,
                "body_fat_relative_pct": 98.8,
                "resistance_ohms": 595.0,
                "body_water_pct": 58.2,
            },
        )
        rows = read_rows(me_csv)
        assert len(rows) == 2
        assert rows[0]["resistance_ohms"] == "600.0"
        assert rows[1]["resistance_ohms"] == "595.0"


class TestDeleteCsv:
    """delete_csv backs the "clear history" button - see
    coordinator.async_clear_history."""

    def test_deletes_an_existing_file(self, tmp_path: Path) -> None:
        me_csv = tmp_path / "me.csv"
        append_row(me_csv, {"time": "2026-07-19T08:00:00", "session_id": "sess-1", "weight_kg": 61.9})
        assert me_csv.exists()

        assert delete_csv(me_csv) is True
        assert not me_csv.exists()

    def test_missing_file_is_a_noop(self, tmp_path: Path) -> None:
        me_csv = tmp_path / "me.csv"
        assert delete_csv(me_csv) is False
        assert not me_csv.exists()
