"""Sensor platform for OKOK Body Composition Scale.

Two sensors per registered person - weight, and body fat relative to their
personal baseline (100% = baseline; see coordinator.py /
body_composition.py for how the baseline itself is established/updated) -
plus one integration-wide "last measurement" sensor that names whoever was
most recently weighed and blanks itself after
const.LAST_MEASUREMENT_TIMEOUT_SECONDS.

Only weight and the baseline-relative body-fat figure are exposed as
entities. The scale's absolute body-fat estimate isn't calibrated (see
body_composition.py's module docstring), so it's kept internal - available
as an attribute on the relative sensor for transparency, but not promoted
to its own card/sensor.

Entities are push-updated via dispatcher signals fired by
coordinator.OkokScaleCoordinator - there's no polling here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BASELINE_MEASUREMENT_COUNT, DOMAIN
from .coordinator import SIGNAL_LAST_MEASUREMENT_UPDATED, SIGNAL_PERSON_UPDATED, OkokScaleCoordinator
from .models import Person


@dataclass(frozen=True, kw_only=True)
class OkokPersonSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


PERSON_SENSOR_DESCRIPTIONS: tuple[OkokPersonSensorDescription, ...] = (
    OkokPersonSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("weight_kg"),
    ),
    OkokPersonSensorDescription(
        key="body_fat_relative",
        translation_key="body_fat_relative",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("body_fat_relative_pct"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OkokScaleCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        OkokScalePersonSensor(coordinator, person, description)
        for person in coordinator.people.values()
        for description in PERSON_SENSOR_DESCRIPTIONS
    ]
    entities.append(OkokScaleLastMeasurementSensor(coordinator))
    async_add_entities(entities)


class OkokScalePersonSensor(SensorEntity):
    """One metric (weight or relative body fat) for one registered person."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: OkokPersonSensorDescription

    def __init__(
        self, coordinator: OkokScaleCoordinator, person: Person, description: OkokPersonSensorDescription
    ) -> None:
        self.entity_description = description
        self._coordinator = coordinator
        self._person_id = person.id
        self._attr_unique_id = f"{DOMAIN}_{person.id}_{description.key}"
        # Pinned explicitly (not left to has_entity_name auto-naming) so it
        # matches the documented sensor.okok_scale_<person>_<metric>
        # pattern the frontend card's auto-discovery regex relies on.
        self.entity_id = f"sensor.{DOMAIN}_{person.id}_{description.key}"
        self._attr_device_info = coordinator.person_device_info(person)

    @property
    def native_value(self) -> Any:
        data = self._coordinator.person_data.get(self._person_id)
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "weight":
            return {"csv_download_url": self._coordinator.csv_download_url(self._person_id)}
        if self.entity_description.key == "body_fat_relative":
            person = self._coordinator.people.get(self._person_id)
            data = self._coordinator.person_data.get(self._person_id) or {}
            history_count = len(person.recent_body_fat_history) if person is not None else 0
            baseline = person.baseline_body_fat_pct if person is not None else None
            return {
                "baseline_body_fat_pct": baseline,
                "absolute_body_fat_pct": data.get("body_fat_pct"),
                "measurements_until_baseline": (
                    max(0, BASELINE_MEASUREMENT_COUNT - history_count) if baseline is None else 0
                ),
            }
        return None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_PERSON_UPDATED}_{self._coordinator.entry_id}_{self._person_id}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class OkokScaleLastMeasurementSensor(SensorEntity):
    """Headline sensor: who was last weighed, blanking after 10 minutes."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_measurement"
    _attr_should_poll = False
    _attr_icon = "mdi:scale-bathroom"

    def __init__(self, coordinator: OkokScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry_id}_last_measurement"
        self.entity_id = f"sensor.{DOMAIN}_last_measurement"
        self._attr_device_info = coordinator.hub_device_info()

    @property
    def native_value(self) -> str | None:
        measurement = self._coordinator.last_measurement
        return measurement["person_name"] if measurement else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._coordinator.last_measurement

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
