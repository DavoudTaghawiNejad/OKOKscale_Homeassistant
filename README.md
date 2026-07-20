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

A weighing session is considered "finished" - and gets logged/matched/reflected in the dashboard -
after a gap with no further frames. That gap is `SESSION_GAP_SECONDS` (60 s) while the reading
hasn't locked yet, but drops to `STABLE_SESSION_GAP_SECONDS` (3 s) the moment a locked (stable)
frame is seen, since waiting a full minute after the scale has already locked serves no purpose
beyond making everything feel unresponsive. This is what lets the "Add person" dialog (below) react
within a few seconds of someone stepping off, instead of up to a minute.

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
Submitting doesn't save anything yet - it arms a **120-second capture window**
(`REGISTRATION_ARMING_SECONDS` in `const.py`) and shows a "step on the scale now" dialog with a
best-effort live reading (refreshed each time you press Submit - Home Assistant dialogs can't push
live updates, so this is "as fresh as your last click", not truly real-time). The dialog **does
not close on its own**: pressing Submit before a stable (locked) reading has come in just
re-checks and re-shows the same form with "no weighing detected yet". Only once the scale actually
locks does the dialog close - and *that's* the moment the person's profile, reference weight, and
reference impedance are all created together. If the 120 s window runs out with nobody weighed in,
you get a proper error dialog and **nothing is saved at all** - no half-created person sitting
around.

(Internally: the capture window is armed "anonymously" - the coordinator holds the captured
weighing in `pending_capture_session` without assigning it to anyone - and the profile you typed
is held on the options-flow instance until the dialog itself turns both into a real person via
`coordinator.async_complete_pending_capture`. If you close the dialog before it captures anything,
and someone else happens to step on the scale within the remaining window, that stray weighing is
recovered via normal matching rather than silently lost - see `_async_recover_abandoned_capture`
in `coordinator.py`.)

We chose an options-flow dialog over a per-person "arm capture" button for the *initial*
registration (the brief's other allowed option) because it doesn't require entities to exist for a
person before they're created, and keeps the whole registration flow in one place. Re-arming an
*existing* person, on the other hand, does use a per-person button - see below.

**Edit a person** / **Remove a person** are also in the Configure menu. Removing a person deletes
their entities and registration but keeps their CSV file on disk.

## How a weighing gets assigned to a person

On every completed session:

1. If a registration is currently armed, the weighing goes to that pending person unconditionally,
   no matter how far off their eventual reference weight/impedance turns out to be.
2. Otherwise, **midpoint-interval matching**: every registered person who has both a reference
   weight and a reference impedance is sorted by weight, and the midpoint between each pair of
   consecutive people's weights becomes the boundary between their "territory" - the lowest
   person's territory extends down to zero, the highest's up to infinity, so every measurement
   lands in exactly one person's territory (there's no "too far to match" case). The same exercise
   is repeated independently sorted by impedance instead of weight.
3. If the weight-territory match and the impedance-territory match agree, that's the person.
4. If they **disagree**, the two disagreeing candidates are compared one more time, the same way,
   but using weight × impedance as the single combined axis between just the two of them.
5. If nobody has both a reference weight and impedance yet (a fresh household, nobody weighed in
   even once), the weighing goes to the first such not-yet-seeded person.

This replaces a simpler nearest-known-weight scheme from an earlier version of this integration;
the pure matching logic lives in
[`custom_components/okok_scale/assignment.py`](custom_components/okok_scale/assignment.py) and is
unit-tested in isolation in
[`tests/test_session_engine.py`](tests/test_session_engine.py).

**Known limitations**:
- Two people whose weight *and* impedance are both very close together can still get confused -
  the impedance axis and the weight×impedance tiebreak reduce this versus weight alone, but can't
  eliminate it entirely. That's what the reassign select is for (see below).
