"""Button platform: one CSV-download helper button per registered person.

The weight sensor already carries `csv_download_url` as an attribute (for
the custom card / a markdown dashboard link), but a button gives a
one-tap way to get the link from the dashboard without digging through
entity attributes: pressing it fires a persistent notification containing
a clickable link to that person's CSV, served from the static path
registered in __init__.py.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import OkokScaleCoordinator
from .models import Person


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OkokScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OkokScaleDownloadCsvButton(coordinator, person) for person in coordinator.people.values()
    )


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
