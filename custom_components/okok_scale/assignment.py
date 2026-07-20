"""Pure person-identification logic: who a completed weighing belongs to.

No Home Assistant imports: unit-tested in isolation via
tests/test_session_engine.py. coordinator.py is the only real caller; it
resolves the "is a registration armed right now" question (which needs a
wall clock) and then calls `match_person`, keeping the actual matching
decision - the part worth testing precisely - free of any HA/async
concerns.

See build brief section 3 ("Person identification") for the full spec.
"""

from __future__ import annotations

from typing import Sequence

from .const import DEFAULT_MATCH_TOLERANCE_KG, REGISTRATION_ARMING_SECONDS
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


def match_person(
    weight_kg: float,
    people: Sequence[Person],
    *,
    pending_person_id: str | None = None,
    match_tolerance_kg: float = DEFAULT_MATCH_TOLERANCE_KG,
) -> str | None:
    """Decide which registered person a completed weighing belongs to.

    Priority order:
      1. `pending_person_id` - the caller should only pass this through when
         a registration is currently armed and unexpired
         (see `is_registration_armed`); it wins unconditionally.
      2. Nearest-neighbour match against every person's `ref_weight_kg`.
      3. Bootstrap rule: if the nearest known ref is further than
         `match_tolerance_kg` away *and* at least one person still has no
         ref at all, assign to that not-yet-seeded person instead - this is
         what lets a second household member get recognised automatically
         the first few times, before they have their own reference weight.
      4. If nobody has a ref yet, the first not-yet-seeded person gets it.
      5. No registered people at all -> None (caller should surface this,
         e.g. as an unassigned measurement).
    """
    if pending_person_id is not None:
        return pending_person_id

    known = [(p.id, p.ref_weight_kg) for p in people if p.ref_weight_kg is not None]
    unseeded_ids = [p.id for p in people if p.ref_weight_kg is None]

    if known:
        nearest_id, nearest_ref = min(known, key=lambda pair: abs(weight_kg - pair[1]))
        nearest_distance = abs(weight_kg - nearest_ref)
        if unseeded_ids and nearest_distance > match_tolerance_kg:
            return unseeded_ids[0]
        return nearest_id

    if unseeded_ids:
        return unseeded_ids[0]

    return None