- A person who has no reference weight/impedance at all (never weighed, or missed their
  registration window) can't be auto-matched once anyone else in the household is registered with
  a reference - step 5 above only applies while *nobody* is seeded yet, so every one of their
  weighings would otherwise silently go to somebody else, not just the first one. Fix this with
  `button.okok_scale_<person>_arm_capture` (see below) - it opens the exact same 120-second capture
  window as registration, without needing to remove and re-add them.

### Re-arming a person's capture window

`button.okok_scale_<person>_arm_capture` opens a fresh 120-second unconditional-capture window for
that specific person, exactly like submitting "Add a person" does. Press it, then have them step
on the scale. This is the fix for the limitation above, and also works any time you want to
force-correct someone's reference (e.g. after a long gap, or a big genuine weight change) without
waiting for the matching algorithm to catch up on its own.

### Fixing a wrong guess

`select.okok_scale_reassign_last` lists every registered person plus `(no change)`. Picking a
name moves the most recent weighing session to them: it rewrites both people's CSVs (recomputing
body-composition fields for the *new* person, since those depend on height/age/sex), updates both
people's reference weights, refreshes every affected sensor, and then resets itself back to
`(no change)`. Reassignment requests older than one hour are ignored as stale.

## `sensor.okok_scale_last_measurement`

Shows the name of whoever was most recently weighed, with the full measurement (weight, absolute
and relative body fat, impedance, timestamp, person id) as attributes. It blanks itself
(`unknown`) 10 minutes after the last weighing (`LAST_MEASUREMENT_TIMEOUT_SECONDS`), and the timer
resets on every new weighing.

## Body composition: weight and body fat relative to a personal baseline

Only two numbers are shown per person: their **weight**, and their **body fat relative to their
own baseline**, where the baseline = 100%. Nothing else (BMI, lean mass, body water, absolute body
fat, raw impedance) is exposed as its own sensor or card field anymore - an earlier version of this
integration did expose all of those, sourced from openScale's BMI/age/sex formulas, but they were
removed in favour of just this one, more honestly-framed number.

**Why relative, not absolute**: none of the available body-fat formulas actually consume the
scale's bio-impedance reading - they're the BMI/age/sex estimation formulas published on the
[openScale wiki's "Body metric
estimations"](https://github.com/oliexdev/openScale/wiki/Bodymetric-estimation-formulas) page,
which is what openScale itself uses for scales like this one that don't document a calibrated
impedance regression. A genuine bio-impedance (BIA) body-fat model needs the raw resistance in
ohms plus a validated, device-specific regression (e.g. Kyle 2001, Sun 2003) and per-scale
calibration constants this scale doesn't publish - so the *absolute* number from any of these
formulas isn't trustworthy on its own. Expressed *relative to a personal baseline*, though, a
formula's systematic bias mostly cancels out (it's applied consistently to every reading for that
person), leaving something much more meaningful: "am I trending up or down from where I started."

**How the baseline works**:
- Every completed weighing computes an absolute, uncalibrated body-fat% (formula selectable in
  Configure -> Settings -> `body_fat_formula`; same four options as before, now purely an internal
  input rather than a displayed value).
- Each person keeps a rolling window of their `BASELINE_MEASUREMENT_COUNT` (5) most recent
  absolute body-fat% readings.
- **The first time that window fills up** (their 5th-ever weighing), its average becomes their
  baseline - the fixed 100% reference point. Until then, `sensor.okok_scale_<person>_body_fat_relative`
  reads `unknown`: there's nothing meaningful to show relative to a baseline that doesn't exist yet.
- The baseline then stays fixed - it does **not** silently drift with every new weighing - until
  you explicitly reset it.
- `button.okok_scale_<person>_reset_baseline` sets a new 100% reference point from whatever's
  currently in that rolling window (their most recent 5 readings, or fewer if they don't have 5
  yet). Use this any time you want "100%" to mean "right now" - e.g. at the start of a new fitness
  phase.
