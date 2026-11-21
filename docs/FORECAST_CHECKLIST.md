# Personal forecast checklist (until the learning model is ready)

Owner and Claude, 2026-11-20, after the 11/19-20 storm (docs/STORM_NOTES.md). These are checks to make, not
calibrated numbers; the numbers come from the learning model (docs/LEARNING_PLAN.md). Plain-text copy:
docs/forecast_checklist.txt (keep the two in step).

## 1. Water first
- Median water across models, leaning on the ensemble mean and RRFS; no single model was reliably wet or dry in
  the backfill.
- Distrust HRRR's big 3-hour bursts (11/19-20: 0.3-0.4" per 3 h forecast at Alta; gauges never above 0.2").
  If its bursts aren't verifying early in a storm, discount the later ones.
- Ensemble means hide spread: check how many members are wet (feed: members with >= 0.10"). If fewer than half
  are wet, the likeliest outcome is below the mean.
- If the trend across runs is down (or up), shade the number that way, or say why not.
- Say which side you lean on when the high-res runs and the ensembles disagree.

## 2. Direction at crest level: 700 mb, not 850
- 700 mb at KSLC or the crest winds in the time-height plot. Ignore 850 mb at KSLC unless well over ~10 kt
  (below that it's valley / lake-breeze wind).
- S-SSW (~180-215 deg): expect the Wasatch Back and Snowbird to do relatively well; lean ranges toward the Back,
  but don't cut Alta by a fixed amount yet (Collins did fine on 11/19-20).
- SW-WSW (~220-260 deg): roughly neutral.
- W-NW at 700 mb (~270-330 deg): favors the Cottonwoods, Alta especially; lake-effect window.

## 3. Moisture depth and continuity
- KSLC sounding or scripts/time_height.py: moist-layer depth, whether the -12 to -18 C growth zone is saturated,
  dry gaps.
- Shallow, broken or gapped moisture: steady light rates, low end of the range; don't expect the bursts.
- Slack crest winds under a lingering moist layer: light snow that outlasts the runs.
- Slack winds = weak crest-level wind, roughly under 10 kt at 700 mb (rule of thumb, not calibrated; the main
  part of the 11/19-20 storm ran 20-35 kt). Little forced lift, so light rates; nothing moves the moisture
  out, so light snow lingers; little spillover to the Back; totals patchy (weak lift or small cells).

## 4. Snow ratio from crest temperature
- Mild SW storms (700 mb ~-5 to -8 C) run denser than cold NW ones; shade the ratio by temperature and don't
  trust Kuchera totals blindly. Whether 15:1 was too high on 11/19-20 is unknown until Collins water is checked.

## 5. Timing, not just the total
- Forecast 12-hour blocks (day/night), an onset time, a peak window and an end time.
- During the storm, watch run-to-run trends, not just the latest run; runs that keep pushing the end later are
  a signal.
- Time the end from the trough: when the 500 mb shortwave passes Alta's longitude (scripts/shortwaves.py) and
  whether another follows. Not yet scored in the backfill.
- Storms outlasting the models: measure it (end-time scoring in the backfill, to do).

## 6. Verify the right way
- Alta: Collins board snow and Collins water only (private; never in git or the feed).
- Other points: SNOTEL water. Depth change only to compare sites against each other, never for absolute totals
  (settling makes it run low).
- Score the total, the blocks, the peak window and the end separately.
- Near a bin edge, give bin probabilities instead of forcing one bin (e.g. "<1": 40%, "1-3": 55%, "3+": 5%):
  work out the low case (low water x low ratio) and the high case, and see which bins they land in.

## 7. Log the surprises
- One note per storm in docs/STORM_NOTES.md, naming the cause only after checking the soundings and the 700 mb
  winds.

Withdrawn during the 11/20 discussion: the NW-tail rule (it was 850 mb valley wind), Atwater as Alta's
verification, a fixed 15-25% direction cut, 9-12:1 for mild storms, and the 11-13" worked example.
