# Cottonwoods Snow Feed: User Manual (v1)

An automated JSON feed of computer-model snow and precipitation forecasts for the four Cottonwood Canyon resorts
(Alta, Snowbird, Solitude, Brighton). It updates itself every 3 hours. There is nothing to install, no key, and no
account: you fetch three public URLs.

> Experimental, automated model guidance. **Not an official forecast.** Check the National Weather Service and the
> Utah Avalanche Center before heading out. (The same text is in `meta.json` → `disclaimer`.)

---

## 1. The endpoints

| URL | What it holds |
|---|---|
| `https://cabbagepatchkim.github.io/cwx-feed/v1/meta.json` | When the feed was built, the resort points, each model's newest run, warnings, units, credits |
| `https://cabbagepatchkim.github.io/cwx-feed/v1/storms.json` | Every upcoming or ongoing storm: its time window and each model's total for it, per resort |
| `https://cabbagepatchkim.github.io/cwx-feed/v1/daily.json` | Each model's newest run, day by day, per resort |

All three are plain `GET` requests returning `application/json`.

- **CORS is open** (`Access-Control-Allow-Origin: *`): browser JavaScript on any site can fetch them directly.
- **Caching:** responses carry `Cache-Control: max-age=600` (up to 10 minutes), plus `ETag` and `Last-Modified`.
  Send `If-None-Match` / `If-Modified-Since` and you get an empty `304 Not Modified` when nothing changed.
- **Sizes** are small (`storms.json` is about 15 KB), so fetch the whole file and filter on your side.
- Each file is replaced in one piece, so you never read a half-written file.

```js
const base = "https://cabbagepatchkim.github.io/cwx-feed/v1/";
const [meta, storms, daily] = await Promise.all(
  ["meta", "storms", "daily"].map(f => fetch(base + f + ".json").then(r => r.json()))
);
```

## 2. When it updates

- Model passes start around **:45 to :50 past 02, 05, 08, 11, 14, 17, 20 and 23 UTC** (every 3 hours). New files
  appear about **15 to 25 minutes later**.
- Fetching every **30 to 60 minutes** is plenty. Fetching more often only returns the same file.
- **To be notified instead of polling:** every update is one commit to the public repository. Subscribe a feed
  reader, webhook or build job to `https://github.com/CabbagePatchKim/cwx-feed/commits/main.atom`.
- **How fresh is it?** `generated` (in every file) is when the feed was built. `meta.json` → `models` lists each
  model's newest run, and `warnings` names any model whose recent runs are missing.

## 3. Conventions used everywhere

| Topic | Rule |
|---|---|
| **Times** | ISO 8601 in Mountain time **with the offset**, e.g. `2026-10-30T12:00:00-06:00`. MDT is `-06:00`, MST `-07:00`; the switch is handled for you. Parse with any ISO parser. |
| **Model runs** | Each run has `init` (its start time, ISO) and `cycle`, its everyday name, e.g. `"10/28 12z"` (date and hour in UTC). |
| **Units** | Water and snow in **inches**, snow levels in **feet** (`meta.json` → `units`). |
| **Missing** | `null`, never `0`. A `0` always means "the model says none". |
| **Resorts** | Keys `alta`, `snowbird`, `solitude`, `brighton`. Their coordinates and elevations are in `meta.json` → `points`. |
| **Rounding** | Water to 0.01 in, snow to 0.1 in, snow level to 100 ft. |

## 4. The models

| `model` | What it is | Runs (UTC) | Reaches |
|---|---|---|---|
| `HRRR` | NOAA High-Resolution Rapid Refresh, 3 km | 00, 06, 12, 18z | 48 h |
| `NAMNEST` | NOAA NAM 3 km nest | 00, 06, 12, 18z | 60 h |
| `NAM` | NOAA North American Mesoscale, 12 km | 00, 06, 12, 18z | 84 h |
| `RDPS` | Canadian regional model, 10 km | 00, 06, 12, 18z | 84 h |
| `GFS` | NOAA Global Forecast System, 0.25° | 00, 06, 12, 18z | shown to 7 days |
| `GDPS` | Canadian global model, 15 km | 00, 12z | shown to 7 days |
| `IFS` | ECMWF high-resolution global model, 0.25° | 00, 06, 12, 18z | shown to 7 days (06/18z runs: 6 days) |
| `GEFS` | NOAA global **ensemble**, 31 members | 00, 06, 12, 18z | shown to 10 days |
| `ENS` | ECMWF **ensemble**, 50 members | 00, 06, 12, 18z | shown to 10 days (06/18z runs: 6 days) |

**Deterministic models** (all except GEFS and ENS) give one answer: water and snow.
**Ensembles** run many slightly different versions. The feed reports how many of them produce measurable precip
(at least `meta.json` → `ensemble_wet_threshold_in`, 0.10 in of water) and their mean water. Ensembles carry no
snow amount.

**Lead limits:** deterministic amounts are shown to 168 h (7 days) after a run starts. Ensembles go to 240 h
(10 days), the range where "x of N members are wet" is the useful signal.

