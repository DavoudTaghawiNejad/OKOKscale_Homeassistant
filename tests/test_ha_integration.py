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
async def okok_hass(hass, enable_custom_integrations, enable_bluetooth, tmp_path):
    """A test `hass` whose config_dir sees our real custom_components/.

    We symlink tmp_path/custom_components -> the real custom_components/
    directory so Home Assistant's loader discovers okok_scale from actual
    source on disk, while CSV/.storage writes land in the throwaway
    tmp_path instead of polluting the repo. `enable_bluetooth` mocks out
    the real bleak scanner / adapter discovery so the "bluetooth" /
    "bluetooth_adapters" dependencies our manifest declares don't try to
    talk to real hardware. `persistent_notification` is always loaded in
    a real Home Assistant instance but not by this minimal test hass, and
    our download/arm-capture buttons call that service.
    """
    (tmp_path / "custom_components").symlink_to(REAL_CUSTOM_COMPONENTS, target_is_directory=True)
    hass.config.config_dir = str(tmp_path)
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "persistent_notification", {})
    return hass


TEST_MAC = "F0:2C:59:F1:F0:28"

# Reference frames (same hex as tests/test_scale_parser.py's captured session).
F_6190_LOCKED = (0x30C0, "182E17700a0125f02c59f1f028")  # 61.90 kg, impedance 6000
# Synthetic (not captured) 78.00 kg / impedance 5500 locked frame for a second person.
F_7800_LOCKED = (0x01C0, "1e78157c0a0125f02c59f1f028")
# Synthetic locked frames for the reassignment test: a well-separated second
# person (70.0 kg / 5000 ohm), then a third weighing (63.0 kg / 5900 ohm)
# that midpoint-interval matching genuinely assigns to "me" (both weight and
# impedance land in "me"'s territory - verified against the real match_person
# function, not just by construction) even though it was actually the wife.
F_7000_LOCKED = (0x02C0, "1b5813880a0125f02c59f1f028")  # 70.00 kg, impedance 5000
F_6300_LOCKED = (0x03C0, "189c170c0a0125f02c59f1f028")  # 63.00 kg, impedance 5900
# Synthetic 58.00 kg / impedance 4000 locked frame, well clear of "me"'s
# 61.9 kg / 6000 ohm reference - used to prove an unseeded second person's
# real weigh-in reaches them once they're armed (button.*_arm_capture).
F_5800_LOCKED = (0x04C0, "16a80fa00a0125f02c59f1f028")


def _build_session(mfr_id: int, payload_hex: str, now: float):
    """Assemble a one-frame Session the same way the BLE pipeline would."""
    from custom_components.okok_scale.scale_parser import SessionAssembler

    assembler = SessionAssembler(TEST_MAC)
    assembler.ingest({mfr_id: bytes.fromhex(payload_hex)}, now)
    return assembler.check_timeout(now + 61)


def _make_service_info(manufacturer_data: dict[int, bytes]):
    """A minimal, real BluetoothServiceInfoBleak for driving
    coordinator._async_handle_advertisement directly (rather than through
    HA's own scanner, which we don't have real hardware for in tests).
    """
    from bleak.backends.device import BLEDevice
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    device = BLEDevice(TEST_MAC, "OKOK", None)
    return BluetoothServiceInfoBleak(
        name="OKOK",
        address=TEST_MAC,
        rssi=-60,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        source="local",
        device=device,
        advertisement=None,
        connectable=False,
        time=0.0,
        tx_power=None,
        raw=None,
    )


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


