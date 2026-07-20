"""Persistence of registered people + related mutable state.

Backed by Home Assistant's `Store` helper (versioned JSON under
`.storage/okok_scale.people`). Holds the things that must survive a
restart: the people themselves, any in-progress "register new person"
arming window, and the most recent weighing's assignment (so the reassign
select entity keeps working after a restart).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import STORAGE_KEY, STORAGE_VERSION
from .models import Person

_LOGGER = logging.getLogger(__name__)


def _person_to_dict(person: Person) -> dict[str, Any]:
    return {
        "id": person.id,
        "name": person.name,
        "sex": person.sex,
        "age_years": person.age_years,
        "height_cm": person.height_cm,
        "activity_level": person.activity_level,
        "created": person.created,
        "ref_weight_kg": person.ref_weight_kg,
        "ref_impedance": person.ref_impedance,
    }


def _person_from_dict(data: dict[str, Any]) -> Person:
    return Person(
        id=data["id"],
        name=data["name"],
        sex=data["sex"],
        age_years=data["age_years"],
        height_cm=data["height_cm"],
        activity_level=data.get("activity_level", "normal"),
        created=data.get("created", ""),
        ref_weight_kg=data.get("ref_weight_kg"),
        ref_impedance=data.get("ref_impedance"),
    )


class PersonStore:
    """Loads/saves people + pending_registration + last_assignment."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.people: dict[str, Person] = {}
        #: {"person_id": str, "armed_at": float} while a registration
        #: capture window is open, else None.
        self.pending_registration: dict[str, Any] | None = None
        #: {"session_id", "person_id", "weight_kg", "impedance", "timestamp"}
        #: for the most recent weighing, so it can be corrected via
        #: select.okok_scale_reassign_last.
        self.last_assignment: dict[str, Any] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.people = {pid: _person_from_dict(pdata) for pid, pdata in data.get("people", {}).items()}
        self.pending_registration = data.get("pending_registration")
        self.last_assignment = data.get("last_assignment")

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "people": {pid: _person_to_dict(person) for pid, person in self.people.items()},
                "pending_registration": self.pending_registration,
                "last_assignment": self.last_assignment,
            }
        )

    def new_person_id(self, name: str) -> str:
        """A unique slug id for a new person, derived from their name."""
        base = slugify(name) or "person"
        candidate = base
        suffix = 2
        while candidate in self.people:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    async def async_add_person(self, person: Person) -> None:
        self.people[person.id] = person
        await self.async_save()

    async def async_update_person(self, person: Person) -> None:
        self.people[person.id] = person
        await self.async_save()

    async def async_remove_person(self, person_id: str) -> None:
        self.people.pop(person_id, None)
        await self.async_save()

    async def async_arm_registration(self, person_id: str, armed_at: float) -> None:
        self.pending_registration = {"person_id": person_id, "armed_at": armed_at}
        await self.async_save()

    async def async_clear_pending_registration(self) -> None:
        self.pending_registration = None
        await self.async_save()

    async def async_set_last_assignment(self, assignment: dict[str, Any] | None) -> None:
        self.last_assignment = assignment
        await self.async_save()
