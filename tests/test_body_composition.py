from __future__ import annotations

import pytest

from custom_components.okok_scale.body_composition import (
    calc_baseline_body_fat_pct,
    calc_baseline_body_water_pct,
    calc_body_fat_pct,
    calc_body_water_pct,
    calc_relative_body_fat_pct,
    calc_relative_body_water_pct,
    calc_resistance_ohms,
)
from custom_components.okok_scale.const import (
    BODY_FAT_MAX_PCT,
    BODY_FAT_MIN_PCT,
    BODY_WATER_MAX_PCT,
    BODY_WATER_MIN_PCT,
    FORMULA_DEURENBERG_1991,
    FORMULA_DEURENBERG_1992,
    FORMULA_EDDY_1976,
    FORMULA_GALLAGHER_2000,
)

# male, 61.9 kg, 178 cm, 40 y/o
MALE = dict(weight_kg=61.9, height_cm=178, age_years=40, sex="male")
# female, 78 kg, 165 cm, 38 y/o
FEMALE = dict(weight_kg=78.0, height_cm=165, age_years=38, sex="female")


@pytest.mark.parametrize(
    ("formula", "male_expected", "female_expected"),
    [
        (FORMULA_DEURENBERG_1991, 16.4, 37.7),
        (FORMULA_DEURENBERG_1992, 16.4, 37.7),
        (FORMULA_EDDY_1976, 14.9, 35.4),
        (FORMULA_GALLAGHER_2000, 11.9, 37.9),
    ],
)
def test_body_fat_formulas(formula: str, male_expected: float, female_expected: float) -> None:
    male_bf = calc_body_fat_pct(**MALE, formula=formula)
    female_bf = calc_body_fat_pct(**FEMALE, formula=formula)
    assert male_bf == pytest.approx(male_expected)
    assert female_bf == pytest.approx(female_expected)


def test_body_fat_ignores_impedance_argument() -> None:
    """Documented limitation: impedance is logged, but doesn't affect the estimate."""
    without = calc_body_fat_pct(**MALE, impedance=None)
    with_imp = calc_body_fat_pct(**MALE, impedance=6000)
    assert without == with_imp


def test_body_fat_deurenberg_1992_uses_child_formula_under_16() -> None:
    child = dict(weight_kg=45.0, height_cm=150, age_years=12, sex="male")
    adult_formula_result = calc_body_fat_pct(**child, formula=FORMULA_DEURENBERG_1991)
    child_formula_result = calc_body_fat_pct(**child, formula=FORMULA_DEURENBERG_1992)
    assert adult_formula_result != child_formula_result


def test_body_fat_clamped_to_plausible_range() -> None:
    # Absurdly low BMI/age should clamp at the floor, not go negative.
    low = calc_body_fat_pct(weight_kg=40.0, height_cm=200, age_years=10, sex="male")
    assert low == BODY_FAT_MIN_PCT

    # Absurdly high BMI/age should clamp at the ceiling, not exceed 100%.
    # (Extreme enough to clamp under every formula, not just the default -
    # Gallagher's 1/bmi term makes it converge much more slowly than the
    # other formulas' linear terms, so a merely-high BMI isn't enough.)
    high = calc_body_fat_pct(weight_kg=400.0, height_cm=100, age_years=200, sex="female")
    assert high == BODY_FAT_MAX_PCT


def test_body_fat_guards_divide_by_zero() -> None:
    assert calc_body_fat_pct(weight_kg=70.0, height_cm=0, age_years=40, sex="male") is None


def test_baseline_is_the_average_of_recent_values() -> None:
    # 16.38 average, rounded to 1dp like every other displayed figure here.
    assert calc_baseline_body_fat_pct([16.4, 16.8, 15.9, 16.1, 16.7]) == pytest.approx(16.4)


def test_baseline_works_with_fewer_than_five_values() -> None:
    # "Use whatever's available" - reset_baseline may be pressed before a
    # person has 5 measurements yet.
    assert calc_baseline_body_fat_pct([16.4, 16.8]) == pytest.approx(16.6)
    assert calc_baseline_body_fat_pct([16.4]) == pytest.approx(16.4)


def test_baseline_is_none_with_no_values() -> None:
    assert calc_baseline_body_fat_pct([]) is None


def test_relative_body_fat_pct_at_baseline_is_100() -> None:
    assert calc_relative_body_fat_pct(16.4, 16.4) == pytest.approx(100.0)


