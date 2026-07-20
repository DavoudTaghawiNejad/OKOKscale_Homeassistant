from __future__ import annotations

import pytest

from custom_components.okok_scale.body_composition import (
    calc_bmi,
    calc_body_fat_pct,
    calc_body_water_pct,
    calc_fat_mass_kg,
    calc_lean_mass_kg,
    compute_body_composition,
)
from custom_components.okok_scale.const import (
    BODY_FAT_MAX_PCT,
    BODY_FAT_MIN_PCT,
    FORMULA_DEURENBERG_1991,
    FORMULA_DEURENBERG_1992,
    FORMULA_EDDY_1976,
    FORMULA_GALLAGHER_2000,
)

# male, 61.9 kg, 178 cm, 40 y/o
MALE = dict(weight_kg=61.9, height_cm=178, age_years=40, sex="male")
# female, 78 kg, 165 cm, 38 y/o
FEMALE = dict(weight_kg=78.0, height_cm=165, age_years=38, sex="female")


def test_bmi_male() -> None:
    assert calc_bmi(**{k: v for k, v in MALE.items() if k in ("weight_kg", "height_cm")}) == pytest.approx(19.5)


def test_bmi_female() -> None:
    assert calc_bmi(**{k: v for k, v in FEMALE.items() if k in ("weight_kg", "height_cm")}) == pytest.approx(28.7)


def test_bmi_guards_divide_by_zero() -> None:
    assert calc_bmi(70.0, 0) is None
    assert calc_bmi(0, 170) is None


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

    # Absurdly high BMI should clamp at the ceiling, not exceed 100%.
    high = calc_body_fat_pct(weight_kg=250.0, height_cm=150, age_years=90, sex="female")
    assert high == BODY_FAT_MAX_PCT


def test_body_fat_guards_divide_by_zero() -> None:
    assert calc_body_fat_pct(weight_kg=70.0, height_cm=0, age_years=40, sex="male") is None


def test_fat_mass_and_lean_mass_male() -> None:
    bf = calc_body_fat_pct(**MALE)  # 16.4
    fat_mass = calc_fat_mass_kg(MALE["weight_kg"], bf)
    lean_mass = calc_lean_mass_kg(MALE["weight_kg"], bf)
    assert fat_mass == pytest.approx(10.2)
    assert lean_mass == pytest.approx(51.7)
    assert fat_mass + lean_mass == pytest.approx(MALE["weight_kg"], abs=0.15)


def test_fat_mass_none_when_body_fat_unknown() -> None:
    assert calc_fat_mass_kg(70.0, None) is None
    assert calc_lean_mass_kg(70.0, None) is None


def test_body_water_pct_male() -> None:
    pct = calc_body_water_pct(MALE["weight_kg"], MALE["height_cm"], MALE["sex"])
    assert pct == pytest.approx(63.1)


def test_body_water_pct_female() -> None:
    pct = calc_body_water_pct(FEMALE["weight_kg"], FEMALE["height_cm"], FEMALE["sex"])
    assert pct == pytest.approx(46.0)


def test_body_water_pct_guards_divide_by_zero() -> None:
    assert calc_body_water_pct(0, 170, "male") is None
    assert calc_body_water_pct(70.0, 0, "male") is None


def test_compute_body_composition_bundle() -> None:
    result = compute_body_composition(**MALE, impedance=6000)
    assert result == {
        "bmi": 19.5,
        "body_fat_pct": 16.4,
        "lean_mass_kg": 51.7,
        "body_water_pct": 63.1,
    }
