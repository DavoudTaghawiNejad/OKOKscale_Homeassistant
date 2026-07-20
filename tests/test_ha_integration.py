"""Real Home Assistant integration tests.

Unlike the other test files, these run against an actual `homeassistant`
core instance (via pytest-homeassistant-custom-component) instead of
importing modules in isolation - they exercise the config flow, options
flow, entity setup, and the full weighing pipeline the way Home Assistant
itself actually drives them. This is what catches API-surface bugs (like
OptionsFlow.config_entry becoming a read-only property in recent HA) that
plain import-checking can't.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.okok_scale.const import CONF_SCALE_MAC, DOMAIN

ROOT = Path(__file__).resolve().parent.parent
REAL_CUSTOM_COMPONENTS = ROOT / "custom_components"

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def _patch_bluetooth_adapter_history():
    """Cover a real-D-Bus codepath the installed test plugin doesn't stub.

    `enable_bluetooth`'s own fixture setup (below) sets up a "bluetooth"
    MockConfigEntry, which triggers the real "bluetooth" component's
    manager.async_setup(), which calls into `bluetooth_adapters` to load
    adapter history. The plugin's session-scoped `mock_bluetooth_adapters`
    fixture patches `.adapters`/`.refresh` but not `.history` (version
    skew between the plugin and the installed `bluetooth_adapters`
    package), so it still hits real D-Bus and crashes on this macOS dev
    machine (no BlueZ/D-Bus here - the target Pi has real Bluetooth and
    doesn't need any of this). Must be autouse so it's active before
    `enable_bluetooth`'s own setup runs, not just around the test body.
    """
    with (
        patch(
            "bluetooth_adapters.systems.linux.LinuxAdapters.history",
            new_callable=PropertyMock,
            return_value={},
        ),
        patch(
            "homeassistant.components.bluetooth.manager.async_load_history_from_system",
            return_value=({}, {}),
        ),
    ):
        yield


@pytest.fixture
def okok_hass(hass, enable_custom_integrations, enable_bluetooth, tmp_path):
    """A test `hass` whose config_dir sees our real custom_components/.

    We symlink tmp_path/custom_components -> the real custom_components/
    directory so Home Assistant's loader discovers okok_scale from actual
    source on disk, while CSV/.storage writes land in the throwaway
    tmp_path instead of polluting the repo. `enable_bluetooth` mocks out
    the real bleak scanner / adapter discovery so the "bluetooth" /
    "bluetooth_adapters" dependencies our manifest declares don't try to
    talk to real hardware.
    """
    (tmp_path / "custom_components").symlink_to(REAL_CUSTOM_COMPONENTS, target_is_directory=True)
    hass.config.config_dir = str(tmp_path)
    return hass


TEST_MAC = "F0:2C:59:F1:F0:28"

# Reference frames (same hex as tests/test_scale_parser.py's captured session).
F_6190_LOCKED = (0x30C0, "182E17700a0125f02c59f1f028")  # 61.90 kg, impedance 6000
# Synthetic (not captured) 78.00 kg / impedance 5500 locked frame for a second person.
F_7800_LOCKED = (0x01C0, "1e78157c0a0125f02c59f1f028")
# Synthetic locked frames for the reassignment test: two people with close
# reference weights (62.5 and 61.9 kg - within match_tolerance_kg of each
# other), then a third, genuinely ambiguous 62.0 kg weighing that nearest-
# neighbour auto-assigns to the wrong one of the two.
F_6250_LOCKED = (0x02C0, "186a13240a0125f02c59f1f028")  # 62.50 kg, impedance 4900
F_6200_LOCKED = (0x03C0, "183813ec0a0125f02c59f1f028")  # 62.00 kg, impedance 5100


def _build_session(mfr_id: int, payload_hex: str, now: float):
    """Assemble a one-frame Session the same way the BLE pipeline would."""
    from custom_components.okok_scale.scale_parser import SessionAssembler

    assembler = SessionAssembler(TEST_MAC)
    assembler.ingest({mfr_id: bytes.fromhex(payload_hex)}, now)
    return assembler.check_timeout(now + 61)


@pytest.fixture
async def configured_entry(okok_hass):
    """A fully set-up config entry, returning (hass, entry)."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SCALE_MAC: TEST_MAC})
    entry.add_to_hass(okok_hass)
    assert await okok_hass.config_entries.async_setup(entry.entry_id)
    await okok_hass.async_block_till_done()
    return okok_hass, entry


def _coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]


