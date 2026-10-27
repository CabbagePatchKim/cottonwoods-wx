"""Archive weather-balloon soundings (Salt Lake City, KSLC) into data/archive/obs/sounding.

Scheduled twice a day, after the 00z and 12z launches (Windows Task Scheduler task "Cottonwoods soundings",
8 am and 8 pm local). Each run re-fetches the last 3 days, so a missed run or a late sounding is caught next time.
    python scripts/archive_soundings.py

Backfill (done a month at a time; already-saved soundings are simply replaced):
    python scripts/archive_soundings.py --start 2007-10-01 --end 2026-06-30
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from cottonwoods.archive.obs import archive_soundings, load_soundings
from cottonwoods.config import ensure_dirs, load_config
from cottonwoods.obs.sounding import summarize

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=3, help="trailing days to (re)fetch")
ap.add_argument("--start")
ap.add_argument("--end")
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
now = datetime.now(timezone.utc).replace(tzinfo=None)

if args.start:
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp(now)
    # month-sized requests: one month of soundings is ~15,000 rows, a comfortable size for the IEM service
    starts = pd.date_range(pd.Timestamp(args.start).to_period("M").to_timestamp(), end, freq="MS")
    spans = [(max(s, pd.Timestamp(args.start)), min(s + pd.offsets.MonthEnd(1) + pd.Timedelta(hours=23), end))
             for s in starts]
else:
    spans = [(pd.Timestamp(now - timedelta(days=args.days)), pd.Timestamp(now))]

fails = 0
for begin, end in spans:
    try:
        n = archive_soundings(cfg, begin.isoformat(), end.isoformat())
        print(f"[sounding] {begin:%Y-%m-%d}..{end:%Y-%m-%d}: store now holds {n:,} rows in touched months")
    except Exception as e:  # noqa: BLE001  (one bad month must not stop a long backfill)
        fails += 1
        print(f"[sounding] {begin:%Y-%m-%d}..{end:%Y-%m-%d} FAILED: {e}")

# Show the newest soundings so a scheduled run's log says what arrived.
recent = load_soundings(start=now - timedelta(days=2))
if not recent.empty:
    cols = ["time_utc", "t700_c", "rh700", "wdir700", "wspd700_kt", "t500_c", "freezing_level_m", "dgz_depth_m"]
    print(summarize(recent)[cols].round({c: 1 for c in cols[1:]}).to_string(index=False))
sys.exit(1 if fails and fails == len(spans) else 0)
