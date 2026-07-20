"""Select platform: correct a wrong auto-identification.

A single `select.okok_scale_reassign_last` entity. Choosing a person moves
the most recent weighing session to them (rewriting both people's CSVs and
refreshing every affected sensor - see
coordinator.OkokScaleCoordinator.async_reassign_last_measurement), then the
select resets itself back to "(no change)".
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SIGNAL_LAST_MEASUREMENT_UPDATED, OkokScaleCoordinator

NO_CHANGE = "(no change)"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OkokScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OkokScaleReassignSelect(coordinator)])


class OkokScaleReassignSelect(SelectEntity):
    """Corrects a wrong auto-identification by moving the last weighing."""

    _attr_has_entity_name = True
    _attr_translation_key = "reassign_last"
    _attr_should_poll = False
    _attr_icon = "mdi:account-switch"
    _attr_current_option = NO_CHANGE

    def __init__(self, coordinator: OkokScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry_id}_reassign_last"
        self.entity_id = f"select.{DOMAIN}_reassign_last"
        self._attr_device_info = coordinator.hub_device_info()

    @property
    def options(self) -> list[str]:
        return [NO_CHANGE, *(p.name for p in self._coordinator.people.values())]

    async def async_select_option(self, option: str) -> None:
        if option != NO_CHANGE:
            target = next((p for p in self._coordinator.people.values() if p.name == option), None)
            if target is not None:
                await self._coordinator.async_reassign_last_measurement(target.id)

        # Always snap back to "(no change)" once the correction (or no-op) is applied.
        self._attr_current_option = NO_CHANGE
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_LAST_MEASUREMENT_UPDATED}_{self._coordinator.entry_id}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