async def test_add_person_creates_nothing_until_a_weighing_is_captured(configured_entry) -> None:
    """Drives the exact options-flow path that previously 500'd, and then
    got stuck (the originally-reported bug: submitting after actually
    stepping on the scale didn't close the dialog).

    Menu -> add_person form -> add_person_done, the same sequence a real
    user goes through in Configure -> Add a person. Under the current
    design, nothing is saved when the form is submitted - only once a
    weighing is actually captured does the person (and their entities)
    get created; see coordinator.async_complete_pending_capture.
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
    await hass.async_block_till_done()

    # Nothing is created yet - just an anonymous capture window armed.
    coordinator = _coordinator(hass, entry)
    assert "me" not in coordinator.people
    assert "sensor.okok_scale_me_weight" not in hass.states.async_entity_ids()
    assert coordinator.store.pending_registration is not None
    assert coordinator.store.pending_registration["person_id"] is None

    # Now they actually step on the scale and lock a reading.
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))
    await hass.async_block_till_done()
    assert coordinator.pending_capture_session is not None

    result4 = await hass.config_entries.options.async_configure(result3["flow_id"], {})
    assert result4["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # *Now* the person and their entities exist, seeded with the captured weighing.
    all_ids = hass.states.async_entity_ids()
    assert "sensor.okok_scale_me_weight" in all_ids
    assert "sensor.okok_scale_me_body_fat_relative" in all_ids
    assert "button.okok_scale_me_download_csv" in all_ids
    assert "button.okok_scale_me_arm_capture" in all_ids
    assert "button.okok_scale_me_reset_baseline" in all_ids
    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(61.9)

    coordinator = _coordinator(hass, entry)
    assert "me" in coordinator.people
    assert coordinator.people["me"].ref_weight_kg == pytest.approx(61.9)


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

    # The weight sensor also carries the BIA-derived figures for whoever's
    # data is currently attributed to it.
    me_weight_state = hass.states.get("sensor.okok_scale_me_weight")
    assert me_weight_state.attributes["resistance_ohms"] == pytest.approx(600.0)
    assert me_weight_state.attributes["body_water_pct"] == pytest.approx(58.0)


async def test_csv_directory_exists_before_any_weighing(configured_entry) -> None:
    """Previously, the CSV directory only got created on the first
    weighing (see csv_logger.ensure_parent_dir), but the static download
    path is only ever registered once, at integration startup - so on a
    fresh install the download route silently never got its resource
    (Home Assistant's static-path registration skips creating one if the
    directory doesn't exist yet at that moment), and every download 404'd
    forever even after weighings started arriving. __init__.py now creates
    the directory upfront so the route is always live.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)
    assert coordinator.csv_dir.is_dir()


async def test_csv_download_url_actually_serves_the_file(configured_entry, hass_client_no_auth) -> None:
    """End-to-end regression for the download-404 bug above: not just that
    csv_download_url is set (test_abandoned_capture_is_recovered_via_normal_
    matching already checks that), but that fetching it actually works and
    returns the row with the new resistance_ohms/body_water_pct columns.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    await coordinator.async_arm_registration("me")
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))
    await hass.async_block_till_done()

    client = await hass_client_no_auth()
    resp = await client.get(coordinator.csv_download_url("me"))
    assert resp.status == 200
    body = await resp.text()

    lines = body.strip().splitlines()
    header = lines[0].split(",")
    row = dict(zip(header, lines[1].split(",")))
    assert "resistance_ohms" in header
    assert "body_water_pct" in header
    assert row["weight_kg"] == "61.9"
    assert row["resistance_ohms"] == "600.0"
    assert row["body_water_pct"] == "58.0"


async def test_reassign_select_moves_measurement_between_people(configured_entry) -> None:
    """The documented failure mode: midpoint-interval matching lands a
    measurement in the wrong person's territory, and the reassign select
    is how you fix it.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await coordinator.async_add_person(name="Wife", sex="female", age_years=38, height_cm=165)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    # Both people get an established reference weight + impedance.
    await coordinator.async_arm_registration("me")
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))  # 61.9 kg / 6000 ohm
    await coordinator.async_arm_registration("wife")
    await coordinator._async_finish_session(_build_session(*F_7000_LOCKED, now=2000.0))  # 70.0 kg / 5000 ohm
    await hass.async_block_till_done()

    # An unarmed 63.0 kg / 5900 ohm weighing lands in "me"'s territory on
    # both axes (verified against match_person directly - see the frame
    # comments above), but it was actually the wife.
    await coordinator._async_finish_session(_build_session(*F_6300_LOCKED, now=3000.0))
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(63.0)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.okok_scale_reassign_last", "option": "Wife"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # "me" falls back to their last remaining (correct) row; "wife" gets the moved one.
    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(61.9)
    assert float(hass.states.get("sensor.okok_scale_wife_weight").state) == pytest.approx(63.0)
    assert hass.states.get("select.okok_scale_reassign_last").state == "(no change)"


