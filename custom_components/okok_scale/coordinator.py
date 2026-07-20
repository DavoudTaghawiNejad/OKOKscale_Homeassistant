"""Live state for the OKOK scale integration.

This integration is push-based (BLE advertisements arrive whenever someone
steps on the scale), so state lives here as a plain object rather than a
`homeassistant.helpers.update_coordinator.DataUpdateCoordinator`, which is
built around polling. Entities subscribe to the dispatcher signals defined
below and read state straight off this object.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .assignment import is_registration_armed, match_person
from .body_composition import compute_body_composition
from .const import (
    CONF_BODY_FAT_FORMULA,
    CONF_MATCH_TOLERANCE_KG,
    CONF_SCALE_MAC,
    CSV_DIR_NAME,
    DEFAULT_BODY_FAT_FORMULA,
    DEFAULT_MATCH_TOLERANCE_KG,
    DOMAIN,
    LAST_MEASUREMENT_TIMEOUT_SECONDS,
    REASSIGN_MAX_AGE_SECONDS,
    REGISTRATION_ARMING_SECONDS,
    SESSION_GAP_SECONDS,
)
from .csv_logger import CsvLogger
from .models import Person
from .scale_parser import Session, SessionAssembler

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

SIGNAL_PERSON_UPDATED = f"{DOMAIN}_person_updated"
SIGNAL_LAST_MEASUREMENT_UPDATED = f"{DOMAIN}_last_measurement_updated"


class OkokScaleCoordinator:
    """Owns the BLE listener, session assembly, person matching, and the
    resulting per-person + "last measurement" state that entities read.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id

        self.scale_mac: str = entry.options.get(CONF_SCALE_MAC, entry.data[CONF_SCALE_MAC])
        self.match_tolerance_kg: float = entry.options.get(CONF_MATCH_TOLERANCE_KG, DEFAULT_MATCH_TOLERANCE_KG)
        self.body_fat_formula: str = entry.options.get(CONF_BODY_FAT_FORMULA, DEFAULT_BODY_FAT_FORMULA)

        # Import here to keep person_store's real `homeassistant.helpers.storage`
        # import out of modules that need to stay importable without HA installed.
        from .person_store import PersonStore

        self.store = PersonStore(hass)
        self.csv_dir: Path = Path(hass.config.path(CSV_DIR_NAME, "csv"))
        self.csv_logger = CsvLogger(hass, self.csv_dir)
        self._assembler = SessionAssembler(self.scale_mac)

        #: person_id -> latest measurement dict (mirrors what's in their CSV)
        self.person_data: dict[str, dict[str, Any]] = {}
        #: the current value of sensor.okok_scale_last_measurement, or None
        self.last_measurement: dict[str, Any] | None = None

        self._unregister_ble: Callable[[], None] | None = None
        self._timeout_cancel: Callable[[], None] | None = None
        self._last_measurement_cancel: Callable[[], None] | None = None

    @property
    def people(self) -> dict[str, Person]:
        return self.store.people

    def csv_download_url(self, person_id: str) -> str:
        from .const import STATIC_CSV_URL_PATH

        return f"{STATIC_CSV_URL_PATH}/{person_id}.csv"

    def hub_device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            name="OKOK Body Composition Scale",
            manufacturer="Chipsea (OKOK-branded)",
            model=self.scale_mac,
        )

    def person_device_info(self, person: Person) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_{person.id}")},
            name=person.name,
            manufacturer="Chipsea (OKOK-branded)",
            model="OKOK Body Composition Scale",
            via_device=(DOMAIN, self.entry_id),
        )

    # ---- lifecycle ------------------------------------------------------

    async def async_setup(self) -> None:
        await self.store.async_load()
        for person_id in list(self.store.people):
            await self._async_refresh_person_from_csv(person_id)

        self._unregister_ble = bluetooth.async_register_callback(
            self.hass,
            self._async_handle_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.scale_mac, connectable=False),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def async_unload(self) -> None:
        if self._unregister_ble is not None:
            self._unregister_ble()
            self._unregister_ble = None
        if self._timeout_cancel is not None:
            self._timeout_cancel()
            self._timeout_cancel = None
        if self._last_measurement_cancel is not None:
            self._last_measurement_cancel()
            self._last_measurement_cancel = None

    # ---- BLE ingestion ----------------------------------------------------

    @callback
    def _async_handle_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        manufacturer_data = service_info.manufacturer_data
        if _LOGGER.isEnabledFor(logging.DEBUG):
            for mfr_id, payload in manufacturer_data.items():
                _LOGGER.debug(
                    "okok_scale raw frame mfr_id=0x%04x payload=%s", mfr_id, payload.hex()
                )

        now = time.monotonic()
        completed = self._assembler.ingest(manufacturer_data, now)
        if completed is not None:
            self.hass.async_create_task(self._async_finish_session(completed))

        # (Re)schedule the timer that closes a session out even if the
        # scale simply goes quiet and no further advertisement ever
        # arrives to trigger the inline gap-detection path in `ingest()`.
        if self._timeout_cancel is not None:
            self._timeout_cancel()
        self._timeout_cancel = async_call_later(
            self.hass, SESSION_GAP_SECONDS + 1, self._async_check_timeout
        )

    @callback
    def _async_check_timeout(self, _now: Any) -> None:
        self._timeout_cancel = None
        completed = self._assembler.check_timeout(time.monotonic())
        if completed is not None:
            self.hass.async_create_task(self._async_finish_session(completed))

    # ---- session -> person assignment -------------------------------------

    async def _async_finish_session(self, session: Session) -> None:
        final = session.final_frame
        _LOGGER.debug(
            "okok_scale session %s finished: weight=%.2fkg impedance=%d stable=%s frames=%d",
            session.id,
            final.weight_kg,
            final.impedance,
            final.stable,
            len(session.frames),
        )

        now = time.time()
        pending = self.store.pending_registration
        pending_person_id: str | None = None
        if pending is not None:
            if is_registration_armed(pending["armed_at"], now, REGISTRATION_ARMING_SECONDS):
                pending_person_id = pending["person_id"]
            await self.store.async_clear_pending_registration()

        person_id = match_person(
            final.weight_kg,
            list(self.store.people.values()),
            pending_person_id=pending_person_id,
            match_tolerance_kg=self.match_tolerance_kg,
        )

        if person_id is None:
            _LOGGER.warning(
                "okok_scale: weighing of %.2f kg could not be assigned to any "
                "registered person (register someone first)",
                final.weight_kg,
            )
            return

        await self._async_record_measurement(session, person_id)

    async def _async_record_measurement(self, session: Session, person_id: str) -> None:
        person = self.store.people[person_id]
        final = session.final_frame
        timestamp = dt_util.now().isoformat(timespec="seconds")

        # Log every frame of the session (the settling curve), not just the
        # final value, so the CSV is directly graphable.
        for frame in session.frames:
            composition = compute_body_composition(
                frame.weight_kg,
                person.height_cm,
                person.age_years,
                person.sex,
                frame.impedance,
                self.body_fat_formula,
            )
            await self.csv_logger.async_append_row(
                person_id,
                {
                    "time": timestamp,
                    "session_id": session.id,
                    "weight_kg": frame.weight_kg,
                    "impedance": frame.impedance,
                    **composition,
                },
            )

        final_composition = compute_body_composition(
            final.weight_kg,
            person.height_cm,
            person.age_years,
            person.sex,
            final.impedance,
            self.body_fat_formula,
        )

        person.ref_weight_kg = final.weight_kg
        await self.store.async_update_person(person)

        measurement = {
            "session_id": session.id,
            "person_id": person_id,
            "person_name": person.name,
            "weight_kg": final.weight_kg,
            "impedance": final.impedance,
            "timestamp": timestamp,
            "assigned_at": time.time(),
            **final_composition,
        }
        self.person_data[person_id] = measurement
        self.last_measurement = measurement
        await self.store.async_set_last_assignment(measurement)

        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{person_id}")
        async_dispatcher_send(self.hass, f"{SIGNAL_LAST_MEASUREMENT_UPDATED}_{self.entry_id}")

        if self._last_measurement_cancel is not None:
            self._last_measurement_cancel()
        self._last_measurement_cancel = async_call_later(
            self.hass, LAST_MEASUREMENT_TIMEOUT_SECONDS, self._async_expire_last_measurement
        )

    @callback
    def _async_expire_last_measurement(self, _now: Any) -> None:
        self._last_measurement_cancel = None
        self.last_measurement = None
        async_dispatcher_send(self.hass, f"{SIGNAL_LAST_MEASUREMENT_UPDATED}_{self.entry_id}")

    async def _async_refresh_person_from_csv(self, person_id: str) -> None:
        """Seed a person's ref_weight_kg / displayed sensors from their CSV.

        Used on startup, and after a reassignment moves rows out of a
        person's file (their displayed state must fall back to whatever
        row is now actually last, or go blank if none remain).
        """
        row = await self.csv_logger.async_read_last_row(person_id)
        person = self.store.people.get(person_id)

        if row is None:
            self.person_data.pop(person_id, None)
            if person is not None and person.ref_weight_kg is not None:
                person.ref_weight_kg = None
                await self.store.async_update_person(person)
            return

        weight_kg = float(row["weight_kg"])
        if person is not None and person.ref_weight_kg is None:
            person.ref_weight_kg = weight_kg
            await self.store.async_update_person(person)

        def _optional_float(value: str | None) -> float | None:
            return float(value) if value not in (None, "") else None

        self.person_data[person_id] = {
            "session_id": row.get("session_id"),
            "person_id": person_id,
            "person_name": person.name if person is not None else person_id,
            "weight_kg": weight_kg,
            "impedance": int(_optional_float(row.get("impedance")) or 0),
            "timestamp": row.get("time"),
            "bmi": _optional_float(row.get("bmi")),
            "body_fat_pct": _optional_float(row.get("body_fat_pct")),
            "lean_mass_kg": _optional_float(row.get("lean_mass_kg")),
            "body_water_pct": _optional_float(row.get("body_water_pct")),
        }

    # ---- registration -----------------------------------------------------

    async def async_add_person(
        self,
        *,
        name: str,
        sex: str,
        age_years: int,
        height_cm: float,
        activity_level: str = "normal",
    ) -> Person:
        person_id = self.store.new_person_id(name)
        person = Person(
            id=person_id,
            name=name,
            sex=sex,  # type: ignore[arg-type]
            age_years=age_years,
            height_cm=height_cm,
            activity_level=activity_level,
            created=dt_util.now().isoformat(timespec="seconds"),
        )
        await self.store.async_add_person(person)
        await self._async_refresh_person_from_csv(person_id)  # seeds ref_weight_kg if a CSV already exists
        return person

    async def async_update_person(self, person: Person) -> None:
        await self.store.async_update_person(person)

    async def async_remove_person(self, person_id: str) -> None:
        """Remove a person's registration, entities, and device.

        Dropping them from the store alone isn't enough: the next platform
        setup just stops re-creating their entities, but Home Assistant
        doesn't know to delete ones it already registered, so they'd be
        left behind as an "unavailable" orphaned device. Removing the
        device explicitly cascades to remove all of its entities too (see
        entity_registry.EntityRegistry.async_device_modified).
        """
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, f"{self.entry_id}_{person_id}")})
        if device is not None:
            device_registry.async_remove_device(device.id)

        await self.store.async_remove_person(person_id)
        self.person_data.pop(person_id, None)

    async def async_arm_registration(self, person_id: str) -> None:
        await self.store.async_arm_registration(person_id, time.time())

    # ---- manual reassignment ------------------------------------------

    async def async_reassign_last_measurement(self, target_person_id: str) -> bool:
        """Move the most recent weighing session to a different person.

        Returns False (a no-op) if there's nothing to reassign, it's too
        old, the target doesn't exist, or it's already assigned there.
        """
        assignment = self.store.last_assignment
        if assignment is None:
            return False
        if time.time() - assignment.get("assigned_at", 0) > REASSIGN_MAX_AGE_SECONDS:
            _LOGGER.debug("okok_scale: ignoring reassign, last measurement is stale")
            return False

        from_person_id = assignment["person_id"]
        if from_person_id == target_person_id:
            return False

        target_person = self.store.people.get(target_person_id)
        if target_person is None:
            return False

        moved_rows = await self.csv_logger.async_reassign_session(
            from_person_id,
            target_person_id,
            assignment["session_id"],
            target_height_cm=target_person.height_cm,
            target_age_years=target_person.age_years,
            target_sex=target_person.sex,
            target_formula=self.body_fat_formula,
        )
        if not moved_rows:
            return False

        await self._async_refresh_person_from_csv(from_person_id)
        await self._async_refresh_person_from_csv(target_person_id)

        new_final = moved_rows[-1]
        new_measurement = {
            "session_id": assignment["session_id"],
            "person_id": target_person_id,
            "person_name": target_person.name,
            "weight_kg": new_final["weight_kg"],
            "impedance": new_final["impedance"],
            "timestamp": assignment["timestamp"],
            "assigned_at": time.time(),
            "bmi": new_final["bmi"],
            "body_fat_pct": new_final["body_fat_pct"],
            "lean_mass_kg": new_final["lean_mass_kg"],
            "body_water_pct": new_final["body_water_pct"],
        }
        self.last_measurement = new_measurement
        await self.store.async_set_last_assignment(new_measurement)

        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{from_person_id}")
        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{target_person_id}")
        async_dispatcher_send(self.hass, f"{SIGNAL_LAST_MEASUREMENT_UPDATED}_{self.entry_id}")
        return True