## 5. Amounts: the two kinds you'll see

Wherever a resort's amount appears (storms and daily), it is one of two objects, told apart by `type`:

```json
{ "type": "deterministic", "water_in": 2.07, "snow_in": 21.1, "snow_in_regime": null }
{ "type": "ensemble", "members_wet": 30, "members": 31, "mean_water_in": 0.75 }
```

| Field | Meaning |
|---|---|
| `water_in` | Liquid water (rain and melted snow), inches |
| `snow_in` | Snowfall, inches: the model's water times the Kuchera snow ratio, worked out step by step at the resort's real elevation, with rain/mix/snow decided by the wet-bulb temperature. For the coarse models (GFS, ECMWF, GDPS and the ECMWF ensemble) each resort uses a blend of the four model grid points around it (since v1.4), so nearby resorts differ mainly by elevation |
| `snow_in_regime` | **Always `null` since v1.4.** It was the same snow scaled by how that resort usually does in the storm's wind direction and temperature; that adjustment is now kept internal until it has been verified. The key stays so existing code keeps working. Use `snow_in` |
| `members_wet` / `members` | Ensemble members with at least 0.10 in of water, out of how many |
| `mean_water_in` | The ensemble members' average water, inches |

`snow_in` is not verified yet. The amount object itself can be `null`
(see the storms and daily sections for when).

## 6. `meta.json`

| Field | Meaning |
|---|---|
| `schema_version` | `1` for everything under `/v1/` |
| `generated` | When this feed was built |
| `code_version` | The forecast program's release that built it, e.g. `"v1.3"` |
| `timezone` | `"America/Denver"` |
| `units` | `{"water": "in", "snow": "in", "snow_level": "ft"}` |
| `ensemble_wet_threshold_in` | What counts as a "wet" ensemble member: `0.1` |
| `snow_method`, `snow_regime_method` | One-sentence descriptions of `snow_in` and `snow_in_regime` |
| `points` | `{ "alta": {"lat", "lon", "elev_ft"}, ... }`: where and at what elevation each resort is forecast |
| `models` | `[ {"model", "cycle", "init"}, ... ]`: the newest run of each model in this feed |
| `warnings` | Text lines for models whose recent runs are missing (empty list when all are current) |
| `disclaimer` | Show this wherever the data is shown |
| `credits` | Data-source credits; see section 10 |

## 7. `storms.json`

A **storm** is a stretch of precipitation the models agree on, ending once there is a 12-hour dry gap. Its window
comes from the model run that shows the longest event. Storms appear while they're upcoming or ongoing, and leave
the list as soon as their forecast window ends (since v1.4; before, they stayed until observations confirmed them
over, about 12 dry hours later).

Top level:

| Field | Meaning |
|---|---|
| `schema_version`, `generated` | As in meta |
| `snow_regime_adjusted` | `true` when `snow_in_regime` values are available (always `false` since v1.4) |
| `storms` | The storms, earliest first (below) |
| `recent_changes` | Storms dropped or merged in the last day (below) |

Each entry in `storms`:

| Field | Meaning |
|---|---|
| `id` | Stable identifier for the storm, e.g. `"2026103109"`. Treat it as an opaque string; use it to follow a storm across updates |
| `start`, `end` | The storm's window |
| `hours` | Its length |
| `window_from` | `{"model", "cycle", "init"}`: the run whose timing defines the window |
| `notes` | `[ {"label", "text"}, ... ]`: short descriptive lines (below) |
| `models` | Each model's newest run that reaches the storm, totalled over the window (below) |

Each entry in a storm's `models`:

| Field | Meaning |
|---|---|
| `model`, `cycle`, `init` | Which run |
| `covers_full_storm` | `false` when the run ends before the storm does, or starts after the storm began |
| `run_ends` | How far that run reaches |
| `snow_level_ft` | `{"start", "end"}`: snow level at the start and end of the storm, or `null` (ensembles) |
| `flow` | Wind-regime text for the storm, e.g. `"SW warm -1C 68% -> N mild -5C 30%"`, or `null` |
| `modes` | Precipitation-type text, e.g. `"frontal 79% -> post-frontal 21%"`, or `null` |
| `points` | `{ "alta": <amount>, ... }` per section 5 |

**Why an amount can be `null` here:** when `covers_full_storm` is `false`, every resort's amount is `null`. Its
partial total isn't a forecast for the whole storm, and a number would read like one; `run_ends` shows how far it
got. This includes runs made after a storm began: a storm stays listed until observations confirm it's over, and a
run that started after it ended would otherwise report 0 for it. An amount is also `null` for a run made before a resort's point existed.

**Notes labels** (`notes[].label`): `"flow (MODEL)"` and `"modes (MODEL)"` describe the storm as the defining run
sees it; `"parts (MODEL)"` lists separate pieces of a storm with dry gaps between them; `"models disagree"` says
which models see it as separate events; `"note"` covers other remarks such as `"split from 2026103109"` or another
model starting or ending earlier.