async def test_remove_person_deletes_device_and_entities(configured_entry) -> None:
    """Removing a person must not leave an orphaned "unavailable" device.

    Dropping the person from our own store is not enough - Home Assistant
    only stops *re-creating* their entities on the next setup; it doesn't
    know to delete the ones already in the registry unless we explicitly
    remove the device (see coordinator.async_remove_person).
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    assert hass.states.get("sensor.okok_scale_me_weight") is not None
    assert entity_registry.async_get("sensor.okok_scale_me_weight") is not None
    device = device_registry.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_me")})
    assert device is not None

    await coordinator.async_remove_person("me")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.okok_scale_me_weight") is None
    assert entity_registry.async_get("sensor.okok_scale_me_weight") is None
    assert entity_registry.async_get("button.okok_scale_me_download_csv") is None
    assert device_registry.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_me")}) is None


async def _start_add_person_flow(hass, entry, name: str = "Me"):
    """Drive the options flow up to (and including) the first add_person_done show."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_person"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": name, "sex": "male", "age_years": 40, "height_cm": 178},
    )
    assert result["step_id"] == "add_person_done"
    return result


async def test_add_person_done_waits_for_weighing_before_closing(configured_entry) -> None:
    """The dialog must not close on submit until a weighing is actually captured."""
    hass, entry = configured_entry
    result = await _start_add_person_flow(hass, entry)

    # Submitting before anyone's stepped on the scale must NOT close the dialog.
    result2 = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result2["type"] == FlowResultType.FORM
    assert result2["step_id"] == "add_person_done"
    assert result2["errors"] == {"base": "not_yet_weighed"}

    # Now they actually step on the scale.
    coordinator = _coordinator(hass, entry)
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))
    await hass.async_block_till_done()

    result3 = await hass.config_entries.options.async_configure(result2["flow_id"], {})
    assert result3["type"] == FlowResultType.CREATE_ENTRY

    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(61.9)


async def test_add_person_done_times_out_with_error(configured_entry) -> None:
    """No weighing within the arming window -> an error dialog, not a silent close."""
    hass, entry = configured_entry
    base_time = 1_700_000_000.0

    with patch("custom_components.okok_scale.config_flow.time.time", return_value=base_time):
        result = await _start_add_person_flow(hass, entry)

    with patch("custom_components.okok_scale.config_flow.time.time", return_value=base_time + 200):
        result2 = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "registration_timed_out"

    # Nothing was ever created - the person only gets saved once a
    # weighing is actually captured, and none was.
    coordinator = _coordinator(hass, entry)
    assert "me" not in coordinator.people
    assert "sensor.okok_scale_me_weight" not in hass.states.async_entity_ids()
    assert coordinator.store.pending_registration is None


