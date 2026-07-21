"""The OKOK Body Composition Scale integration.

Reads passive BLE broadcast weighings from a Chipsea-based body
composition scale, assigns each weighing to the right household member,
computes body composition, and exposes sensors + a per-person CSV export.
Everything is configured through the UI (config flow); there is no YAML.
"""

from __future__ import annotations

import logging

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATIC_CSV_URL_PATH
from .coordinator import OkokScaleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "select", "button"]

#: hass.data key (not entry-scoped) tracking whether the CSV static path
#: has been registered yet. It only needs registering once per HA runtime;
#: the same physical directory backs it regardless of how many times the
#: single config entry is reloaded, and aiohttp raises if you register the
#: same url_path twice.
_STATIC_PATH_REGISTERED_KEY = f"{DOMAIN}_static_path_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OKOK Scale from a config entry."""
    coordinator = OkokScaleCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if not hass.data.get(_STATIC_PATH_REGISTERED_KEY):
        # Home Assistant only serves a static path if the directory already
        # exists at *registration* time (it silently skips creating the
        # route otherwise - see homeassistant.components.http._make_static_
        # resources' os.path.isdir check) and this only ever runs once per
        # HA runtime. On a fresh install nothing has been logged yet, so
        # the csv/ directory doesn't exist until the first weighing - which
        # made every CSV download 404 forever, even after weighings started
        # arriving. Create it upfront so the route is always live.
        await hass.async_add_executor_job(lambda: coordinator.csv_dir.mkdir(parents=True, exist_ok=True))
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    url_path=STATIC_CSV_URL_PATH,
                    path=str(coordinator.csv_dir),
                    cache_headers=False,
                )
            ]
        )
        hass.data[_STATIC_PATH_REGISTERED_KEY] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: OkokScaleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_unload()
    return unload_ok