**Reading `flow` and `modes`:** phases are joined by `->`, each with its share of the storm's water.
- Flow phase = 700 mb wind direction (`SW`, `W`, `NW`, `N`, or `S/E` for anything else), a temperature band
  (`warm` above -4 °C, `mild` -4 to -10 °C, `cold` below -10 °C), the 700 mb temperature, and the share.
- Modes: `frontal`, `post-frontal`, `cellular` (showery), `lake-effect`.

Each entry in `recent_changes`, so a storm that vanished doesn't disappear silently:

| Field | Meaning |
|---|---|
| `id`, `start` | The storm |
| `status` | `"dropped"` (no model shows it any more, or it moved past the 7-day limit) or `"merged"` (joined into another storm) |
| `note` | Why, in words |

## 8. `daily.json`

Every model's newest run, day by day, for each resort.

```json
{ "schema_version": 1, "generated": "...", "points": {
    "alta": [ { "model": "HRRR", "cycle": "10/28 12z", "init": "...", "ensemble": false,
                "days": [ { "start": "2026-10-28T06:00:00-06:00", "end": "2026-10-29T06:00:00-06:00",
                            "amounts": { "type": "deterministic", "water_in": 0.0, "snow_in": 0.0, "snow_in_regime": 0.0 } },
                          ... ] },
              ... ],
    "snowbird": [ ... ], "solitude": [ ... ], "brighton": [ ... ] } }
```

- **A day** is the 24 hours ending at **6:00 am Mountain** (05:00 during standard time, since it follows 12:00 UTC),
  the same day a morning snow report covers. There are 10 days, starting with the next one to end.
- `ensemble` is `true` for GEFS and ENS.
- **`amounts` is `null`** when the run doesn't cover that whole day (it starts after the day began, or ends
  before it), or when the day is past the lead limit (7 days deterministic, 10 days ensembles).
- Models are listed short-range first: HRRR, NAMNEST, NAM, RDPS, GFS, GDPS, IFS, GEFS, ENS.

## 9. Stability promise

- Everything under `/v1/` keeps its meaning. **Fields may be added; none will be renamed, removed or changed in
  meaning.** Ignore fields you don't recognize.
- A breaking change would be published as `/v2/` **alongside** `/v1/`, so nothing breaks without warning.
- Only owner-approved releases of the forecast program can publish; development code never reaches the feed.
- v1 contains **model forecasts only**: no observations or verification scores.

### Changes by version

Each published version puts its own copy of this manual here, so this list is current for the feed you're reading.

- **v1.4:** `snow_in` is plain Kuchera snow and `snow_in_regime` is always `null`; GFS, ECMWF, GDPS and the ECMWF
  ensemble blend the 4 grid points around each resort (higher resorts no longer get less snow from landing in a
  different grid cell); a storm leaves `storms.json` once its window ends; runs that start after a storm began have
  `null` amounts and `covers_full_storm: false`; GEFS is not blended.
- **v1.5:** `flow` text corrected for HRRR, NAM Nest, NAM and RDPS runs. Their 700 mb winds were read 7-18
  degrees off true north, so a wind near a boundary could carry the wrong direction label (`SW` instead of `W`).
  A run that lost a few values to a download timeout gets them back from the model's own files a pass or two later.
  Snow amounts can then change, usually by a few tenths of an inch (a filled temperature changes the snow ratio or
  rain/snow split), and water changes only if a precipitation value itself was missing. The new numbers are the
  model's own. No fields were added, removed or renamed. This manual is now published with every version as the
  README of the feed's repository.

## 10. Credits and licences

If you show the data, show the disclaimer and credit the sources. `meta.json` → `credits` lists them:

- NOAA/NCEP (HRRR, NAM, GFS, GEFS) via NOAA Open Data Dissemination
- ECMWF open data (IFS, ENS), CC BY 4.0
- Environment and Climate Change Canada / MSC Datamart (RDPS, GDPS), Open Government Licence - Canada
- NCAR GDEX GFS archive, CC BY 4.0, doi:10.5065/D65D8PWK

## 11. Quick recipes

**"How much snow at Alta this storm, by model?"**
```js
const s = storms.storms[0];
for (const m of s.models) {
  const a = m.points.alta;
  if (!a) continue;                                   // run doesn't cover the whole storm
  console.log(m.model, m.cycle,
    a.type === "deterministic" ? `${a.water_in}" water, ${a.snow_in}" snow`
                               : `${a.members_wet}/${a.members} members wet, mean ${a.mean_water_in}"`);
}
```

**"Is the feed stale?"**
```js
const ageHours = (Date.now() - Date.parse(meta.generated)) / 3.6e6;
if (ageHours > 4 || meta.warnings.length) showBanner(meta.warnings);
```

**"Tomorrow morning's report at Brighton, per model"**
```js
for (const run of daily.points.brighton) {
  const day = run.days[0];                            // the next 24 h ending 6 am
  console.log(run.model, day.end, day.amounts);       // null = not covered
}
```
