"""Constants for the OKOK Body Composition Scale integration."""

from __future__ import annotations

DOMAIN = "okok_scale"

# --- Bluetooth / protocol -------------------------------------------------

#: Low byte of the manufacturer id that identifies a Chipsea scale frame.
#: The high byte is a rolling packet counter and is therefore NOT matched.
CHIPSEA_MARKER_BYTE = 0xC0

#: Length in bytes of a valid scale payload.
PAYLOAD_LENGTH = 13

#: Default / factory MAC address of the scale, used to pre-fill the config
#: flow. The user can override it (and multiple scales could theoretically
#: share this firmware, hence "configurable").
DEFAULT_SCALE_MAC = "F0:2C:59:F1:F0:28"

#: Bit 0 of payload[6] signals a locked/stable measurement.
STABLE_FLAG_BIT = 0x01

#: A gap of more than this many seconds between two valid frames from the
#: scale starts a new weighing session. Used while the session hasn't
#: locked yet (no stable frame seen), so a slow-to-settle scale still gets
#: a generous window.
SESSION_GAP_SECONDS = 60

#: Once a session has seen a stable (locked) frame, a much shorter gap is
#: enough to consider it finished - waiting the full SESSION_GAP_SECONDS
#: after the reading has already locked serves no purpose except making
#: every dependent feature (the "add person" dialog, the last-measurement
#: sensor, CSV logging) feel unresponsive for up to a minute.
STABLE_SESSION_GAP_SECONDS = 3

# --- Person registration ---------------------------------------------------

#: How long (seconds) a "register new person" arming window stays open
#: waiting for the next completed weighing session.
REGISTRATION_ARMING_SECONDS = 120

#: How many of a person's most recent weighings make up their body-fat
#: baseline (both the automatic first-time baseline and what "reset
#: baseline" recomputes from).
BASELINE_MEASUREMENT_COUNT = 5

# --- "Last measurement" headline sensor ------------------------------------

#: How long (seconds) sensor.okok_scale_last_measurement keeps showing the
#: most recent weighing before blanking itself out again.
LAST_MEASUREMENT_TIMEOUT_SECONDS = 600

# --- Body composition --------------------------------------------------

FORMULA_DEURENBERG_1991 = "deurenberg1991"
FORMULA_DEURENBERG_1992 = "deurenberg1992"
FORMULA_EDDY_1976 = "eddy1976"
FORMULA_GALLAGHER_2000 = "gallagher2000"

BODY_FAT_FORMULAS = [
    FORMULA_DEURENBERG_1991,
    FORMULA_DEURENBERG_1992,
    FORMULA_EDDY_1976,
    FORMULA_GALLAGHER_2000,
]

DEFAULT_BODY_FAT_FORMULA = FORMULA_GALLAGHER_2000

#: Plausible body-fat percentage clamp range.
BODY_FAT_MIN_PCT = 3.0
BODY_FAT_MAX_PCT = 70.0

#: The scale's raw impedance reading is 10x true resistance in ohms -
#: confirmed against the real captured session (61.90 kg / raw impedance
#: 6000): plugging 6000 ohms directly into a BIA regression gives a
#: non-physical ~24% body-water estimate, while 600 ohms lands right in
#: the expected ~500+-100 ohm range for a foot-to-foot scale and produces a
#: plausible ~58% estimate. See body_composition.calc_resistance_ohms.
IMPEDANCE_RAW_UNITS_PER_OHM = 10

#: Plausible total-body-water percentage clamp range (defensive bounds,
#: not "normal" bounds - genuine physiological range is roughly 43-73%,
#: see body_composition.py).
BODY_WATER_MIN_PCT = 25.0
BODY_WATER_MAX_PCT = 75.0

# --- Options flow keys -------------------------------------------------

CONF_SCALE_MAC = "scale_mac"
CONF_BODY_FAT_FORMULA = "body_fat_formula"

# --- Storage -------------------------------------------------------------

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.people"

#: Per-person CSVs live at <config>/okok_scale/csv/<person_id>.csv, kept
#: out of config/www (which may not exist, and mixes user dashboards with
#: integration data) and instead served through a dedicated, cache-disabled
#: static path registered by __init__.py. See README "CSV downloads".
CSV_DIR_NAME = "okok_scale"
STATIC_CSV_URL_PATH = "/api/okok_scale/csv"

#: A reassignment request for a measurement older than this is ignored -
#: it's almost certainly not what the user meant to correct anymore.
REASSIGN_MAX_AGE_SECONDS = 3600

#: CSV column header, also used as the canonical row-dict key order.
#: impedance is the scale's raw (x10) unit; resistance_ohms is that value
#: converted via IMPEDANCE_RAW_UNITS_PER_OHM, i.e. what any BIA formula
#: actually needs. body_fat_pct is the absolute (unrelative-ized,
#: uncalibrated, impedance-blind - see body_composition.py) BMI-based
#: estimate; body_fat_relative_pct is that same value expressed against
#: the person's baseline (100% = baseline average). body_water_pct is the
#: Sun et al. 2003 BIA regression estimate, which does use resistance_ohms;
#: body_water_relative_pct is that value against the person's own water
#: baseline, same idea as body_fat_relative_pct but tracked independently
#: (see models.Person.baseline_body_water_pct). See body_composition.py
#: and coordinator.py for how each is derived.
#:
#: New columns are appended, never inserted in the middle - csv_logger.
#: append_row migrates any pre-existing file's header to match this list
#: (see _migrate_schema_if_needed), but appending here keeps that a no-op
#: for the common case of a file already on this exact schema.
CSV_FIELDNAMES = [
    "time",
    "session_id",
    "weight_kg",
    "impedance",
    "body_fat_pct",
    "body_fat_relative_pct",
    "resistance_ohms",
    "body_water_pct",
    "body_water_relative_pct",
]

# --- Diagnostics -----------------------------------------------------------

#: Shown as the hub device's software-version in its Home Assistant device
#: info panel, so you can confirm which build is actually running after an
#: update. Set to the timestamp of the last `git push` to main.
BUILD_TIMESTAMP = "2026-07-20T14:00:00Z"
