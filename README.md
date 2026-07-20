# OKOK Body Composition Scale

A Home Assistant custom integration (HACS-installable) for a nameless OKOK/Chipsea BLE
body-composition scale. It listens passively for the scale's BLE broadcasts (no pairing, no
connecting), figures out which household member just weighed in, computes body composition, and
keeps a downloadable CSV per person plus a custom Lovelace history card.

Everything is configured through the UI - there is no YAML to edit and no SSH required.

## Hardware notes

The scale only broadcasts while someone is standing on it (~15 s), advertising its weight (and,
once locked, bio-impedance) in manufacturer-data entries keyed by a rolling counter
(`0xNNC0` - `0xC0` is a constant Chipsea marker, `NN` rolls through the weighing). Each frame is
validated against the configured MAC and 13-byte payload length before being trusted. See
[`custom_components/okok_scale/scale_parser.py`](custom_components/okok_scale/scale_parser.py) for
the full protocol writeup and [`tests/test_scale_parser.py`](tests/test_scale_parser.py) for a
real captured session (which must decode to **61.90 kg / impedance 6000**).

## Install

1. In HACS, add this repository as a **custom repository** of type **Integration**.
2. Install "OKOK Body Composition Scale" and restart Home Assistant.
3. **Settings -> Devices & Services -> Add Integration -> OKOK Body Composition Scale.**
   Pick the discovered MAC if one shows up, or enter/paste it manually (default
   `F0:2C:59:F1:F0:28`).
4. Add the Lovelace card resource (Settings -> Dashboards -> Resources -> Add Resource):
   `/hacsfiles/okok_scale/okok-scale-card.js`, type **JavaScript Module** (HACS registers this
   automatically once the frontend module is recognised; add it manually if it doesn't show up).

## Registering people

Open the integration's **Configure** dialog and choose **Add a person**: name, sex, age, height.
Submitting arms a **120-second capture window** (`REGISTRATION_ARMING_SECONDS` in `const.py`) -
have that person step on the scale within that window and their very first weighing becomes their
reference weight, bypassing the usual nearest-weight matching entirely. If nobody steps on in
time, the person is still saved (with no reference weight yet) and gets matched automatically the
next time they're weighed, via the bootstrap rule below.

We chose an options-flow arming step over a per-person "arm capture" button (the brief's other
allowed option) because it doesn't require entities to exist for a person before they're created,
and keeps the whole registration flow in one place.

**Edit a person** / **Remove a person** are also in the Configure menu. Removing a person deletes
their entities and registration but keeps their CSV file on disk.

## How a weighing gets assigned to a person

On every completed session:

1. If a registration is currently armed, the weighing goes to that pending person, no matter how
   far off their eventual reference weight is.
2. Otherwise, nearest-neighbour matching against every person's last known weight
   (`ref_weight_kg`): the closest match wins.
3. **Bootstrap rule**: if the closest known reference is further away than the match tolerance
   (default **2.5 kg**, `match_tolerance_kg` in Configure -> Settings) *and* at least one person
   still has no reference weight at all, the weighing goes to that not-yet-seeded person instead.
   This is what lets a second household member get picked up automatically the first few times,
   before they have their own reference weight.
4. If nobody has a reference weight yet, the first not-yet-seeded person gets it.

**Known limitation**: two people whose weights are within `match_tolerance_kg` of each other can
get confused with one another. That's what the reassign select is for (see below) - lower the
tolerance if this happens often, at the cost of the bootstrap rule kicking in more eagerly for a
new person.

### Fixing a wrong guess

`select.okok_scale_reassign_last` lists every registered person plus `(no change)`. Picking a
name moves the most recent weighing session to them: it rewrites both people's CSVs (recomputing
body-composition fields for the *new* person, since those depend on height/age/sex), updates both
people's reference weights, refreshes every affected sensor, and then resets itself back to
`(no change)`. Reassignment requests older than one hour are ignored as stale.

## `sensor.okok_scale_last_measurement`

Shows the name of whoever was most recently weighed, with the full measurement (weight, body fat,
lean mass, body water, impedance, timestamp, person id) as attributes. It blanks itself
(`unknown`) 10 minutes after the last weighing (`LAST_MEASUREMENT_TIMEOUT_SECONDS`), and the timer
resets on every new weighing.

## Body composition - and its honesty caveat

**The scale's impedance reading is logged but not used by any of the body-fat estimates below.**
openScale's published body-metric formulas (which this integration also uses, since this hardware
doesn't document a calibrated impedance regression) are BMI/age/sex based - a genuine bio-impedance
(BIA) body-fat model needs the raw resistance in ohms plus a validated, device-specific regression
(e.g. Kyle 2001, Sun 2003) and per-scale calibration constants this scale doesn't publish. Raw
impedance is still recorded on every row (sensor + CSV) so a real BIA model can be dropped in later
without losing any data.

Pick the body-fat formula in Configure -> Settings (`body_fat_formula`):

| Formula | Notes |
|---|---|
| `deurenberg1991` (default) | Deurenberg et al. 1991 |
| `deurenberg1992` | Deurenberg et al. 1992, separate child (<16) formula |
| `eddy1976` | Eddy et al. 1976 |
| `gallagher2000` | Gallagher et al. 2000 (non-Asian) |

