"""Plain data models shared across the OKOK Scale integration.

Deliberately free of Home Assistant imports so they can be used from the
pure `scale_parser` / `body_composition` modules and tested standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Sex = Literal["male", "female"]


@dataclass
class Person:
    """A registered household member."""

    id: str
    name: str
    sex: Sex
    age_years: int
    height_cm: float
    activity_level: str = "normal"
    created: str = ""
    ref_weight_kg: float | None = None
    ref_impedance: int | None = None
    #: Personal body-fat baseline (100% reference point) - the average of
    #: recent_body_fat_history at the time it was last (re)established.
    #: None until BASELINE_MEASUREMENT_COUNT measurements exist.
    baseline_body_fat_pct: float | None = None
    #: Rolling window of this person's most recent absolute body-fat%
    #: readings, capped at BASELINE_MEASUREMENT_COUNT. Source data for both
    #: the automatic first-time baseline and "reset baseline".
    recent_body_fat_history: list[float] = field(default_factory=list)
    #: Same as baseline_body_fat_pct/recent_body_fat_history, but for
    #: body-water% (see body_composition.calc_body_water_pct). Tracked
    #: separately since a session without a usable impedance reading (e.g.
    #: it never locked) has a body-fat% but no body-water%, so the two
    #: histories can drift out of sync with each other.
    baseline_body_water_pct: float | None = None
    recent_body_water_history: list[float] = field(default_factory=list)


@dataclass
class ScaleFrame:
    """One decoded, validated manufacturer-data frame from the scale."""

    counter: int
    weight_kg: float
    impedance: int
    stable: bool
    raw_mfr_id: int
    raw_payload: bytes

    @property
    def priority(self) -> tuple[bool, int]:
        """Sort key: locked/stable beats unlocked, then higher counter wins."""
        return (self.stable, self.counter)


@dataclass
class Measurement:
    """The resolved outcome of a completed weighing session."""

    session_id: str
    timestamp: str
    weight_kg: float
    impedance: int
    person_id: str | None = None
    body_fat_pct: float | None = None
    body_fat_relative_pct: float | None = None
    frames: list[ScaleFrame] = field(default_factory=list)