- A subsequent reading's relative percentage is `absolute_body_fat_pct / baseline_body_fat_pct *
  100` - so 105% means "5% higher (relative) body fat than baseline", not "5 percentage points".

All of this lives in
[`custom_components/okok_scale/body_composition.py`](custom_components/okok_scale/body_composition.py)
(the pure formulas + `calc_baseline_body_fat_pct`/`calc_relative_body_fat_pct`) and
[`coordinator.py`](custom_components/okok_scale/coordinator.py) (the rolling-history/baseline
bookkeeping), unit-tested in
[`tests/test_body_composition.py`](tests/test_body_composition.py) and
[`tests/test_session_engine.py`](tests/test_session_engine.py).

## Entities

Per registered person (`<person>` = their slugified id):

- `sensor.okok_scale_<person>_weight` (kg) - also carries `csv_download_url`
- `sensor.okok_scale_<person>_body_fat_relative` (%, 100% = baseline) - carries
  `baseline_body_fat_pct`, `absolute_body_fat_pct`, and `measurements_until_baseline` as attributes
- `button.okok_scale_<person>_download_csv` - posts a persistent notification with the CSV link
- `button.okok_scale_<person>_arm_capture` - opens a fresh 120 s reference-capture window (see
  "Re-arming a person's capture window" above)
- `button.okok_scale_<person>_reset_baseline` - sets their 100% reference point to the average of
  their most recent 5 weighings (see "Body composition" above)

Integration-wide:

- `sensor.okok_scale_last_measurement`
- `select.okok_scale_reassign_last`

## CSV logging and downloads

Every frame of a session is appended (not just the final value), so each person's file is a
directly graphable settling-curve-plus-trend history:
`time,session_id,weight_kg,impedance,body_fat_pct,body_fat_relative_pct`. `body_fat_pct` is the
absolute (uncalibrated) estimate; `body_fat_relative_pct` is that row's value against whatever the
person's baseline was *at the time the row was written* (unlike the live sensor, which always
reflects the *current* baseline - resetting the baseline doesn't rewrite CSV history).

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

## Dashboard cards

Two cards, matching the two things there are to look at: today's numbers, and the trend.

**Current values card** - weight, relative body fat, and the reset-baseline button, per person. No
custom code needed, just a stock entities card (repeat per person, swapping the id):

```yaml
type: entities
title: Me
entities:
  - sensor.okok_scale_me_weight
  - sensor.okok_scale_me_body_fat_relative
  - button.okok_scale_me_reset_baseline
```

**History card** - the bundled custom card, showing weight and relative body fat as two stacked
line charts since the beginning of that person's measurements, with a person-tab switcher and a
30d/90d/1y/all range picker:

```yaml
type: custom:okok-scale-card
# people:            # optional - auto-discovered from sensor.okok_scale_<id>_weight if omitted
#   - me
#   - wife
default_range: 30d    # 30d | 90d | 1y | all
```

It pulls history through Home Assistant's own history websocket API (no external service or CDN -
important on a Pi 3) and renders it as inline SVG - no charting library dependency. The relative
body-fat chart also draws a dashed line at 100% (the baseline) for reference.

Sample dashboard combining both, plus the reassign control:

```yaml
title: Body Scale
cards:
  - type: entities
    title: Me
    entities:
      - sensor.okok_scale_me_weight
      - sensor.okok_scale_me_body_fat_relative
      - button.okok_scale_me_reset_baseline
  - type: custom:okok-scale-card
    default_range: 90d
  - type: entities
    title: Fix a wrong guess
    entities:
      - select.okok_scale_reassign_last
      - sensor.okok_scale_last_measurement
