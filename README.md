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

### Starting fresh / clearing history

Two different buttons handle two different meanings of "start over" for a person:

- `button.okok_scale_<person>_reset_baseline` - recalibrates "100%" from their *existing* recent
  history. Nothing is deleted; see "How the baseline works" above.
- `button.okok_scale_<person>_clear_history` - the more drastic option: **permanently deletes**
  their CSV file and resets `ref_weight_kg`/`ref_impedance` and both baselines/rolling histories to
  blank, as if they had never weighed in. Their registration (name/sex/age/height) and entities are
  untouched - this is not the same as "Remove a person," which deletes the person entirely. There's
  no confirmation dialog and no undo; the CSV file is gone for good. See
  `coordinator.async_clear_history` / `csv_logger.delete_csv`.

Neither button touches Home Assistant's own recorder history (the graphs/statistics behind the
entities) - that's a separate system; purge it via Developer Tools -> Statistics if you want those
clean too.

## `sensor.okok_scale_last_measurement`

Shows the name of whoever was most recently weighed, with the full measurement (weight, absolute
and relative body fat, impedance, timestamp, person id) as attributes. It blanks itself
(`unknown`) 10 minutes after the last weighing (`LAST_MEASUREMENT_TIMEOUT_SECONDS`), and the timer
resets on every new weighing.

## Body composition: weight and body fat, absolute and relative to a personal baseline

