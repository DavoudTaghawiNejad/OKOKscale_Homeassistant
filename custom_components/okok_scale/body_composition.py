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

Because none of that calibration exists, the integration doesn't expose
this absolute body-fat estimate directly - only *relative to a personal
baseline* (see calc_relative_body_fat_pct / calc_baseline_body_fat_pct),
where a lot of the calibration error cancels out since it's the same bias
applied consistently to every reading for that person.
"""

from __future__ import annotations

from collections.abc import Sequence

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
    """Absolute, uncalibrated body-fat percentage estimate, clamped to a
    plausible range. Not exposed directly - see calc_relative_body_fat_pct.
    """
    bmi = _raw_bmi(weight_kg, height_cm)
    if bmi is None:
        return None
    fn = _FORMULA_DISPATCH.get(formula, _FORMULA_DISPATCH[DEFAULT_BODY_FAT_FORMULA])
    pct = fn(bmi, age_years, sex)
    return round(_clamp_body_fat(pct), 1)


def calc_baseline_body_fat_pct(recent_values: Sequence[float]) -> float | None:
    """A person's baseline = the average of their N most recent absolute
    body-fat% readings (N = const.BASELINE_MEASUREMENT_COUNT). None if no
    values are available yet.
    """
    if not recent_values:
        return None
    return round(sum(recent_values) / len(recent_values), 1)


def calc_relative_body_fat_pct(body_fat_pct: float | None, baseline_pct: float | None) -> float | None:
    """Body-fat% expressed relative to a personal baseline (baseline = 100%).

    None until a baseline exists yet (see calc_baseline_body_fat_pct) - the
    absolute estimate isn't calibrated, so there's nothing meaningful to
    show before there's a personal reference point to compare it against.
    """
    if body_fat_pct is None or not baseline_pct:
        return None
    return round(body_fat_pct / baseline_pct * 100, 1)
