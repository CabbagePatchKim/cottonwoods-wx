"""Write the website feed (static JSON; see src/cottonwoods/feed.py). Run after scripts/storms.py.

    python scripts/publish_feed.py            # approved releases only -> data/feed/v1/
    python scripts/publish_feed.py --preview  # any code, for checking -> data/feed_preview/v1/ (never published)

Without --preview this stops unless the code running is an owner-approved release (cottonwoods/release.py):
development code can't feed the website, even when run by hand.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from cottonwoods import feed, release, storms
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.config import FEED_DIR, FEED_PREVIEW_DIR, ROOT, load_config
from cottonwoods.report import staleness

ap = argparse.ArgumentParser()
ap.add_argument("--preview", action="store_true", help="write a preview from any code; never published")
args = ap.parse_args()

if args.preview:
    version, folder = release.dev_version(ROOT), FEED_PREVIEW_DIR
else:
    try:
        version, folder = release.approved_version(ROOT), FEED_DIR
    except release.NotApproved as e:
        sys.exit(f"NOT PUBLISHED: {e}. Use --preview to look at the output.")

cfg = load_config()
now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
fc = load_forecasts(cfg, start=now - timedelta(days=20))   # same span scripts/storms.py tracks storms over
if fc.empty:
    sys.exit("No archived model runs in the last 20 days; feed not written.")
data = feed.build_feed(storms.load_storms(), fc, cfg, now, version, staleness(cfg, now))
out = feed.write_feed(data, folder)
print(f"feed {version} written to {out} ({', '.join(f'{k}.json' for k in data)})")
