"""Verification backfill step 2 (docs/PLAN.md step 4): fetch the past runs for the storm list (backfill.plan_runs)
into data/local/backfill/archive (its own archive: never the live one, never synced). Laptop; one model at a time,
with the live pass's parallel downloads. Already fetched runs are skipped, so it can be stopped and started again.

    python scripts/backfill_runs.py --winter 2025-26 --model GFS            # every planned GFS run of that winter
    python scripts/backfill_runs.py --winter 2025-26 --model GFS --limit 1  # one run, to time it

Each run's result (archived / exists / failed, seconds) is appended to data/local/backfill/runs_log.csv.
"""
import argparse
import time
from datetime import datetime, timezone

import pandas as pd

from cottonwoods import backfill
from cottonwoods.archive.forecasts import archive_run
from cottonwoods.config import DATA_DIR, load_config

ap = argparse.ArgumentParser()
ap.add_argument("--winter", required=True, help="e.g. 2025-26 (October to May)")
ap.add_argument("--model", required=True)
ap.add_argument("--limit", type=int, help="fetch at most this many runs")
args = ap.parse_args()

cfg = load_config()
base = DATA_DIR / "local" / "backfill"
y = int(args.winter[:4])
found = pd.read_csv(base / "storms.csv", parse_dates=["final_start", "final_end"])
found = found[(found["final_start"] >= f"{y}-10-01") & (found["final_start"] < f"{y + 1}-06-01")]
plan = backfill.plan_runs(found, cfg)
plan = plan[plan["model"] == args.model.upper()]
if args.limit:
    plan = plan.head(args.limit)
print(f"[backfill] {args.model} {args.winter}: {len(plan)} runs, {plan['max_lead_hours'].sum():.0f} forecast hours")
log = base / "runs_log.csv"
for r in plan.itertuples():
    t0, status = time.time(), ""
    try:
        _, status = archive_run(r.model, r.init.to_pydatetime(), cfg, root=base / "archive",
                                max_lead_hours=r.max_lead_hours)
    except Exception as e:  # noqa: BLE001 - one bad run doesn't stop the rest; it's logged and retried next time
        status = f"failed: {type(e).__name__}: {str(e)[:150]}"
    secs = time.time() - t0
    print(f"[backfill] {r.model} {r.init:%Y-%m-%d %H}z to {r.max_lead_hours:.0f} h: {status} ({secs:.0f} s)")
    pd.DataFrame([{"logged": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "model": r.model,
                   "init": f"{r.init:%Y-%m-%d %H:%M}", "max_lead_hours": r.max_lead_hours, "storms": r.storms,
                   "status": status, "seconds": round(secs)}]).to_csv(log, mode="a", header=not log.exists(), index=False)
