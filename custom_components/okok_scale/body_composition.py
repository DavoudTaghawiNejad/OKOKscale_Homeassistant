"""Pure body-composition estimation formulas.

No Home Assistant imports: unit-tested in isolation
(see tests/test_body_composition.py).

Honesty note (also surfaced in the README): none of the body-fat formulas
below actually consume the scale's bio-impedance reading. They are the
BMI/age/sex estimation formulas published on the openScale wiki's "Body
metric estimations" page, which is what openScale itself uses for scales
like this one that don't document a calibrated impedance regression. A
genuine bio-impedance (BIA) body-fat model needs the raw resistance in ohms
plus a validated, device-specific regression (e.g. Kyle 2001, Sun 2003) and
per-scale calibration constants that this hardware does not publish.
Because of that:
  * the primary body_fat_pct / lean_mass_kg / bmi outputs below are BMI
    based, not impedance based;
  * the raw impedance is still recorded (by the CSV logger / sensor) so a
    better BIA model can be dropped in later without any data loss.

Total body water uses the Hume (1966) formula, which *is* an independently
well-established weight/height regression (not impedance based either).
"""

from __future__ import annotations

from .const import (
    BODY_FAT_MAX_PCT,
    BODY_FAT_MIN_PCT,
    DEFAULT_BODY_FAT_FORMULA,
    FORMULA_DEURENBERG_1991,
    FORMULA_DEURENBERG_1992,
    FORMULA_EDDY_1976,
    FORMULA_GALLAGHER_2000,
)
from .models import Sex


def _is_male(sex: Sex) -> int:
    return 1 if sex == "male" else 0


def _raw_bmi(weight_kg: float, height_cm: float) -> float | None:
    """Unrounded BMI, or None if height is non-physical (avoids div/0)."""
    if height_cm is None or height_cm <= 0 or weight_kg is None or weight_kg <= 0:
        return None
    height_m = height_cm / 100
    return weight_kg / (height_m**2)


def calc_bmi(weight_kg: float, height_cm: float) -> float | None:
    """BMI, rounded to 1 decimal place for display."""
    bmi = _raw_bmi(weight_kg, height_cm)
    return None if bmi is None else round(bmi, 1)


def _clamp_body_fat(pct: float) -> float:
    return max(BODY_FAT_MIN_PCT, min(BODY_FAT_MAX_PCT, pct))


def _deurenberg_1991(bmi: float, age_years: float, sex: Sex) -> float:
    is_male = _is_male(sex)
    return 1.2 * bmi + 0.23 * age_years - 10.8 * is_male - 5.4


def _deurenberg_1992(bmi: float, age_years: float, sex: Sex) -> float:
    is_male = _is_male(sex)
    if age_years >= 16:
        return 1.2 * bmi + 0.23 * age_years - 10.8 * is_male - 5.4
    return 1.294 * bmi + 0.20 * age_years - 11.4 * is_male - 8.0


def _eddy_1976(bmi: float, sex: Sex) -> float:
    if sex == "male":
        return 1.281 * bmi - 10.13
    return 1.48 * bmi - 7.0


def _gallagher_2000(bmi: float, age_years: float, sex: Sex) -> float:
    inv_bmi = 1 / bmi
    if sex == "male":
        return 64.5 - 848.0 * inv_bmi + 0.079 * age_years - 16.4 + 0.05 * age_years + 39.0 * inv_bmi
    return 64.5 - 848.0 * inv_bmi + 0.079 * age_years


_FORMULA_DISPATCH = {
    FORMULA_DEURENBERG_1991: lambda bmi, age, sex: _deurenberg_1991(bmi, age, sex),
    FORMULA_DEURENBERG_1992: lambda bmi, age, sex: _deurenberg_1992(bmi, age, sex),
    FORMULA_EDDY_1976: lambda bmi, age, sex: _eddy_1976(bmi, sex),
    FORMULA_GALLAGHER_2000: lambda bmi, age, sex: _gallagher_2000(bmi, age, sex),
}


def calc_body_fat_pct(
    weight_kg: float,
    height_cm: float,
    age_years: float,
    sex: Sex,
    impedance: int | None = None,  # noqa: ARG001 - intentionally unused, see module docstring
    formula: str = DEFAULT_BODY_FAT_FORMULA,
) -> float | None:
    """Estimated body-fat percentage, clamped to a plausible range."""
    bmi = _raw_bmi(weight_kg, height_cm)
    if bmi is None:
        return None
    fn = _FORMULA_DISPATCH.get(formula, _FORMULA_DISPATCH[DEFAULT_BODY_FAT_FORMULA])
    pct = fn(bmi, age_years, sex)
    return round(_clamp_body_fat(pct), 1)


def calc_fat_mass_kg(weight_kg: float, body_fat_pct: float | None) -> float | None:
    if body_fat_pct is None or weight_kg is None:
        return None
    return round(weight_kg * body_fat_pct / 100, 1)


def calc_lean_mass_kg(weight_kg: float, body_fat_pct: float | None) -> float | None:
    """Lean body mass = weight - fat mass (openScale convention)."""
    fat_mass = calc_fat_mass_kg(weight_kg, body_fat_pct)
    if fat_mass is None:
        return None
    return round(weight_kg - fat_mass, 1)


def calc_body_water_pct(weight_kg: float, height_cm: float, sex: Sex) -> float | None:
    """Total body water percentage via the Hume (1966) formula."""
    if weight_kg is None or weight_kg <= 0 or height_cm is None or height_cm <= 0:
        return None
    if sex == "male":
        tbw_l = 0.194786 * height_cm + 0.296785 * weight_kg - 14.012934
    else:
        tbw_l = 0.344547 * height_cm + 0.183809 * weight_kg - 35.270121
    tbw_l = max(tbw_l, 0.0)
    return round(tbw_l / weight_kg * 100, 1)


def compute_body_composition(
    weight_kg: float,
    height_cm: float,
    age_years: float,
    sex: Sex,
    impedance: int | None = None,
    formula: str = DEFAULT_BODY_FAT_FORMULA,
) -> dict[str, float | None]:
    """Convenience bundle of every derived metric for one weight reading."""
    bmi = calc_bmi(weight_kg, height_cm)
    body_fat_pct = calc_body_fat_pct(weight_kg, height_cm, age_years, sex, impedance, formula)
    lean_mass_kg = calc_lean_mass_kg(weight_kg, body_fat_pct)
    body_water_pct = calc_body_water_pct(weight_kg, height_cm, sex)
    return {
        "bmi": bmi,
        "body_fat_pct": body_fat_pct,
        "lean_mass_kg": lean_mass_kg,
        "body_water_pct": body_water_pct,
    }
