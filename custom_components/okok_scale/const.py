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
#: scale starts a new weighing session.
SESSION_GAP_SECONDS = 60

# --- Person registration ---------------------------------------------------

#: How long (seconds) a "register new person" arming window stays open
#: waiting for the next completed weighing session.
REGISTRATION_ARMING_SECONDS = 120

#: Default tolerance (kg) used to decide whether an incoming weight is
#: "close enough" to a known person's reference weight to be assigned to
#: them, versus being routed to a not-yet-seeded person.
DEFAULT_MATCH_TOLERANCE_KG = 2.5

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

# --- Options flow keys -------------------------------------------------

CONF_SCALE_MAC = "scale_mac"
CONF_MATCH_TOLERANCE_KG = "match_tolerance_kg"
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
CSV_FIELDNAMES = [
    "time",
    "session_id",
    "weight_kg",
    "impedance",
    "bmi",
    "body_fat_pct",
    "lean_mass_kg",
    "body_water_pct",
]