All four are sourced from the [openScale wiki's "Body metric
estimations"](https://github.com/oliexdev/openScale/wiki/Bodymetric-estimation-formulas) page.
Body-fat percentage is clamped to 3-70% and derived mass figures guard against divide-by-zero
(missing height, etc).

- `fat_mass_kg = weight_kg * body_fat_pct / 100`
- `lean_mass_kg = weight_kg - fat_mass_kg` (openScale convention)
- `body_water_pct` uses the **Hume (1966)** total-body-water formula, which is an independently
  well-established weight/height regression - also not impedance-based.

We deliberately do **not** expose a "muscle mass" sensor: openScale doesn't publish a muscle-mass
formula, and inventing a fraction-of-LBM constant without a citation would be worse than not
showing it. Lean body mass is exposed instead.

All formulas live in
[`custom_components/okok_scale/body_composition.py`](custom_components/okok_scale/body_composition.py)
and are unit-tested in isolation (no Home Assistant required) in
[`tests/test_body_composition.py`](tests/test_body_composition.py).

## Entities

Per registered person (`<person>` = their slugified id):

- `sensor.okok_scale_<person>_weight` (kg) - also carries `csv_download_url`
- `sensor.okok_scale_<person>_body_fat` (%)
- `sensor.okok_scale_<person>_lean_mass` (kg)
- `sensor.okok_scale_<person>_body_water` (%)
- `sensor.okok_scale_<person>_impedance` (raw, diagnostic)
- `sensor.okok_scale_<person>_bmi`
- `button.okok_scale_<person>_download_csv` - posts a persistent notification with the CSV link

Integration-wide:

- `sensor.okok_scale_last_measurement`
- `select.okok_scale_reassign_last`

## CSV logging and downloads

Every frame of a session is appended (not just the final value), so each person's file is a
directly graphable settling-curve-plus-trend history:
`time,session_id,weight_kg,impedance,bmi,body_fat_pct,lean_mass_kg,body_water_pct`.

Files live at `<config>/okok_scale/csv/<person_id>.csv` - deliberately **outside** `config/www`,
since that folder may not exist, mixes integration data into the user's own dashboard assets, and
its default `/local/` route is long-cached by the frontend. Instead, `__init__.py` registers a
dedicated, cache-disabled static path once per Home Assistant run:
`hass.http.async_register_static_paths([StaticPathConfig(url_path="/api/okok_scale/csv", ...)])`,
so a person's file is always fetchable at `/api/okok_scale/csv/<person_id>.csv`. This is the
"nicer, more robust for HA Container" option the brief called out, over writing into `www/`. Every
weight sensor carries this URL as the `csv_download_url` attribute, and each person also gets a
download button that posts a persistent notification with the same link - pick whichever suits
your dashboard.

All file I/O runs through `hass.async_add_executor_job`; the row read/write/delete/reassign logic
itself is plain, synchronous, path-based functions in `csv_logger.py` so it's unit-testable
without a Home Assistant runtime.

## The Lovelace card

```yaml
type: custom:okok-scale-card
# people:            # optional - auto-discovered from sensor.okok_scale_<id>_weight if omitted
#   - me
#   - wife
default_range: 30d    # 30d | 90d | 1y | all
```

Per selected person: a small inline-SVG weight-over-time line chart (pulled from Home Assistant's
own history websocket API, so it works without any external service or CDN - important on a Pi 3),
a 30d/90d/1y/all range picker, current-value tiles for body fat / lean mass / body water / BMI, and
a link to that person's full CSV.

Sample dashboard section combining the card with the reassign control:

```yaml
title: Body Scale
cards:
  - type: custom:okok-scale-card
    default_range: 90d
  - type: entities
    title: Fix a wrong guess
    entities:
      - select.okok_scale_reassign_last
      - sensor.okok_scale_last_measurement
```

## Deviations from the brief's suggested file layout

- **No `text.py`/`number.py`**: the brief made these conditional on "if using entities" for
  registration input. We collect name/sex/age/height as an options-flow form instead (see
  "Registering people" above), so there are no helper input entities to define.
- **Added `assignment.py`**: pure nearest-neighbour/arming logic, split out of `coordinator.py` so
  the person-matching decision (section 3 of the brief) is unit-testable without a Home Assistant
  runtime, same as the parser and formulas.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/pip install homeassistant   # optional: only needed to import the HA-dependent modules
                                       # (coordinator.py, config_flow.py, sensor.py, select.py,
                                       # button.py, __init__.py, person_store.py); the parser,
                                       # body-composition, assignment, and csv_logger tests never
                                       # import homeassistant at all.
.venv/bin/python -m pytest tests/ -v
```

`tests/conftest.py` registers lightweight placeholder package objects for
`custom_components`/`custom_components.okok_scale` in `sys.modules` before any test imports a
submodule, so `scale_parser.py`, `body_composition.py`, `assignment.py`, and `csv_logger.py`'s
synchronous helpers can be unit tested with plain `pytest` and no Home Assistant install at all,
even though they use the same intra-package relative imports (`from .const import ...`) that the
real integration uses inside Home Assistant.

- `tests/test_scale_parser.py` - frame validation, session dedup, the 60 s gap rule, and the
  reference session decode (61.90 kg / impedance 6000).
- `tests/test_body_composition.py` - all four body-fat formulas, BMI, lean mass, body water,
  clamping, and divide-by-zero guards.
- `tests/test_session_engine.py` - registration-arming bypass, nearest-neighbour + bootstrap
  matching, and CSV reassignment (row movement + recomputed refs).