def test_relative_body_fat_pct_above_and_below_baseline() -> None:
    # Higher absolute body fat than baseline -> over 100%.
    assert calc_relative_body_fat_pct(18.0, 16.4) == pytest.approx(109.8, abs=0.1)
    # Lower absolute body fat than baseline -> under 100%.
    assert calc_relative_body_fat_pct(15.0, 16.4) == pytest.approx(91.5, abs=0.1)


def test_relative_body_fat_pct_none_without_baseline() -> None:
    """Documented pre-baseline behaviour: unknown, not some placeholder value."""
    assert calc_relative_body_fat_pct(16.4, None) is None
    assert calc_relative_body_fat_pct(16.4, 0) is None


def test_relative_body_fat_pct_none_without_absolute_value() -> None:
    assert calc_relative_body_fat_pct(None, 16.4) is None


def test_resistance_ohms_converts_the_scales_raw_x10_units() -> None:
    # Confirmed against the real captured session (61.90 kg / raw 6000):
    # 600 ohms is within openScale's documented ~500+-100 ohm range for a
    # foot-to-foot scale; 6000 ohms directly is not.
    assert calc_resistance_ohms(6000) == pytest.approx(600.0)


def test_resistance_ohms_none_without_impedance() -> None:
    assert calc_resistance_ohms(None) is None
    assert calc_resistance_ohms(0) is None


def test_body_water_pct_sun_2003() -> None:
    # Sun et al. 2003, gender-specific coefficients - see body_composition.py.
    male = calc_body_water_pct(weight_kg=61.9, height_cm=178, sex="male", impedance=6000)
    female = calc_body_water_pct(weight_kg=78.0, height_cm=165, sex="female", impedance=6000)
    assert male == pytest.approx(58.0)
    assert female == pytest.approx(41.8)


def test_body_water_pct_none_without_impedance() -> None:
    """Unlocked/not-yet-measured frames report impedance 0 - nothing to
    estimate from yet, same treatment as calc_resistance_ohms."""
    assert calc_body_water_pct(weight_kg=61.9, height_cm=178, sex="male", impedance=0) is None
    assert calc_body_water_pct(weight_kg=61.9, height_cm=178, sex="male", impedance=None) is None


def test_body_water_pct_guards_divide_by_zero() -> None:
    assert calc_body_water_pct(weight_kg=61.9, height_cm=0, sex="male", impedance=6000) is None


def test_body_water_pct_clamped_to_plausible_range() -> None:
    # Implausibly high resistance shrinks the height^2/R term toward zero,
    # leaving too little water for the weight alone to explain - clamps at
    # the floor rather than reporting an absurd number.
    low = calc_body_water_pct(weight_kg=61.9, height_cm=178, sex="male", impedance=100000)
    assert low == BODY_WATER_MIN_PCT

    # Implausibly low resistance blows the same term up instead - clamps
    # at the ceiling rather than exceeding the person's own body weight.
    high = calc_body_water_pct(weight_kg=61.9, height_cm=178, sex="male", impedance=100)
    assert high == BODY_WATER_MAX_PCT


def test_water_baseline_is_the_average_of_recent_values() -> None:
    assert calc_baseline_body_water_pct([58.0, 58.4, 57.5, 58.1, 57.9]) == pytest.approx(58.0, abs=0.05)


def test_water_baseline_works_with_fewer_than_five_values() -> None:
    assert calc_baseline_body_water_pct([58.0, 58.4]) == pytest.approx(58.2)
    assert calc_baseline_body_water_pct([58.0]) == pytest.approx(58.0)


def test_water_baseline_is_none_with_no_values() -> None:
    assert calc_baseline_body_water_pct([]) is None


def test_relative_body_water_pct_at_baseline_is_100() -> None:
    assert calc_relative_body_water_pct(58.0, 58.0) == pytest.approx(100.0)


def test_relative_body_water_pct_above_and_below_baseline() -> None:
    # A same-day reading above/below the personal baseline - e.g. more or
    # less hydrated than usual - not necessarily anything alarming.
    assert calc_relative_body_water_pct(59.5, 58.0) == pytest.approx(102.6, abs=0.1)
    assert calc_relative_body_water_pct(56.5, 58.0) == pytest.approx(97.4, abs=0.1)


def test_relative_body_water_pct_none_without_baseline() -> None:
    assert calc_relative_body_water_pct(58.0, None) is None
    assert calc_relative_body_water_pct(58.0, 0) is None


def test_relative_body_water_pct_none_without_absolute_value() -> None:
    assert calc_relative_body_water_pct(None, 58.0) is None
