"""Pure body-composition estimation formulas.

No Home Assistant imports: unit-tested in isolation
(see tests/test_body_composition.py).

Honesty note (also surfaced in the README): none of the body-*fat*
formulas below actually consume the scale's bio-impedance reading. They
are the BMI/age/sex estimation formulas published on the openScale wiki's
"Body metric estimations" page, which is what openScale itself uses for
scales like this one that don't document a calibrated impedance
regression. Because no such calibration exists for body fat, the
integration doesn't expose this absolute body-fat estimate directly -
only *relative to a personal baseline* (see calc_relative_body_fat_pct /
calc_baseline_body_fat_pct), where a lot of the systematic error cancels
out since it's the same bias applied consistently to every reading for
that person.

Body *water* is different: calc_body_water_pct below is a genuine
bio-impedance (BIA) estimate - Sun et al. 2003's population regression
(also adopted by openScale's own StandardImpedanceLib.kt for scales like
this one), which needs the true resistance in ohms rather than BMI alone.
That resistance isn't what the scale broadcasts directly - see
calc_resistance_ohms and const.IMPEDANCE_RAW_UNITS_PER_OHM for the raw-to-
ohms conversion this hardware needs first. Sun 2003 is a well-validated
*general-population* regression (SEE roughly 3-5% of body weight against a
4-compartment reference method), not a regression calibrated for this
specific scale's electrodes, so treat the absolute percentage as a good
average-case estimate rather than a lab-grade measurement - and, same as
body fat, trust changes over time under consistent measurement conditions
more than any single absolute reading.
"""

from __future__ import annotations

from collections.abc import Sequence

from .const import (
    BODY_FAT_MAX_PCT,
    BODY_FAT_MIN_PCT,
    BODY_WATER_MAX_PCT,
    BODY_WATER_MIN_PCT,
    DEFAULT_BODY_FAT_FORMULA,
    FORMULA_DEURENBERG_1991,
    FORMULA_DEURENBERG_1992,
    FORMULA_EDDY_1976,
    FORMULA_GALLAGHER_2000,
    IMPEDANCE_RAW_UNITS_PER_OHM,
)
from .models import Sex

#: Converts total-body-water liters to kg, at ~36.5C average body
#: temperature - see Sun et al. 2003 / openScale's StandardImpedanceLib.kt.
_WATER_DENSITY_KG_PER_LITER = 0.99513


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


def _clamp_body_water(pct: float) -> float:
    return max(BODY_WATER_MIN_PCT, min(BODY_WATER_MAX_PCT, pct))


def calc_resistance_ohms(impedance: int | None) -> float | None:
    """Convert the scale's raw impedance reading to true resistance in
    ohms (see const.IMPEDANCE_RAW_UNITS_PER_OHM), or None for an
    unlocked/not-yet-measured frame (impedance 0 or missing).
    """
    if not impedance or impedance <= 0:
        return None
    return round(impedance / IMPEDANCE_RAW_UNITS_PER_OHM, 1)


def _sun_2003_total_body_water_kg(weight_kg: float, height_cm: float, resistance_ohms: float, sex: Sex) -> float:
    """TBW (Sun et al. 2003): height^2/resistance ("resistance index") plus
    weight, gender-specific coefficients. See module docstring.
    """
    h2r = height_cm * height_cm / resistance_ohms
    if sex == "male":
        liters = 1.2 + 0.45 * h2r + 0.18 * weight_kg
    else:
        liters = 3.75 + 0.45 * h2r + 0.11 * weight_kg
    return liters * _WATER_DENSITY_KG_PER_LITER


def calc_body_water_pct(
    weight_kg: float,
    height_cm: float,
    sex: Sex,
    impedance: int | None,
) -> float | None:
    """Total-body-water percentage via the Sun et al. 2003 BIA regression,
    clamped to a plausible range. None if weight/height are non-physical
    or impedance isn't available yet (see calc_resistance_ohms).
    """
    if weight_kg is None or weight_kg <= 0 or height_cm is None or height_cm <= 0:
        return None
    resistance_ohms = calc_resistance_ohms(impedance)
    if resistance_ohms is None:
        return None
    tbw_kg = _sun_2003_total_body_water_kg(weight_kg, height_cm, resistance_ohms, sex)
    return round(_clamp_body_water(tbw_kg / weight_kg * 100), 1)


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


def calc_baseline_body_water_pct(recent_values: Sequence[float]) -> float | None:
    """Same idea as calc_baseline_body_fat_pct, for body-water% - the
    average of a person's N most recent calc_body_water_pct readings.
    """
    if not recent_values:
        return None
    return round(sum(recent_values) / len(recent_values), 1)


def calc_relative_body_water_pct(body_water_pct: float | None, baseline_pct: float | None) -> float | None:
    """Body-water% expressed relative to a personal baseline (baseline =
    100%). None until a baseline exists yet (see
    calc_baseline_body_water_pct).

    Unlike body fat, the absolute body-water% is already exposed directly
    (see calc_body_water_pct's own docstring on why) - this is an
    additional, complementary view for spotting day-to-day hydration
    swings against a personal norm, not a stand-in for a missing absolute
    number.
    """
    if body_water_pct is None or not baseline_pct:
        return None
    return round(body_water_pct / baseline_pct * 100, 1)