async def test_entity_ids_match_the_documented_naming_scheme(okok_hass) -> None:
    """sensor.okok_scale_<person>_weight etc. must actually be the entity_id.

    The frontend card auto-discovers people by regex-matching
    `sensor.okok_scale_(.+)_weight`, and the README documents these exact
    entity_id patterns - if Home Assistant's has_entity_name-based
    auto-naming produces something else (e.g. derived from the device
    name instead), both break silently.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SCALE_MAC: TEST_MAC})
    entry.add_to_hass(okok_hass)

    assert await okok_hass.config_entries.async_setup(entry.entry_id)
    await okok_hass.async_block_till_done()

    all_ids = okok_hass.states.async_entity_ids()

    assert "sensor.okok_scale_last_measurement" in all_ids
    assert "select.okok_scale_reassign_last" in all_ids


async def test_config_flow_user_step_creates_entry(okok_hass) -> None:
    result = await okok_hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM

    result2 = await okok_hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SCALE_MAC: TEST_MAC}
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"][CONF_SCALE_MAC] == TEST_MAC


async def test_add_person_via_options_flow_creates_entities(configured_entry) -> None:
    """Drives the exact options-flow path that previously 500'd.

    Menu -> add_person form -> add_person_done confirmation, the same
    sequence a real user goes through in Configure -> Add a person.
    """
    hass, entry = configured_entry

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_person"}
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "add_person"

    result3 = await hass.config_entries.options.async_configure(
        result2["flow_id"],
        {"name": "Me", "sex": "male", "age_years": 40, "height_cm": 178},
    )
    assert result3["type"] == FlowResultType.FORM
    assert result3["step_id"] == "add_person_done"

    result4 = await hass.config_entries.options.async_configure(result3["flow_id"], {})
    assert result4["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    all_ids = hass.states.async_entity_ids()
    assert "sensor.okok_scale_me_weight" in all_ids
    assert "sensor.okok_scale_me_body_fat" in all_ids
    assert "button.okok_scale_me_download_csv" in all_ids

    coordinator = _coordinator(hass, entry)
    assert coordinator.store.pending_registration is not None
    assert coordinator.store.pending_registration["person_id"] == "me"


async def test_full_weighing_pipeline_assigns_and_updates_sensors(configured_entry) -> None:
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await coordinator.async_add_person(name="Wife", sex="female", age_years=38, height_cm=165)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    await coordinator.async_arm_registration("me")
    session = _build_session(*F_6190_LOCKED, now=1000.0)
    assert session is not None
    await coordinator._async_finish_session(session)
    await hass.async_block_till_done()

    weight_state = hass.states.get("sensor.okok_scale_me_weight")
    assert weight_state is not None
    assert float(weight_state.state) == pytest.approx(61.9)

    last_state = hass.states.get("sensor.okok_scale_last_measurement")
    assert last_state.state == "Me"
    assert last_state.attributes["impedance"] == 6000

    await coordinator.async_arm_registration("wife")
    session2 = _build_session(*F_7800_LOCKED, now=2000.0)
    assert session2 is not None
    await coordinator._async_finish_session(session2)
    await hass.async_block_till_done()

    wife_weight_state = hass.states.get("sensor.okok_scale_wife_weight")
    assert float(wife_weight_state.state) == pytest.approx(78.0)


async def test_reassign_select_moves_measurement_between_people(configured_entry) -> None:
    """The documented failure mode: two people within match_tolerance_kg
    of each other get confused, and the reassign select is how you fix it.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await coordinator.async_add_person(name="Wife", sex="female", age_years=38, height_cm=165)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    # Both people get an established reference weight, close together.
    await coordinator.async_arm_registration("me")
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))  # 61.9
    await coordinator.async_arm_registration("wife")
    await coordinator._async_finish_session(_build_session(*F_6250_LOCKED, now=2000.0))  # 62.5
    await hass.async_block_till_done()

    # An unarmed 62.0 kg weighing is nearer to "me" (0.1 kg) than "wife"
    # (0.5 kg) by nearest-neighbour, but it was actually the wife.
    await coordinator._async_finish_session(_build_session(*F_6200_LOCKED, now=3000.0))
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(62.0)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.okok_scale_reassign_last", "option": "Wife"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # "me" falls back to their last remaining (correct) row; "wife" gets the moved one.
    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(61.9)
    assert float(hass.states.get("sensor.okok_scale_wife_weight").state) == pytest.approx(62.0)
    assert hass.states.get("select.okok_scale_reassign_last").state == "(no change)"
