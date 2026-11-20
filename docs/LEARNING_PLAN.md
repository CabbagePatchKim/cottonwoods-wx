# Learning model plan ("CWX"), agreed with the owner 2026-11-19

The build plan for docs/PLAN.md step 6 (the learning layer). PLAN.md stays the short status page; this file is the
design. Nothing here is built yet except the measurement tools listed under "Already built".

## 0. First, before any model: is GFS really too dry at Alta?
The 2025-26 backfill says GFS gives ~45% of the observed water at Alta (SNOTEL Atwater gauge), IFS ~33%. The U of U
(Wasatch Weather Weenies, 2021) says the GFS "tends to run wet" at Alta and applies no downscaling. Check before
building corrections:
- our observed water: SNOTEL gauge vs Collins / NWS storm totals (and the pillow) for the same storms;
- our forecast water: blended 2x2 cells vs the raw nearest grid point ("GFS raw"), vs U of U's raw GFS (UUGFS);
- per storm, so one gauge problem or a few big storms can't drive it.
The answer decides what the correction targets. Do this first.

## 1. Rules
1. Water and snow ratio are separate models: snow = water x SLR, per time step (never on totals).
2. Inputs are forecast-time only: each run's own fields, its time-height features, its 500 mb patterns, the lead.
   Observed / analysis features (RAP, balloons) are for understanding errors, never inputs.
3. Season-out testing: fit on 3 of the 4 backfill winters (2022-26), test on the 4th, rotating; whole storms held
   out together (never single blocks: blocks of one storm are near-copies).
4. Small data, small models: few, physical predictors, strong shrinkage. A piece stays only if it beats the raw
   model (and the simpler version) on seasons it never saw.
5. Final check on untouched data: the live 2026-27 season, frozen forecasts, decides (see section 4).

