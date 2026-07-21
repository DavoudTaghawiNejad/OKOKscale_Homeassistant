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
from .body_composition import (
    calc_baseline_body_fat_pct,
    calc_body_fat_pct,
    calc_body_water_pct,
    calc_relative_body_fat_pct,
    calc_resistance_ohms,
)
from .const import (
    BASELINE_MEASUREMENT_COUNT,
    BUILD_TIMESTAMP,
    CONF_BODY_FAT_FORMULA,
    CONF_SCALE_MAC,
    CSV_DIR_NAME,
    DEFAULT_BODY_FAT_FORMULA,
    DOMAIN,
    LAST_MEASUREMENT_TIMEOUT_SECONDS,
    REASSIGN_MAX_AGE_SECONDS,
    REGISTRATION_ARMING_SECONDS,
    SESSION_GAP_SECONDS,
    STABLE_SESSION_GAP_SECONDS,
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
        #: A session captured for an armed *anonymous* registration (the
        #: "Add person" dialog arms before a Person exists yet) that hasn't
        #: been turned into a person by async_complete_pending_capture yet.
        self.pending_capture_session: Session | None = None
        #: Best-effort live peek at the in-progress session's latest frame,
        #: updated on every advertisement regardless of session completion -
        #: lets the "Add person" dialog show a reading before it locks.
        self.live_reading: dict[str, Any] | None = None

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
            sw_version=BUILD_TIMESTAMP,
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

        current = self._assembler.current
        if current is not None and current.frames:
            latest = current.final_frame
            self.live_reading = {
                "weight_kg": latest.weight_kg,
                "impedance": latest.impedance,
                "stable": latest.stable,
            }

        # (Re)schedule the timer that closes a session out even if the
        # scale simply goes quiet and no further advertisement ever
        # arrives to trigger the inline gap-detection path in `ingest()`.
        # Once the in-progress session has locked, a much shorter gap is
        # enough to consider it finished (see STABLE_SESSION_GAP_SECONDS) -
        # this is what makes the "Add person" dialog, last-measurement
        # sensor, and CSV logging react within a few seconds instead of up
        # to a minute after someone steps off.
        gap = STABLE_SESSION_GAP_SECONDS if (current is not None and current.has_stable_frame) else SESSION_GAP_SECONDS
        if self._timeout_cancel is not None:
            self._timeout_cancel()
        self._timeout_cancel = async_call_later(self.hass, gap + 1, self._async_check_timeout)

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
        if pending is not None:
            if is_registration_armed(pending["armed_at"], now, REGISTRATION_ARMING_SECONDS):
                await self.store.async_clear_pending_registration()
                pending_person_id = pending.get("person_id")
                if pending_person_id is None:
                    # Anonymous capture for the "Add person" dialog: don't
                    # assign/log anything until async_complete_pending_capture
                    # is told what profile to attach it to. If a *previous*
                    # anonymous capture is still sitting here unclaimed (the
                    # dialog was closed without finishing), recover it via
                    # normal matching now rather than silently discarding it
                    # when it gets overwritten below.
                    if self.pending_capture_session is not None:
                        await self._async_recover_abandoned_capture()
                    self.pending_capture_session = session
                    return
                await self._async_record_measurement(session, pending_person_id)
                return
            # Armed but expired by the time this session finished - drop
            # it and fall through to normal matching below.
            await self.store.async_clear_pending_registration()

        person_id = match_person(
            final.weight_kg,
            final.impedance,
            list(self.store.people.values()),
        )

        if person_id is None:
            _LOGGER.warning(
                "okok_scale: weighing of %.2f kg could not be assigned to any "
                "registered person (register someone first)",
                final.weight_kg,
            )
            return

        await self._async_record_measurement(session, person_id)

    async def _async_recover_abandoned_capture(self) -> None:
        """Salvage a session captured for an "Add person" dialog that was
        never finished (closed without waiting), via normal matching,
        instead of letting it be silently dropped."""
        stray = self.pending_capture_session
        self.pending_capture_session = None
        if stray is None:
            return
        final = stray.final_frame
        person_id = match_person(final.weight_kg, final.impedance, list(self.store.people.values()))
        if person_id is not None:
            await self._async_record_measurement(stray, person_id)

    async def _async_record_measurement(self, session: Session, person_id: str) -> None:
        person = self.store.people[person_id]
        final = session.final_frame
        timestamp = dt_util.now().isoformat(timespec="seconds")

        final_body_fat_pct = calc_body_fat_pct(
            final.weight_kg, person.height_cm, person.age_years, person.sex, final.impedance, self.body_fat_formula
        )
        final_resistance_ohms = calc_resistance_ohms(final.impedance)
        final_body_water_pct = calc_body_water_pct(final.weight_kg, person.height_cm, person.sex, final.impedance)

        # Update the rolling baseline history and auto-establish the
        # baseline itself the first time it fills up (see
        # const.BASELINE_MEASUREMENT_COUNT) - only touched once per
        # session, using the final value, not once per logged frame below.
        if final_body_fat_pct is not None:
            person.recent_body_fat_history = (person.recent_body_fat_history + [final_body_fat_pct])[
                -BASELINE_MEASUREMENT_COUNT:
            ]
            if person.baseline_body_fat_pct is None and len(person.recent_body_fat_history) == BASELINE_MEASUREMENT_COUNT:
                person.baseline_body_fat_pct = calc_baseline_body_fat_pct(person.recent_body_fat_history)

        final_relative_pct = calc_relative_body_fat_pct(final_body_fat_pct, person.baseline_body_fat_pct)

        # Log every frame of the session (the settling curve), not just the
        # final value, so the CSV is directly graphable.
        for frame in session.frames:
            frame_body_fat_pct = calc_body_fat_pct(
                frame.weight_kg, person.height_cm, person.age_years, person.sex, frame.impedance, self.body_fat_formula
            )
            frame_relative_pct = calc_relative_body_fat_pct(frame_body_fat_pct, person.baseline_body_fat_pct)
            frame_resistance_ohms = calc_resistance_ohms(frame.impedance)
            frame_body_water_pct = calc_body_water_pct(frame.weight_kg, person.height_cm, person.sex, frame.impedance)
            await self.csv_logger.async_append_row(
                person_id,
                {
                    "time": timestamp,
                    "session_id": session.id,
                    "weight_kg": frame.weight_kg,
                    "impedance": frame.impedance,
                    "body_fat_pct": frame_body_fat_pct,
                    "body_fat_relative_pct": frame_relative_pct,
                    "resistance_ohms": frame_resistance_ohms,
                    "body_water_pct": frame_body_water_pct,
                },
            )

        person.ref_weight_kg = final.weight_kg
        person.ref_impedance = final.impedance
        await self.store.async_update_person(person)

        measurement = {
            "session_id": session.id,
            "person_id": person_id,
            "person_name": person.name,
            "weight_kg": final.weight_kg,
            "impedance": final.impedance,
            "timestamp": timestamp,
            "assigned_at": time.time(),
            "body_fat_pct": final_body_fat_pct,
            "body_fat_relative_pct": final_relative_pct,
            "resistance_ohms": final_resistance_ohms,
            "body_water_pct": final_body_water_pct,
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
        """Sync a person's ref_weight_kg/ref_impedance + displayed sensors
        from their CSV's last row.

        Used on startup, after a reassignment moves rows in or out of a
        person's file, and after resetting their baseline. Always
        overwrites ref_weight_kg/ref_impedance rather than only seeding
        when unset (a reassignment moving rows *out* must follow the row
        that's now actually last, not stay pinned to the just-reassigned-
        away value). body_fat_relative_pct is always recomputed fresh from
        the row's absolute body_fat_pct against the person's *current*
        baseline, never trusted from the CSV's own (point-in-time) value.
        """
        row = await self.csv_logger.async_read_last_row(person_id)
        person = self.store.people.get(person_id)

        if row is None:
            self.person_data.pop(person_id, None)
            if person is not None and (person.ref_weight_kg is not None or person.ref_impedance is not None):
                person.ref_weight_kg = None
                person.ref_impedance = None
                await self.store.async_update_person(person)
            return

        weight_kg = float(row["weight_kg"])
        impedance = int(float(row["impedance"])) if row.get("impedance") else 0
        if person is not None and (person.ref_weight_kg != weight_kg or person.ref_impedance != impedance):
            person.ref_weight_kg = weight_kg
            person.ref_impedance = impedance
            await self.store.async_update_person(person)

        def _optional_float(value: str | None) -> float | None:
            return float(value) if value not in (None, "") else None

        body_fat_pct = _optional_float(row.get("body_fat_pct"))
        baseline = person.baseline_body_fat_pct if person is not None else None

        self.person_data[person_id] = {
            "session_id": row.get("session_id"),
            "person_id": person_id,
            "person_name": person.name if person is not None else person_id,
            "weight_kg": weight_kg,
            "impedance": impedance,
            "timestamp": row.get("time"),
            "body_fat_pct": body_fat_pct,
            "body_fat_relative_pct": calc_relative_body_fat_pct(body_fat_pct, baseline),
            "resistance_ohms": _optional_float(row.get("resistance_ohms")),
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

    async def async_complete_pending_capture(
        self,
        *,
        name: str,
        sex: str,
        age_years: int,
        height_cm: float,
        activity_level: str = "normal",
    ) -> Person:
        """Create a person from a just-captured anonymous session.

        Used by the "Add person" dialog, which arms an anonymous capture
        (no Person exists yet - see async_arm_registration/person_id=None)
        instead of creating the person up front, so nothing is saved until
        a real weighing has actually been captured. Raises ValueError if
        there's no captured session waiting (caller should check
        pending_capture_session first).
        """
        session = self.pending_capture_session
        if session is None:
            raise ValueError("no pending capture session to complete")
        self.pending_capture_session = None
        person = await self.async_add_person(
            name=name, sex=sex, age_years=age_years, height_cm=height_cm, activity_level=activity_level
        )
        await self._async_record_measurement(session, person.id)
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

    async def async_arm_registration(self, person_id: str | None) -> None:
        """Arm the next completed weighing for unconditional capture.

        `person_id=None` arms an *anonymous* capture for the "Add person"
        dialog, which doesn't create the person until the weighing is
        actually captured - see async_complete_pending_capture.
        """
        await self.store.async_arm_registration(person_id, time.time())

    async def async_cancel_pending_registration(self) -> None:
        """Clear an armed-but-abandoned registration immediately.

        Not strictly required for correctness (a stale entry is harmless -
        `_async_finish_session` treats it as expired and clears it lazily
        on the next weighing, and arming again just overwrites it), but
        avoids a stale window lingering after e.g. the "Add person" dialog
        times out.
        """
        await self.store.async_clear_pending_registration()

    async def async_reset_baseline(self, person_id: str) -> bool:
        """Recompute a person's baseline from their current rolling
        history (their most recent BASELINE_MEASUREMENT_COUNT absolute
        body-fat% readings - fewer if they don't have that many yet).

        Returns False (a no-op) if the person has no history at all yet.
        """
        person = self.store.people.get(person_id)
        if person is None or not person.recent_body_fat_history:
            return False

        person.baseline_body_fat_pct = calc_baseline_body_fat_pct(person.recent_body_fat_history)
        await self.store.async_update_person(person)
        await self._async_refresh_person_from_csv(person_id)
        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{person_id}")
        return True

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
            target_baseline_body_fat_pct=target_person.baseline_body_fat_pct,
        )
        if not moved_rows:
            return False

        await self._async_refresh_person_from_csv(from_person_id)
        await self._async_refresh_person_from_csv(target_person_id)

        new_measurement = {
            **self.person_data.get(target_person_id, {}),
            "assigned_at": time.time(),
        }
        self.last_measurement = new_measurement
        await self.store.async_set_last_assignment(new_measurement)

        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{from_person_id}")
        async_dispatcher_send(self.hass, f"{SIGNAL_PERSON_UPDATED}_{self.entry_id}_{target_person_id}")
        async_dispatcher_send(self.hass, f"{SIGNAL_LAST_MEASUREMENT_UPDATED}_{self.entry_id}")
        return True
