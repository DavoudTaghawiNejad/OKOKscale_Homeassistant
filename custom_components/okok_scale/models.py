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
    bmi: float | None = None
    body_fat_pct: float | None = None
    lean_mass_kg: float | None = None
    body_water_pct: float | None = None
    frames: list[ScaleFrame] = field(default_factory=list)
