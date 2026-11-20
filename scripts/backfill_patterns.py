"""500 mb pattern maps for the backfill storms (owner 2026-11-19, shortwaves): the live pattern pull
(patterns.archive_pattern) on past runs, one winter, one model. Laptop only; skips runs already fetched.

    python scripts/backfill_patterns.py --winter 2025-26 --what analyses           # GFS 0 h analyses (full maps), chained
    python scripts/backfill_patterns.py --winter 2025-26 --what forecasts --model GFS

analyses   every 6-hourly GFS run from analysis_before_hours before each storm (>= backfill.min_water_in) to
           analysis_after_hours after it, only its shortwaves.analysis_lead_hr map (0 h: the ANALYSIS, a full map)
           -> data/local/backfill/maps_analysis
forecasts  every run backfill.plan_runs picks for the model, maps to the storm's end (rounded up to 6 h, at most the
           pattern inner box's max_hours) -> data/local/backfill/archive/patterns (next to the backfilled point runs)
Then: python scripts/shortwaves.py ... --root data/local/backfill/maps_analysis --fc-root data/local/backfill/archive
"""
import argparse
import copy
import time

import pandas as pd

from cottonwoods import backfill
from cottonwoods.config import DATA_DIR, load_config
from cottonwoods.patterns import archive_pattern

ap = argparse.ArgumentParser()
ap.add_argument("--winter", required=True)
ap.add_argument("--what", choices=["analyses", "forecasts"], required=True)
ap.add_argument("--model", default="GFS")
args = ap.parse_args()

cfg = load_config()
sc = cfg["shortwaves"]
base = DATA_DIR / "local" / "backfill"
y = int(args.winter[:4])
found = pd.read_csv(base / "storms.csv", parse_dates=["final_start", "final_end"])
found = found[(found["final_start"] >= f"{y}-10-01") & (found["final_start"] < f"{y + 1}-06-01")]
found = found[found["obs_water_in"] >= cfg["backfill"]["min_water_in"]]

jobs = []                                    # (init, max_hours)
if args.what == "analyses":
    root = base / "maps_analysis"
    lead = sc["analysis_lead_hr"]
    inits = set()
    for s in found.itertuples():
        a = (s.final_start - pd.Timedelta(hours=sc["analysis_before_hours"] + lead)).floor("6h")
        b = s.final_end + pd.Timedelta(hours=sc["analysis_after_hours"])
        inits |= set(pd.date_range(a, b, freq="6h"))
    jobs = [(t, lead) for t in sorted(inits)]
else:
    root = base / "archive"
    c = copy.deepcopy(cfg)
    c["backfill"]["models"] = [args.model]
    cap = cfg["patterns"]["inner"]["max_hours"]
    for r in backfill.plan_runs(found, c).itertuples():
        jobs.append((r.init, int(min(cap, -(-r.max_lead_hours // 6) * 6))))
print(f"[maps] {args.model} {args.winter} {args.what}: {len(jobs)} runs -> {root}", flush=True)

t0, done, failed = time.time(), 0, 0
for init, hours in jobs:
    c = copy.deepcopy(cfg)
    c["patterns"]["max_hours"] = hours
    c["patterns"]["inner"]["max_hours"] = hours
    if args.what == "analyses":
        c["patterns"]["first_hour"] = hours           # just the analysis map (lead analysis_lead_hr, 0)
    try:
        _, status = archive_pattern(args.model, init.to_pydatetime(), c, root=root, workers=4)
        done += status == "archived"
    except Exception as e:  # noqa: BLE001 - logged, the rest go on; a rerun retries it
        failed += 1
        print(f"[maps] {init:%Y-%m-%d %H}z: {type(e).__name__}: {str(e)[:120]}", flush=True)
    if (done + failed) % 25 == 0 and done + failed:
        print(f"[maps] {done} archived, {failed} failed, {time.time() - t0:.0f} s", flush=True)
print(f"[maps] {args.model} {args.winter} {args.what}: {done} archived, {failed} failed, {time.time() - t0:.0f} s")
