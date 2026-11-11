"""Backfill observations month by month for the verification backfill (docs/PLAN.md step 4). Laptop; winter months
only (October-May); fetching a month again replaces its rows, so a rerun is harmless.

    python scripts/backfill_obs.py pns --since 2016-10-01       # NWS storm statements -> data/archive/obs/pns
    python scripts/backfill_obs.py snotel --since 2016-10-01    # hourly SNOTEL (every config snotel site)
        --until 2026-05-31 (default today)

The live pass's own obs step keeps recent months current; this fills the past. Send a store to the bucket with
`python scripts/bucket_sync.py push --only <files>` only when the cloud needs it (the passes read recent months).
"""
import argparse
import time
from datetime import date

import pandas as pd

from cottonwoods.archive.obs import archive_pns, archive_snotel, with_retries
from cottonwoods.config import ensure_dirs, load_config

WINTER = {10, 11, 12, 1, 2, 3, 4, 5}      # summer storms are not Collins snow
SOURCES = {"pns": archive_pns, "snotel": archive_snotel}

ap = argparse.ArgumentParser()
ap.add_argument("source", choices=sorted(SOURCES))
ap.add_argument("--since", required=True, help="first month, YYYY-MM-DD")
ap.add_argument("--until", default=date.today().isoformat())
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
archive, total = SOURCES[args.source], 0
for m in pd.date_range(pd.Timestamp(args.since).replace(day=1), args.until, freq="MS"):
    if m.month not in WINTER:
        continue
    # end = the next month's first day: IEM's end date may be exclusive; a day of overlap is merged away
    begin, end = m.strftime("%Y-%m-%d"), (m + pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d")
    n = with_retries(lambda: archive(cfg, begin, end), cfg, f"{args.source} {begin}..{end}")
    total += n
    print(f"[{args.source}] {m:%Y-%m}: {n} rows")
    time.sleep(1)
print(f"[{args.source}] done: {total} rows")