async def test_arm_capture_button_fixes_a_person_who_missed_registration(configured_entry) -> None:
    """Reproduces and fixes the reported bug: a person who missed their
    registration window isn't just unmatched for that one weighing - every
    subsequent real weigh-in of theirs silently goes to whoever's already
    seeded, because the matching algorithm has no fallback once anyone else
    has a reference. button.okok_scale_<person>_arm_capture is the fix.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await coordinator.async_add_person(name="Wife", sex="female", age_years=38, height_cm=165)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    # "Me" gets seeded; "Wife" never steps on (her registration arming, if
    # any, has already lapsed - we don't even bother arming her here).
    await coordinator.async_arm_registration("me")
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))
    await hass.async_block_till_done()
    assert coordinator.people["wife"].ref_weight_kg is None

    # Bug reproduction: wife's own real weigh-in (58 kg/4000 ohm, nowhere
    # near "me"'s 61.9 kg/6000 ohm) still gets silently assigned to "me",
    # since she has no interval of her own to fall into.
    await coordinator._async_finish_session(_build_session(*F_5800_LOCKED, now=2000.0))
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(58.0)
    assert hass.states.get("sensor.okok_scale_wife_weight").state in ("unknown", None)

    # Fix: press her arm-capture button, then she steps on for real.
    assert "button.okok_scale_wife_arm_capture" in hass.states.async_entity_ids()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.okok_scale_wife_arm_capture"}, blocking=True
    )
    await hass.async_block_till_done()
    assert coordinator.store.pending_registration is not None
    assert coordinator.store.pending_registration["person_id"] == "wife"

    await coordinator._async_finish_session(_build_session(*F_5800_LOCKED, now=3000.0))
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.okok_scale_wife_weight").state) == pytest.approx(58.0)
    assert coordinator.people["wife"].ref_weight_kg == pytest.approx(58.0)
    assert coordinator.people["wife"].ref_impedance == 4000

    # Now that she's seeded, her own weighings reach her correctly, unarmed.
    await coordinator._async_finish_session(_build_session(*F_5800_LOCKED, now=4000.0))
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.okok_scale_wife_weight").state) == pytest.approx(58.0)


async def test_add_person_dialog_shows_a_live_reading_before_it_locks(configured_entry) -> None:
    """Home Assistant dialogs can't get server-pushed updates, so this
    isn't truly live - but it must reflect the scale's current (still
    settling, not yet locked) reading as of the last time the dialog was
    checked, which is what "show the weight and impedance as it's
    submitted" means in a data_entry_flow form.
    """
    from homeassistant.components import bluetooth

    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    result = await _start_add_person_flow(hass, entry)
    assert "No reading yet" in result["description_placeholders"]["live_reading"]

    # An in-progress, unlocked frame arrives (62.10 kg, not yet the final
    # captured value) - not a completed session, just a live peek.
    info = _make_service_info({0x07C0: bytes.fromhex("184200000a0124f02c59f1f028")})
    coordinator._async_handle_advertisement(info, bluetooth.BluetoothChange.ADVERTISEMENT)

    result2 = await hass.config_entries.options.async_configure(result["flow_id"], {})
    live_text = result2["description_placeholders"]["live_reading"]
    assert "62.10" in live_text
    assert "settling" in live_text

    # Still nothing created - only a completed, locked session does that.
    assert "me" not in coordinator.people


async def test_abandoned_capture_is_recovered_via_normal_matching(configured_entry) -> None:
    """If the "Add person" dialog is closed after arming but before a
    weighing is captured, and someone else steps on the scale in the
    meantime, that weighing must not be silently swallowed - it gets
    recovered via normal matching the next time a capture would otherwise
    overwrite it. See coordinator._async_recover_abandoned_capture.
    """
    hass, entry = configured_entry
    coordinator = _coordinator(hass, entry)

    await coordinator.async_add_person(name="Me", sex="male", age_years=40, height_cm=178)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)

    await coordinator.async_arm_registration("me")
    await coordinator._async_finish_session(_build_session(*F_6190_LOCKED, now=1000.0))
    await hass.async_block_till_done()

    # First "Add person" dialog: armed, then abandoned (never completes).
    await coordinator.async_arm_registration(None)
    await coordinator._async_finish_session(_build_session(*F_7000_LOCKED, now=2000.0))
    assert coordinator.pending_capture_session is not None
    stray_session_id = coordinator.pending_capture_session.id

    # A second "Add person" dialog is opened and captures something else
    # before the first one was ever claimed.
    await coordinator.async_arm_registration(None)
    await coordinator._async_finish_session(_build_session(*F_5800_LOCKED, now=3000.0))
    await hass.async_block_till_done()

    # The stray 70.0 kg session was not lost - it reached "Me" (the only
    # seeded person) via normal matching instead of vanishing.
    assert float(hass.states.get("sensor.okok_scale_me_weight").state) == pytest.approx(70.0)
    assert hass.states.get("sensor.okok_scale_me_weight").attributes.get("csv_download_url") is not None

    # The second capture is still waiting to be claimed.
    assert coordinator.pending_capture_session is not None
    assert coordinator.pending_capture_session.id != stray_session_id
    assert coordinator.pending_capture_session.final_frame.weight_kg == pytest.approx(58.0)


async def test_hub_device_shows_build_timestamp_as_sw_version(configured_entry) -> None:
    """Settings -> Devices & Services -> the scale device -> a version
    field you can check after a HACS redownload + restart to confirm
    which build actually landed."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.okok_scale.const import BUILD_TIMESTAMP

    hass, entry = configured_entry
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.sw_version == BUILD_TIMESTAMP