```

## Diagnostics: confirming which build is running

The hub device's info panel (Settings -> Devices & Services -> OKOK Body Composition Scale -> the
device, not a person) shows a **software version** field set to the timestamp of the last `git
push` to this repo's `main` branch (`BUILD_TIMESTAMP` in `const.py`). After a HACS
redownload + restart, check that field to confirm you're actually running the build you think you
are.

## Deviations from the brief's suggested file layout

- **No `text.py`/`number.py`**: the brief made these conditional on "if using entities" for
  registration input. We collect name/sex/age/height as an options-flow form instead (see
  "Registering people" above), so there are no helper input entities to define.
- **Added `assignment.py`**: pure midpoint-interval matching/arming logic, split out of
  `coordinator.py` so the person-matching decision (section 3 of the brief) is unit-testable
  without a Home Assistant runtime, same as the parser and formulas.

## History of this integration

The original build brief specified openScale-style BMI/age/sex body-composition sensors (BMI, lean
mass, body water, absolute body fat) as first-class entities/CSV fields. Those were later removed
entirely in favour of the single baseline-relative body-fat number described above, at the user's
explicit request, once it became clear the absolute numbers weren't calibrated enough to be useful
on their own. The matching algorithm was also replaced along the way (nearest-known-weight +
tolerance -> weight/impedance midpoint intervals, also at the user's request), and the "Add person"
registration flow was reworked twice: first to keep the dialog open until a weighing is actually
captured (instead of closing immediately regardless of outcome), then to not create the person's
profile *at all* until that capture succeeds (instead of creating it up front and only capturing a
reference value afterward). If you're reading old issues/commits referencing BMI/lean
mass/body water sensors, a `match_tolerance_kg` option, or a person existing before their first
weigh-in, that's why - it hasn't been that way for a while.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/test_scale_parser.py tests/test_body_composition.py tests/test_session_engine.py -v
```

The three files above are pure - no Home Assistant install required. `tests/conftest.py` registers
lightweight placeholder package objects for `custom_components`/`custom_components.okok_scale` in
`sys.modules` (only when `homeassistant` isn't importable) before any test imports a submodule, so
`scale_parser.py`, `body_composition.py`, `assignment.py`, and `csv_logger.py`'s synchronous
helpers can be unit tested with plain `pytest`, even though they use the same intra-package
relative imports (`from .const import ...`) that the real integration uses inside Home Assistant.

- `tests/test_scale_parser.py` - frame validation, session dedup, the 60 s/3 s gap rules (a locked
  session finalizes much faster than an unlocked one - see "Hardware notes" above), and the
  reference session decode (61.90 kg / impedance 6000).
- `tests/test_body_composition.py` - all four body-fat formulas, clamping, divide-by-zero guards,
  and the baseline/relative-body-fat calculations.
- `tests/test_session_engine.py` - registration-arming bypass, weight/impedance midpoint-interval
  matching (agreement, disagreement + weight×impedance tiebreak, unseeded fallback), and CSV
  reassignment (row movement + recomputed refs + baseline-relative recompute).

### Real Home Assistant integration tests

`tests/test_ha_integration.py` runs the config flow, options flow, entity setup, and the full
weighing pipeline against an actual `homeassistant` core instance, via
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).
This is what caught several real bugs that plain import-checking missed, including:
`OptionsFlow.config_entry` becoming a read-only property in recent Home Assistant (assigning to it
in `__init__` used to work silently and now raises); `has_entity_name` + device-name auto-naming
producing entity IDs like `select.okok_body_composition_scale_reassign_last_measurement` instead of
the documented `select.okok_scale_reassign_last`; and a person who missed their registration window
becoming permanently unmatchable rather than just missing that one weighing, since the matching
algorithm has no "too far" fallback once anyone else is seeded (fixed by the
`button.okok_scale_<person>_arm_capture` re-arm button - see "Re-arming a person's capture window"
above). Also covers the "Add person" dialog's capture-before-create flow end to end: not-yet-locked
live readings, a genuine capture, the timeout error path, and an abandoned-dialog session getting
recovered via normal matching instead of silently lost.

```bash
.venv/bin/pip install pytest-homeassistant-custom-component dbus-fast
.venv/bin/python -m pytest tests/test_ha_integration.py -v
```

`pytest.ini` sets `pythonpath = .` and `asyncio_mode = auto` for these tests. On macOS (no
BlueZ/D-Bus), `_patch_bluetooth_adapter_history` in that file plugs a couple of real-D-Bus
codepaths the test plugin's own bluetooth mocking doesn't stub for the installed package versions
- none of that is needed on the actual target (HA Container on Linux with a real adapter).