Body fat is shown both **relative to its own baseline** (100% = baseline) and as a **plain
absolute number** - see "Absolute body fat: still uncalibrated" below for why relative is still
the more trustworthy of the two. An earlier version of this integration exposed BMI, lean mass,
body water, absolute body fat, and raw impedance as first-class sensors/CSV fields, sourced from
openScale's BMI/age/sex formulas; those were all removed in favour of just the one honestly-framed
relative number. Body water was later reintroduced on a more defensible footing (see "Body water
(BIA)" below), and absolute body fat was reintroduced too - at explicit request - alongside the
relative sensor rather than instead of it.

**Absolute body fat: still uncalibrated**: none of the available body-*fat* formulas actually
consume the scale's bio-impedance reading - they're the BMI/age/sex estimation formulas published
on the [openScale wiki's "Body metric
estimations"](https://github.com/oliexdev/openScale/wiki/Bodymetric-estimation-formulas) page,
which is what openScale itself uses for scales like this one that don't document a calibrated
impedance regression. So the *absolute* number from any of these formulas isn't trustworthy on its
own - `sensor.okok_scale_<person>_body_fat` carries a `formula` attribute naming which of the four
BMI-based formulas produced it, precisely because it's a formula-dependent estimate, not a
measurement. Expressed *relative to a personal baseline*, though, a formula's systematic bias
mostly cancels out (it's applied consistently to every reading for that person), leaving something
much more meaningful: "am I trending up or down from where I started" - `body_fat_relative` is
still the one to trust for that. (Body *water* is a different story - see "Body water (BIA)" below
- since that one does consume the scale's impedance reading, so its absolute value doesn't carry
the same caveat.)

**How the baseline works**: the same mechanism applies independently to both body fat and body
water (see "Body water (BIA)" below) - each has its own rolling history and its own baseline,
described here in terms of body fat.
- Every completed weighing computes an absolute, uncalibrated body-fat% (formula selectable in
  Configure -> Settings -> `body_fat_formula`; same four options as before, now purely an internal
  input rather than a displayed value) and, separately, a body-water% (see below).
- Each person keeps a rolling window of their `BASELINE_MEASUREMENT_COUNT` (5) most recent
  readings of each metric. These two histories can drift out of sync: a session whose final frame
  never locked has a body-fat% (BMI-based, doesn't need impedance) but no body-water% (does).
- **The first time a given window fills up** (that metric's 5th-ever reading), its average becomes
  that metric's baseline - the fixed 100% reference point. Until then, the corresponding relative
  sensor reads `unknown`: there's nothing meaningful to show relative to a baseline that doesn't
  exist yet.
- Each baseline then stays fixed - it does **not** silently drift with every new weighing - until
  you explicitly reset it.
- `button.okok_scale_<person>_reset_baseline` sets a new 100% reference point for **both** metrics
  at once, from whatever's currently in each rolling window (the most recent 5 readings of each,
  or fewer if they don't have 5 yet). Use this any time you want "100%" to mean "right now" for
  both - e.g. at the start of a new fitness phase.
- A subsequent reading's relative percentage is `absolute_pct / baseline_pct * 100` - so 105%
  means "5% higher (relative) than baseline", not "5 percentage points".

All of this lives in
[`custom_components/okok_scale/body_composition.py`](custom_components/okok_scale/body_composition.py)
(the pure formulas + `calc_baseline_body_fat_pct`/`calc_relative_body_fat_pct` and their
`_body_water_` counterparts) and
[`coordinator.py`](custom_components/okok_scale/coordinator.py) (the rolling-history/baseline
bookkeeping), unit-tested in
[`tests/test_body_composition.py`](tests/test_body_composition.py) and
[`tests/test_session_engine.py`](tests/test_session_engine.py).

## Body water (BIA)

Unlike body fat, total-body-water *does* use the scale's impedance reading, via `calc_body_water_pct`
in `body_composition.py`: Sun et al. 2003's bioelectrical-impedance regression (`1.2 + 0.45 *
height_cm^2/resistance_ohms + 0.18 * weight_kg`, gender-specific coefficients), the same formula
openScale's own `StandardImpedanceLib.kt` uses for scales like this one that don't publish a
calibrated regression of their own.

That formula needs true resistance in ohms, and the scale's raw impedance reading isn't that -
it's 10x too high (a real captured reading of 61.90 kg decodes to raw impedance 6000, but
openScale documents ~500+-100 ohm as normal for a foot-to-foot scale; 6000 ohms plugged in
directly gives a non-physical ~24% water estimate, while 600 ohms - raw / 10 - gives a plausible
~58%). `calc_resistance_ohms` (`IMPEDANCE_RAW_UNITS_PER_OHM` in `const.py`) does that conversion.

Sun 2003 is a well-validated *general-population* regression (roughly 3-5% of body weight standard
error against a 4-compartment reference method in the original study), not a regression calibrated
for this specific scale's electrodes - so, same as body fat, treat the absolute percentage as a
good average-case estimate and trust changes over time under consistent measurement conditions
(not right after a workout or a big meal) more than any single reading. Unlike body fat, the
*absolute* percentage is exposed as its own entity rather than only relative to a baseline, since
it's a genuine physical-quantity regression rather than a BMI proxy.

That said, a **relative-to-baseline** view exists for body water too -
`sensor.okok_scale_<person>_body_water_relative` - using the exact same baseline mechanism as body
fat (see "How the baseline works" above), just tracked independently. This is for spotting
day-to-day hydration swings against your own personal norm (e.g. "noticeably more dehydrated than
usual today") on top of the absolute figure, not a replacement for it - both entities exist side by
side, unlike body fat where only the relative one is shown.

`resistance_ohms`, `body_water_pct`, and `body_water_relative_pct` are logged to CSV for every
frame (see "CSV logging and downloads" below). `body_water_pct` is `sensor.okok_scale_<person>_
body_water`'s state (with `resistance_ohms` as its attribute); `body_water_relative_pct` is
`sensor.okok_scale_<person>_body_water_relative`'s state (with `baseline_body_water_pct` and
`absolute_body_water_pct` as attributes - same pattern as `body_fat_relative`).

## Entities

Per registered person (`<person>` = their slugified id):

- `sensor.okok_scale_<person>_weight` (kg) - also carries `csv_download_url`
- `sensor.okok_scale_<person>_body_fat` (%) - the raw BMI-based estimate (see "Absolute body fat:
  still uncalibrated" above); carries `formula` as an attribute
- `sensor.okok_scale_<person>_body_fat_relative` (%, 100% = baseline) - carries
  `baseline_body_fat_pct`, `absolute_body_fat_pct`, and `measurements_until_baseline` as attributes
- `sensor.okok_scale_<person>_body_water` (%) - the Sun 2003 BIA estimate (see "Body water (BIA)"
  above); carries `resistance_ohms` as an attribute
- `sensor.okok_scale_<person>_body_water_relative` (%, 100% = baseline) - carries
  `baseline_body_water_pct`, `absolute_body_water_pct`, and `measurements_until_baseline` as
  attributes (independent baseline from `body_fat_relative`'s - see "How the baseline works" above)
- `button.okok_scale_<person>_download_csv` - posts a persistent notification with the CSV link
- `button.okok_scale_<person>_arm_capture` - opens a fresh 120 s reference-capture window (see
  "Re-arming a person's capture window" above)
- `button.okok_scale_<person>_reset_baseline` - sets **both** their body-fat and body-water 100%
  reference points to the average of each metric's most recent 5 readings (see "How the baseline
  works" above)
- `button.okok_scale_<person>_clear_history` - **permanently deletes** their CSV file and resets
  reference weight/impedance and both baselines/histories to blank (see "Starting fresh / clearing
  history" above) - keeps their registration, unlike "Remove a person"

Integration-wide:

- `sensor.okok_scale_last_measurement`
- `select.okok_scale_reassign_last`

## CSV logging and downloads

Every frame of a session is appended (not just the final value), so each person's file is a
directly graphable settling-curve-plus-trend history:
`time,session_id,weight_kg,impedance,body_fat_pct,body_fat_relative_pct,resistance_ohms,body_water_pct,body_water_relative_pct`.
`body_fat_pct`/`body_water_pct` are the absolute estimates; `body_fat_relative_pct`/
`body_water_relative_pct` are those rows' values against whatever that metric's baseline was *at
the time the row was written* (unlike the live sensors, which always reflect the *current*
baseline - resetting a baseline doesn't rewrite CSV history). `resistance_ohms` is the BIA figure
described in "Body water (BIA)" above. New columns are always appended at the end, never inserted
in the middle - if a person's file already existed with an older, shorter header,
`csv_logger.append_row` migrates it in place (rewriting the header and backfilling the new columns
blank for old rows) the next time anything is appended, rather than silently misaligning columns.

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

**Fresh-install download bug (fixed)**: Home Assistant only actually wires up a static path's
directory listing if that directory already exists at the moment `async_register_static_paths` is
called (it silently skips creating the route otherwise), and that registration only happens once
per Home Assistant run. The `csv/` directory used to only get created lazily, on the first weighing
(`csv_logger.ensure_parent_dir`) - which on a fresh install is *after* startup already registered
the (then-missing) directory, so every download 404'd permanently, even once weighings started
arriving, until the next full Home Assistant restart. `__init__.py` now creates the directory
upfront, before registering the static path, so the route is always live. Regression-tested end to
end (actual HTTP fetch, not just checking the attribute is set) in
`tests/test_ha_integration.py::test_csv_download_url_actually_serves_the_file`.

All file I/O runs through `hass.async_add_executor_job`; the row read/write/delete/reassign logic
itself is plain, synchronous, path-based functions in `csv_logger.py` so it's unit-testable
without a Home Assistant runtime.

## Dashboard cards

Two cards, matching the two things there are to look at: today's numbers, and the trend.

**Current values card** - weight, body fat (absolute and relative), body water (absolute and
relative), and the reset-baseline button, per person. No custom code needed, just a stock entities
card (repeat per person, swapping the id; drop whichever entities you don't want):

```yaml
type: entities
title: Me
entities:
  - sensor.okok_scale_me_weight
  - sensor.okok_scale_me_body_fat
  - sensor.okok_scale_me_body_fat_relative
  - sensor.okok_scale_me_body_water
  - sensor.okok_scale_me_body_water_relative
  - button.okok_scale_me_reset_baseline
```

**History card** - the bundled custom card, showing weight and relative body fat as two stacked
line charts since the beginning of that person's measurements, with a person-tab switcher and a
30d/90d/1y/all range picker. It predates body water and doesn't chart it yet - use the entities
card above, or a stock `history-graph`/`statistics-graph` card pointed at
`sensor.okok_scale_<person>_body_water`, in the meantime:

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
      - sensor.okok_scale_me_body_water
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
reference value afterward). If you're reading old issues/commits referencing BMI/lean mass
sensors, a `match_tolerance_kg` option, or a person existing before their first weigh-in, that's
why - it hasn't been that way for a while.

Body water was later reintroduced on a different footing than the original BMI-based estimate:
once the scale's raw impedance reading was decoded to be 10x true resistance in ohms (see "Body
water (BIA)" above), a genuine bio-impedance regression (Sun et al. 2003, the same one openScale's
own `StandardImpedanceLib.kt` uses) became possible - unlike the BMI-only body-fat formulas, this
one actually consumes the measurement this scale exists to provide, so its absolute figure is shown
directly rather than only relative to a baseline. A relative-to-baseline view (`body_water_relative`)
was added alongside it shortly after, reusing body fat's existing baseline mechanism but tracked as
its own independent baseline - see "How the baseline works" above.

Absolute body fat itself was reintroduced still later, again at explicit request, as its own
`sensor.okok_scale_<person>_body_fat` alongside (not instead of) the relative sensor - unlike body
water's reintroduction, this one didn't come with new calibration: it's still the same BMI/age/sex
proxy described in "Absolute body fat: still uncalibrated" above, just no longer hidden from view.
If you're looking for why the reasoning above sounds like it's arguing against exposing the very
number that's now a sensor, that's why - the caveat is older than the sensor.

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
  the baseline/relative calculations for both body fat and body water, and the Sun 2003 body-water
  formula (raw-to-ohms conversion, gender-specific coefficients, clamping).
- `tests/test_session_engine.py` - registration-arming bypass, weight/impedance midpoint-interval
  matching (agreement, disagreement + weight×impedance tiebreak, unseeded fallback), CSV
  reassignment (row movement + recomputed refs + baseline-relative recompute), CSV schema migration
  (appending to a file still on the pre-body-water header), and CSV deletion (the "clear history"
  button's underlying `delete_csv`).

`tests/test_person_store.py` sits in between: its two functions under test
(`_person_to_dict`/`_person_from_dict`) are plain dict transforms with no Home Assistant runtime
needed, but `person_store.py` itself does real `homeassistant.core`/`homeassistant.helpers.storage`
imports (unlike the three pure modules above, which stay HA-import-free on purpose), so this file
needs `homeassistant` pip-installed - same as the integration tests below - even though it doesn't
need `pytest-homeassistant-custom-component`'s `hass` fixture. It's a regression test for a real
bug: `baseline_body_water_pct`/`recent_body_water_history` were added to `models.Person` but never
wired into serialization, so they silently reset to their defaults on every restart or config-entry
reload, even though the equivalent body-fat fields persisted fine.

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
