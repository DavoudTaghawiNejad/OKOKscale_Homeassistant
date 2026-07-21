"""Button platform: per-person download, re-arm-capture, baseline, and
history-management helper buttons.

The weight sensor already carries `csv_download_url` as an attribute (for
the custom card / a markdown dashboard link), but a button gives a
one-tap way to get the link from the dashboard without digging through
entity attributes: pressing it fires a persistent notification containing
a clickable link to that person's CSV, served from the static path
registered in __init__.py.

The arm-capture button covers a gap the pure options-flow "Add person"
arming (see config_flow.py) can't: a person only gets a reference
weight/impedance if they step on the scale within that first 120 s
window. Miss it, and - since the matching algorithm has no "too far to
match" fallback once anyone else is already seeded (see
assignment.match_person) - they can never be auto-matched again, not
just for that one missed weighing but for every one after it, until
they get a reference somehow. This button re-arms the exact same
capture window on demand, without needing to remove and re-add them.

reset_baseline and clear_history are two different kinds of "start over":
reset_baseline recalibrates "100%" from a person's *existing* recent
history (nothing is deleted), while clear_history permanently deletes
their CSV file and all tracked history/baselines - for when old data
(a test/debug session, a mismatched weighing, wanting a genuinely blank
slate) shouldn't factor into anything going forward at all.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, REGISTRATION_ARMING_SECONDS
from .coordinator import OkokScaleCoordinator
from .models import Person


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OkokScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    for person in coordinator.people.values():
        entities.append(OkokScaleDownloadCsvButton(coordinator, person))
        entities.append(OkokScaleArmCaptureButton(coordinator, person))
        entities.append(OkokScaleResetBaselineButton(coordinator, person))
        entities.append(OkokScaleClearHistoryButton(coordinator, person))
    async_add_entities(entities)


class OkokScaleDownloadCsvButton(ButtonEntity):
    """Pressing this fires a persistent notification with the CSV link."""

    _attr_has_entity_name = True
    _attr_translation_key = "download_csv"
    _attr_icon = "mdi:file-download"

    def __init__(self, coordinator: OkokScaleCoordinator, person: Person) -> None:
        self._coordinator = coordinator
        self._person_id = person.id
        self._person_name = person.name
        self._attr_unique_id = f"{DOMAIN}_{person.id}_download_csv"
        self.entity_id = f"button.{DOMAIN}_{person.id}_download_csv"
        self._attr_device_info = coordinator.person_device_info(person)

    async def async_press(self) -> None:
        url = self._coordinator.csv_download_url(self._person_id)
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"OKOK Scale: {self._person_name}'s CSV",
                "message": f"[Download {self._person_name}'s weight history]({url})",
                "notification_id": f"{DOMAIN}_{self._person_id}_csv_download",
            },
            blocking=False,
        )


class OkokScaleArmCaptureButton(ButtonEntity):
    """Pressing this arms a fresh capture window for this person.

    The next completed weighing is assigned to them unconditionally,
    exactly like the "Add person" registration arming - this is how to
    (re-)seed someone's reference weight/impedance without removing and
    re-adding them, e.g. after they missed their original window.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "arm_capture"
    _attr_icon = "mdi:scale-bathroom"

    def __init__(self, coordinator: OkokScaleCoordinator, person: Person) -> None:
        self._coordinator = coordinator
        self._person_id = person.id
        self._person_name = person.name
        self._attr_unique_id = f"{DOMAIN}_{person.id}_arm_capture"
        self.entity_id = f"button.{DOMAIN}_{person.id}_arm_capture"
        self._attr_device_info = coordinator.person_device_info(person)

    async def async_press(self) -> None:
        await self._coordinator.async_arm_registration(self._person_id)
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"OKOK Scale: {self._person_name}",
                "message": (
                    f"Capture window open for {REGISTRATION_ARMING_SECONDS} seconds - "
                    f"have {self._person_name} step on the scale now."
                ),
                "notification_id": f"{DOMAIN}_{self._person_id}_arm_capture",
            },
            blocking=False,
        )


class OkokScaleResetBaselineButton(ButtonEntity):
    """Pressing this sets this person's body-fat *and* body-water baselines
    (100% reference points) to the average of their most recent
    measurements of each.

    See sensor.py's body_fat_relative/body_water_relative sensors and
    coordinator.async_reset_baseline. Useful any time you want to reset
    "100%" to where they are now, e.g. at the start of a new fitness phase.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reset_baseline"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(self, coordinator: OkokScaleCoordinator, person: Person) -> None:
        self._coordinator = coordinator
        self._person_id = person.id
        self._person_name = person.name
        self._attr_unique_id = f"{DOMAIN}_{person.id}_reset_baseline"
        self.entity_id = f"button.{DOMAIN}_{person.id}_reset_baseline"
        self._attr_device_info = coordinator.person_device_info(person)

    async def async_press(self) -> None:
        ok = await self._coordinator.async_reset_baseline(self._person_id)
        message = (
            f"{self._person_name}'s body-fat and body-water baselines have been reset to their "
            "recent averages - that's now their new 100% for both."
            if ok
            else f"{self._person_name} has no measurements yet, so there's nothing to set a baseline from."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"OKOK Scale: {self._person_name}",
                "message": message,
                "notification_id": f"{DOMAIN}_{self._person_id}_reset_baseline",
            },
            blocking=False,
        )


class OkokScaleClearHistoryButton(ButtonEntity):
    """Pressing this permanently deletes this person's weighing history:
    their CSV file, reference weight/impedance, and both body-fat/body-
    water baselines and rolling histories - as if they'd never weighed in.

    Their registration (name, sex, age, height) is kept - use "Remove a
    person" in Configure instead if you want to delete them entirely.
    Unlike reset_baseline (which recalibrates "100%" from existing
    history), this deletes the history itself and cannot be undone.

    See coordinator.async_clear_history.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "clear_history"
    _attr_icon = "mdi:delete-sweep"

    def __init__(self, coordinator: OkokScaleCoordinator, person: Person) -> None:
        self._coordinator = coordinator
        self._person_id = person.id
        self._person_name = person.name
        self._attr_unique_id = f"{DOMAIN}_{person.id}_clear_history"
        self.entity_id = f"button.{DOMAIN}_{person.id}_clear_history"
        self._attr_device_info = coordinator.person_device_info(person)

    async def async_press(self) -> None:
        await self._coordinator.async_clear_history(self._person_id)
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"OKOK Scale: {self._person_name}",
                "message": (
                    f"{self._person_name}'s weighing history has been permanently deleted - "
                    "their CSV file, reference weight/impedance, and both baselines are all "
                    "gone, and this cannot be undone. Their profile is still registered - step "
                    "on the scale to start building fresh history."
                ),
                "notification_id": f"{DOMAIN}_{self._person_id}_clear_history",
            },
            blocking=False,
        )
