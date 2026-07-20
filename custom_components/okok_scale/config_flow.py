"""Config flow for OKOK Body Composition Scale.

Adding the integration only asks for the scale's MAC address (with a
discovery pick-list when possible). Everything else - registering people,
picking a body-fat formula - lives in the options flow, reachable via
Settings -> Devices & Services -> OKOK Body Composition Scale -> Configure.

Registration UX decision (see README "Registering people"): "Add person"
collects name/sex/age/height, then arms a time-limited capture window (see
REGISTRATION_ARMING_SECONDS) - the next completed weighing session is
assigned to that person unconditionally. We chose this over a per-person
"arm capture" button because it keeps the very first weigh-in from ever
requiring the user to already have entities for a person who doesn't exist
yet.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    BODY_FAT_FORMULAS,
    CHIPSEA_MARKER_BYTE,
    CONF_BODY_FAT_FORMULA,
    CONF_SCALE_MAC,
    DEFAULT_BODY_FAT_FORMULA,
    DEFAULT_SCALE_MAC,
    DOMAIN,
    REGISTRATION_ARMING_SECONDS,
)
from .scale_parser import mac_str_to_bytes

_LOGGER = logging.getLogger(__name__)


def _discovered_scale_macs(hass: HomeAssistant) -> list[str]:
    """MAC addresses of any currently-visible 0xC0-family (Chipsea) device."""
    macs: list[str] = []
    for info in bluetooth.async_discovered_service_info(hass, connectable=False):
        if any((mfr_id & 0xFF) == CHIPSEA_MARKER_BYTE for mfr_id in info.manufacturer_data):
            macs.append(info.address)
    return macs


def _format_live_reading(live_reading: dict[str, Any] | None) -> str:
    """Best-effort "what's the scale showing right now" text for the
    add_person_done dialog. Not truly live (data_entry_flow forms can't
    push updates), but re-reads coordinator.live_reading - which updates on
    every advertisement, not just completed sessions - on every submit, so
    it's at least fresh as of the last time the user checked.
    """
    if live_reading is None:
        return "No reading yet."
    status = "locked - capturing" if live_reading["stable"] else "settling"
    return f"Currently reading {live_reading['weight_kg']:.2f} kg, {live_reading['impedance']} Ω ({status})"


class OkokScaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle adding the integration. Single instance only."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        discovered = _discovered_scale_macs(self.hass)

        if user_input is not None:
            mac = user_input[CONF_SCALE_MAC].strip().upper()
            try:
                mac_str_to_bytes(mac)
            except ValueError:
                errors["base"] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="OKOK Body Composition Scale",
                    data={CONF_SCALE_MAC: mac},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SCALE_MAC, default=discovered[0] if discovered else DEFAULT_SCALE_MAC): (
                    selector.selector(
                        {"select": {"options": discovered, "custom_value": True, "mode": "dropdown"}}
                    )
                    if discovered
                    else str
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OkokScaleOptionsFlow:
        return OkokScaleOptionsFlow()


class OkokScaleOptionsFlow(config_entries.OptionsFlow):
    """Add/edit/remove people, and tune matching + formula settings.

    Note: `self.config_entry` is *not* set here - modern Home Assistant
    exposes it as a read-only property (populated by the flow manager after
    construction, valid from the first async_step_* call onward), so this
    class must not assign to it itself.
    """

    def __init__(self) -> None:
        self._editing_person_id: str | None = None
        #: name/sex/age_years/height_cm collected by add_person, held here
        #: (not yet persisted as a Person) until a weighing is captured.
        self._pending_profile: dict[str, Any] | None = None
        self._pending_armed_at: float | None = None

    @property
    def _coordinator(self):
        return self.hass.data[DOMAIN][self.config_entry.entry_id]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_person", "edit_person", "remove_person", "settings"],
        )

    # ---- add person -----------------------------------------------------

    async def async_step_add_person(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Nothing is created yet - only armed. The profile is held on
            # this flow instance and only turned into a real Person once a
            # weighing is actually captured (see async_step_add_person_done
            # / coordinator.async_complete_pending_capture).
            self._pending_profile = dict(user_input)
            self._pending_armed_at = time.time()
            await self._coordinator.async_arm_registration(None)
            return await self.async_step_add_person_done()

        schema = vol.Schema(
            {
                vol.Required("name"): str,
                vol.Required("sex", default="male"): vol.In(["male", "female"]),
                vol.Required("age_years"): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
                vol.Required("height_cm"): vol.All(vol.Coerce(float), vol.Range(min=50, max=250)),
            }
        )
        return self.async_show_form(step_id="add_person", data_schema=schema)

    async def async_step_add_person_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Hold the dialog open until a stable weighing is actually captured.

        Submitting this form doesn't close it by itself - each submission
        (and the initial display) re-checks whether the armed, anonymous
        capture (see coordinator.pending_capture_session) has landed yet.
        Only once it has does this create the person - not before, so
        nothing gets saved for a name typed in but never actually weighed.
        If the window expires with nothing captured, this becomes an error
        dialog (`async_abort`) instead of a silent success.
        """
        assert self._pending_profile is not None
        name = self._pending_profile["name"]
        coordinator = self._coordinator

        if coordinator.pending_capture_session is not None:
            await coordinator.async_complete_pending_capture(
                name=name,
                sex=self._pending_profile["sex"],
                age_years=self._pending_profile["age_years"],
                height_cm=self._pending_profile["height_cm"],
            )
            self._pending_profile = None
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        remaining = REGISTRATION_ARMING_SECONDS - (time.time() - self._pending_armed_at)
        if remaining <= 0:
            self._pending_profile = None
            await coordinator.async_cancel_pending_registration()
            return self.async_abort(
                reason="registration_timed_out",
                description_placeholders={"name": name},
            )

        errors: dict[str, str] = {"base": "not_yet_weighed"} if user_input is not None else {}
        return self.async_show_form(
            step_id="add_person_done",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "name": name,
                "seconds_left": str(max(0, int(remaining))),
                "live_reading": _format_live_reading(coordinator.live_reading),
            },
        )

    # ---- edit person ------------------------------------------------------

    async def async_step_edit_person(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        people = self._coordinator.people
        if not people:
            return self.async_abort(reason="no_people")

        if user_input is not None:
            self._editing_person_id = user_input["person_id"]
            return await self.async_step_edit_person_details()

        schema = vol.Schema({vol.Required("person_id"): vol.In({pid: p.name for pid, p in people.items()})})
        return self.async_show_form(step_id="edit_person", data_schema=schema)

    async def async_step_edit_person_details(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        person = self._coordinator.people[self._editing_person_id]

        if user_input is not None:
            person.name = user_input["name"]
            person.sex = user_input["sex"]
            person.age_years = user_input["age_years"]
            person.height_cm = user_input["height_cm"]
            await self._coordinator.async_update_person(person)
            self._editing_person_id = None
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        schema = vol.Schema(
            {
                vol.Required("name", default=person.name): str,
                vol.Required("sex", default=person.sex): vol.In(["male", "female"]),
                vol.Required("age_years", default=person.age_years): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=120)
                ),
                vol.Required("height_cm", default=person.height_cm): vol.All(
                    vol.Coerce(float), vol.Range(min=50, max=250)
                ),
            }
        )
        return self.async_show_form(
            step_id="edit_person_details", data_schema=schema, description_placeholders={"name": person.name}
        )

    # ---- remove person -----------------------------------------------

    async def async_step_remove_person(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        people = self._coordinator.people
        if not people:
            return self.async_abort(reason="no_people")

        if user_input is not None:
            await self._coordinator.async_remove_person(user_input["person_id"])
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        schema = vol.Schema({vol.Required("person_id"): vol.In({pid: p.name for pid, p in people.items()})})
        return self.async_show_form(step_id="remove_person", data_schema=schema)

    # ---- settings -----------------------------------------------------

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        options = dict(self.config_entry.options)

        if user_input is not None:
            user_input[CONF_SCALE_MAC] = user_input[CONF_SCALE_MAC].strip().upper()
            return self.async_create_entry(title="", data={**options, **user_input})

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCALE_MAC, default=options.get(CONF_SCALE_MAC, self.config_entry.data[CONF_SCALE_MAC])
                ): str,
                vol.Required(
                    CONF_BODY_FAT_FORMULA, default=options.get(CONF_BODY_FAT_FORMULA, DEFAULT_BODY_FAT_FORMULA)
                ): vol.In(BODY_FAT_FORMULAS),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