## 2. The layers (v0)
1. WATER CORRECTION per model and point. Target: observed water per 6 h block (best estimate, section 3).
   Predictors (each run's own): its water; moist-layer depth, growth-zone saturation, convective flag; dry gap under
   the cloud (overrunning); cross-barrier wind, Froude, drift (spillover); flow direction + temperature band
   (regimes); lead; PVA / shortwave timing; cross-barrier moisture flux at crest level and IVT.
   Form: multiplier exp(a + b . features), global (model x lead) first, split by flow regime only where the data
   allow (partial pooling, like regimes.factor_for). Capped (e.g. x0.5-x2.5); the raw model always shown beside it.
   First look (2025-26): models driest when their OWN forecast has a shallow moist layer or an unsaturated growth zone.
2. TIMING: delay the onset / trim the first blocks when the forecast shows a deep dry gap under the cloud; each
   model's measured trough-timing bias by pattern (shortwave matching) shifts its steps (days 2-5).
3. BLEND WEIGHTS per model x lead x regime from out-of-sample error after correction, shrunk toward equal weights;
   model FAMILIES counted (GFS/GEFS, IFS/ENS, GDPS/GEPS) so related models don't vote twice. Expected: HRRR / RRFS /
   U of U lead 0-48 h, globals + ensembles 3-7 d; the data decides who wins in which flow.
4. SNOW RATIO, its own model. Target: observed snow / water (Collins 12 h boards 2000-23 from the Hive dataset,
   CC BY-NC: the main training set; Collins hourly this season; stake / NWS totals as checks). Trained on OBSERVED
   profiles (RAP / BUFKIT analyses) vs observed SLR, applied to each run's forecast profiles. Predictors: Kuchera's
   column max temperature, dendritic-zone temperature + saturation, crest-level wind (fragmentation), riming signs
   (warm cloud top, strong lift -10..0 C), convective / graupel, precip rate. Benchmarks: Kuchera, Veals et al. 2025.
5. RAIN / SNOW LINE: keep the wet-bulb rule, calibrate its thresholds on marginal storms (Alta vs Snowbird gauges).
6. BACK SIDE (park_city at the Thaynes SNOTEL): the same water correction, spillover features weighted most;
   targets Thaynes gauge + NWS "Park City Mountain" totals.
7. UNCERTAINTY + BINS: ensemble spread and the corrected forecasts' measured error spread -> calibrated bin
   probabilities; which bin to call tuned on the owner's bin score (the "lean").
OUTPUT: "CWX", every run, frozen, stored and scored like a model; private reports beside the raw models; the feed only
after it has proven itself and the owner approves.

## 3. Robustness (owner: "make this a more robust model")
- MORE DATA: pool the gauges (Alta, Snowbird, Brighton, Mill-D, Thaynes + nearby SNOTELs: Parleys, Lookout Peak,
  Timpanogos Divide, Ben Lomond) with shared physics and a small per-site offset; every hour, not only storms
  (false alarms); Collins boards for SLR. Later phase: GEFS v12 reforecast 2000-2019 (one frozen version; verify it
  is on AWS), older HRRR (2014+) / GFS (2015+) seasons, ERA5 (needs a Copernicus account in the owner's name).
- BETTER TARGETS: one best-estimate observed water per block (gauge + pillow + Collins) with an uncertainty; wind
  undercatch flagged; QC of every target (gauge glitch rules; NWS periods spanning two storms, e.g. 3/5/2026).
- PHYSICS IN THE STRUCTURE: crest-level moisture flux / IVT as the core orographic predictor; monotonic where
  physics says so; capped corrections; partial pooling (small groups shrink toward the overall answer).
- PREDICT A RANGE (v2): quantiles or a zero-inflated distribution, so bins and "less sure than usual" fall out;
  an analog check (similar past forecasts and what fell) as a second opinion.
- TESTING LUCK CAN'T PASS: whole storms / seasons held out; CRPS, Brier per bin, bin score; baselines raw models,
  a per-model multiplier, U of U guidance, climatology; bootstrap by storm (keep a change only if its improvement's
  interval stays above 0); separate checks for big storms, marginal rain/snow storms, each regime.
- LIVE ROBUSTNESS: a rolling bias update per model (absorbs upgrades: RRFS for NAM, new GFS versions); model version
  as a factor, recent seasons weighted more; automatic fallback to the raw blend if CWX's rolling skill drops; a
  missing model reweights the blend, a missing feature drops to the simpler level; training script + saved
  parameters per release + a golden test; each forecast lists its main adjustments ("+35%: deep moisture, strong
  NW crest flow").

## 4. v0 and switches (owner 2026-11-19: "a version from before reading the articles, and one that takes them in")
v0 = sections 1-3 as planned before reading the Wasatch Weather Weenies posts. Each idea from them is a SWITCH, off
in v0, tested alone against v0 (same season-out test, scores, storm bootstrap); v1 = v0 + every switch that passed,
tested again together. Both v0 and v1 then run live and frozen through 2026-27, the one season neither was tuned on:
it decides. Keep the switch list to these (each extra comparison on ~100 storms raises the chance of a lucky "win"):
| idea | v0 | switch |
| moisture flux | one predictor, any flow | interacts with flow direction: NW/WNW efficiency, extremes with low IVT (Wasserstein & Steenburgh 2024, MWR-D-23-0206.1) |
| spillover barrier | one "from the west" normal | direction-dependent: Park City ridge parallel to NW flow (weak lift); S/SE flow can favor PC / Deer Valley |
| snow level | Alta point (stuck at the model ground, "<9.3k") | wet-bulb zero from the SLC (KSLC) upstream profile, as U of U does |
| blocking (Froude) | Alta's own column (starts near the crest) | KSLC valley-to-crest stability (BUFKIT kslc, Iowa State, 2016+) |
| SLR limits | uncapped | capped ~7:1-20:1 (lower only below the wet-bulb zero), U of U practice |
| SLR benchmark | Kuchera, Veals | + Alcott & Steenburgh (2010), developed on Alta observations |
| extra inputs | none | U of U Great Salt Lake-effect guidance (LAKE_EFFECT.txt: lake-700 mb, excess, RH850-700, 700 mb wind, lapse, probability, areas; Alcott et al. 2012); GEPS ensemble; Mt. Baldy crest wind (if a free source exists) |
Also from the posts, small: fronts also from a sharp theta-e drop in time (beside the 650 mb cooling + veer); flag a
strong stable layer just below crest level; Gowan et al. (2018, models at high Wasatch SNOTELs) as a prior on
each model's bias; real Great Salt Lake temperature for the lake-effect predictor (U of U's AVHRR source is down:
their guidance uses climatology, 7 C; ours uses monthly climatology too).

## 5. Build order
0. The GFS dryness check (section 0).
1. Finish the backfill + features (running 2026-11-19: GFS / HRRR 2022-25, IFS 2024-25, 500 mb maps).
2. Baseline: raw skill by model, lead, regime (season tables; scripts/backfill_scores.py, timeheight_scores.py).
3. Water multiplier v0 (model x lead), season-out tested.
4. Add predictors one at a time, each kept only if season-out skill improves.
5. SLR model (separate track; needs the Collins Hive import).
6. Blend weights, then timing, then calibrated bins.
7. The switches (section 4), one at a time; then v1.
8. v0 + v1 live and frozen through 2026-27; feed only on the owner's approval.

## Already built (2026-11-19, in main, laptop tools; not in the pass or the feed)
- Time-height features from BUFKIT point soundings (models/bufkit.py, timeheight.py, scripts/time_height.py):
  freezing / wet-bulb-zero level, moist layer, growth zone, stability, convective flag, front, overrunning (dry gap),
  spillover (cross-barrier wind, Froude, drift). Backfill: scripts/backfill_timeheight.py (RAP analyses + HRRR / GFS
  forecasts, 4 winters done). Measurement: scripts/timeheight_scores.py (6 h blocks; onset; back vs front).
- 500 mb shortwaves + PVA (shortwaves.py, scripts/shortwaves.py) on the 1 deg pattern grid; backfill maps
  (scripts/backfill_patterns.py: GFS 0 h analyses + forecast maps).
- Thaynes Canyon SNOTEL (2016+) and the park_city point (internal).
