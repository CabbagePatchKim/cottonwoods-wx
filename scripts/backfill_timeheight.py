"""Time-height backfill (owner 2026-11-19): BUFKIT profiles for the backfill storm list, one winter. Laptop only.

    python scripts/backfill_timeheight.py --winter 2025-26

1. ANALYSES: every hourly RAP run from timeheight.pad_hours before each storm (>= backfill.min_water_in) to its end;
   each run's FIRST profile = a near-observed time-height at Alta. Stored raw (levels + hours, so thresholds can be
   re-tuned without downloading again): data/local/backfill/timeheight/analysis_<winter>_{levels,hours}.parquet.
2. FORECASTS: the BUFKIT file of every run backfill.plan_runs picks for timeheight.backfill_models (HRRR, GFS),
   cached in data/local/bufkit/<MODEL>/alta_<init>.buf.
Already-fetched storms / files are skipped, so it can be stopped and started again. Missing files are counted.
"""
import argparse
import copy
import time

import pandas as pd

from cottonwoods import backfill, timeheight
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.models import bufkit

ap = argparse.ArgumentParser()
ap.add_argument("--winter", required=True)
args = ap.parse_args()

cfg = load_config()
tc = cfg["timeheight"]
y = int(args.winter[:4])
found = pd.read_csv(DATA_DIR / "local" / "backfill" / "storms.csv", parse_dates=["final_start", "final_end"])
found = found[(found["final_start"] >= f"{y}-10-01") & (found["final_start"] < f"{y + 1}-06-01")]
found = found[found["obs_water_in"] >= cfg["backfill"]["min_water_in"]]
out = DATA_DIR / "local" / "backfill" / "timeheight"
out.mkdir(parents=True, exist_ok=True)
lv_path, hr_path = out / f"analysis_{args.winter}_levels.parquet", out / f"analysis_{args.winter}_hours.parquet"
have_lv = pd.read_parquet(lv_path) if lv_path.exists() else pd.DataFrame()
have_hr = pd.read_parquet(hr_path) if hr_path.exists() else pd.DataFrame()
done = set(have_hr["time"]) if not have_hr.empty else set()

t0, got, missing = time.time(), 0, 0
for s in found.itertuples():
    hours = pd.date_range(s.final_start - pd.Timedelta(hours=tc["pad_hours"]), s.final_end, freq="h")
    texts = []
    for h in hours:
        if h in done:
            continue
        try:
            texts.append(bufkit.fetch(tc["analysis_model"], "alta", h.to_pydatetime(), cfg))
            got += 1
        except Exception:  # noqa: BLE001 - a missing hour is counted, the rest go on
            missing += 1
        time.sleep(tc["pause_s"])
    if texts:
        lv, hr = timeheight.first_hours(texts)
        have_lv = pd.concat([have_lv, lv], ignore_index=True).drop_duplicates(["time", "pres"])
        have_hr = pd.concat([have_hr, hr], ignore_index=True).drop_duplicates("time")
        have_lv.to_parquet(lv_path, index=False)
        have_hr.to_parquet(hr_path, index=False)
        done |= set(hr["time"])
    print(f"[timeheight] {s.storm_id}: analyses {len(texts)} new ({time.time() - t0:.0f} s so far)", flush=True)
print(f"[timeheight] {args.winter} analyses: {got} fetched, {missing} missing -> {lv_path.name}")

c = copy.deepcopy(cfg)
c["backfill"]["models"] = tc["backfill_models"]
plan = backfill.plan_runs(found, c)
cache = DATA_DIR / "local" / "bufkit"
ok = miss = 0
for r in plan.itertuples():
    try:
        bufkit.fetch(r.model, "alta", r.init.to_pydatetime(), cfg, cache)
        ok += 1
    except Exception as e:  # noqa: BLE001
        miss += 1
        print(f"[timeheight] {r.model} {r.init:%Y-%m-%d %H}z: {str(e)[:120]}")
    time.sleep(tc["pause_s"])
print(f"[timeheight] {args.winter} forecasts: {ok} files ({', '.join(tc['backfill_models'])}), {miss} missing")
