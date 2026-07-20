"""Pure person-identification logic: who a completed weighing belongs to.

No Home Assistant imports: unit-tested in isolation via
tests/test_session_engine.py. coordinator.py is the only real caller; it
resolves the "is a registration armed right now" question (which needs a
wall clock) and then calls `match_person`, keeping the actual matching
decision - the part worth testing precisely - free of any HA/async
concerns.

Matching algorithm (see build brief section 3 for the arming-bypass part;
the rest replaces the original nearest-neighbour/tolerance scheme):

1. An armed registration (`pending_person_id`) wins unconditionally.
2. Every person with both a reference weight *and* a reference impedance
   is sorted by weight; the boundary between two consecutive people is
   the midpoint between their reference weights, so each person owns a
   contiguous interval (the lowest extends to -inf, the highest to +inf -
   there is no "too far to match" concept here). Find whose interval the
   measured weight falls in. Do the same, independently, sorted by
   impedance instead.
3. If both agree, that's the match.
4. If they disagree, narrow to just those two candidates and repeat the
   same midpoint-interval check once more, this time using weight *
   impedance as the single comparison axis between just the two of them.
5. If nobody has both a reference weight and impedance yet (a brand new
   household with nobody weighed even once), fall back to the first such
   not-yet-seeded person - otherwise a second/third person could never be
   picked up before they have reference data of their own.
6. No registered people at all -> None.

A measurement landing exactly on a midpoint is assigned to the
lower-value person (ties broken by `<=`) - arbitrary but deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .const import REGISTRATION_ARMING_SECONDS
from .models import Person


def is_registration_armed(
    armed_at: float | None,
    now: float,
    window_seconds: float = REGISTRATION_ARMING_SECONDS,
) -> bool:
    """Whether a pending "register new person" capture is still active."""
    if armed_at is None:
        return False
    return (now - armed_at) <= window_seconds


def _interval_match(value: float, people: Sequence[Person], key: Callable[[Person], float]) -> str:
    """Which person's midpoint-bounded interval `value` falls into.

    `people` is sorted by `key`; consecutive people are split at the
    midpoint of their reference values. Always returns somebody's id.
    """
    ordered = sorted(people, key=key)
    for person, next_person in zip(ordered, ordered[1:]):
        midpoint = (key(person) + key(next_person)) / 2
        if value <= midpoint:
            return person.id
    return ordered[-1].id


def match_person(
    weight_kg: float,
    impedance: int,
    people: Sequence[Person],
    *,
    pending_person_id: str | None = None,
) -> str | None:
    """Decide which registered person a completed weighing belongs to."""
    if pending_person_id is not None:
        return pending_person_id

    seeded = [p for p in people if p.ref_weight_kg is not None and p.ref_impedance is not None]
    if not seeded:
        unseeded = [p for p in people if p.ref_weight_kg is None or p.ref_impedance is None]
        return unseeded[0].id if unseeded else None

    weight_match = _interval_match(weight_kg, seeded, key=lambda p: p.ref_weight_kg)
    impedance_match = _interval_match(impedance, seeded, key=lambda p: p.ref_impedance)

    if weight_match == impedance_match:
        return weight_match

    candidates = [p for p in seeded if p.id in (weight_match, impedance_match)]
    return _interval_match(
        weight_kg * impedance,
        candidates,
        key=lambda p: p.ref_weight_kg * p.ref_impedance,
    )
