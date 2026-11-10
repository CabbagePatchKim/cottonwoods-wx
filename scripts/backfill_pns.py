"""Backfill NWS Salt Lake storm statements (obs/pns.py) month by month into data/archive/obs/pns (plan step 4: the
backfill scores old runs against these Collins totals). Laptop, once; then send the store to the bucket:

    python scripts/backfill_pns.py --since 2016-10-01            # to today
    python scripts/bucket_sync.py push --only obs/pns/ym=2016-12.parquet ...   (or every file under obs/pns)

Only the winter months (pns_backfill_months below) are asked for; each month is one request, a second apart.
Fetching a month again replaces its rows, so a rerun is harmless.
"""
import argparse
import time
from datetime import date

import pandas as pd

from cottonwoods.archive.obs import archive_pns, with_retries
from cottonwoods.config import ensure_dirs, load_config

WINTER = {10, 11, 12, 1, 2, 3, 4, 5}      # statements outside these months are summer storms: not Collins snow

ap = argparse.ArgumentParser()
ap.add_argument("--since", required=True, help="first month, YYYY-MM-DD")
ap.add_argument("--until", default=date.today().isoformat())
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
total = 0
for m in pd.date_range(pd.Timestamp(args.since).replace(day=1), args.until, freq="MS"):
    if m.month not in WINTER:
        continue
    begin, end = m.strftime("%Y-%m-%d"), (m + pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d")
    n = with_retries(lambda: archive_pns(cfg, begin, end), cfg, f"pns {begin}..{end}")
    total += n
    print(f"[pns] {m:%Y-%m}: {n} Collins rows")
    time.sleep(1)
print(f"[pns] done: {total} rows")
