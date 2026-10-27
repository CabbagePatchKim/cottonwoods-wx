"""Archive SNOTEL observations into data/archive/obs/snotel, and Synoptic stations (Alta Collins) into
data/archive/obs/synoptic, and NOAA coop stations (ALTA) into data/archive/obs/ghcn.
Synoptic needs SYNOPTIC_TOKEN set; without it those stations are skipped. NOAA needs no token, but its daily
values usually post a few days late, which the trailing re-fetch covers.

Long NOAA backfill (ALTA goes back to 1905; one request per chunk, so keep chunks to a few years):
    python scripts/archive_obs.py --start 2004-10-01 --end 2026-06-30

Cron usage (daily is enough; it re-fetches the trailing 7 days so revisions overwrite earlier values):
    python scripts/archive_obs.py

Backfill:
    python scripts/archive_obs.py --start 2024-11-01 --end 2025-04-30
    python scripts/archive_obs.py --journal
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone


from cottonwoods.archive.obs import archive_ghcn, archive_snotel, archive_synoptic
from cottonwoods.obs import synoptic
from cottonwoods.config import ensure_dirs, load_config

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=7, help="trailing days to (re)fetch")
ap.add_argument("--start")
ap.add_argument("--end")
ap.add_argument("--journal", action="store_true", help="fetch every storm window in the journal (+/- 3 days)")
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
today = datetime.now(timezone.utc).date()

# spans = list of (begin, end) date strings to fetch. Like archive_forecasts.py, the flags choose how it's
# built: every journal storm, an explicit range, or (default) the trailing --days.
spans = []
if args.journal:
    from cottonwoods.journal import load_storms

    for _, s in load_storms().iterrows():
        # pad each storm by 3 days so the SWE before and after the storm is in the data too
        spans.append(((s.start_date - timedelta(days=3)).strftime("%Y-%m-%d"), (s.end_date + timedelta(days=3)).strftime("%Y-%m-%d")))
elif args.start:
    spans.append((args.start, args.end or today.strftime("%Y-%m-%d")))
else:
    # re-fetch the last week each day: SNOTEL sometimes revises recent values, and newer ones replace older
    spans.append(((today - timedelta(days=args.days)).strftime("%Y-%m-%d"), (today + timedelta(days=1)).strftime("%Y-%m-%d")))

for begin, end in spans:
    try:
        n = archive_snotel(cfg, begin, end)
        print(f"[obs] {begin}..{end}: store now holds {n:,} rows in touched months")
    except Exception as e:  # noqa: BLE001
        print(f"[obs] {begin}..{end} FAILED: {e}")
        sys.exit(1)

# Second pass: Synoptic/MesoWest stations (Alta Collins). cfg.get() returns None instead of erroring if the
# section is missing. With no token we skip rather than fail, so the SNOTEL half still counts as success.
if cfg.get("synoptic") and not synoptic.token():
    print("[obs] SYNOPTIC_TOKEN not set; skipped Synoptic stations (Alta Collins).")
elif cfg.get("synoptic"):
    for begin, end in spans:
        try:
            n = archive_synoptic(cfg, begin, end)
            print(f"[obs:synoptic] {begin}..{end}: store now holds {n:,} rows in touched months")
        except Exception as e:  # noqa: BLE001
            print(f"[obs:synoptic] {begin}..{end} FAILED: {e}")
            sys.exit(1)

# Third pass: NOAA GHCN-Daily (ALTA). Daily values, so even a 20-year span is only ~7,000 rows per element.
if cfg.get("ghcn"):
    for begin, end in spans:
        try:
            n = archive_ghcn(cfg, begin, end)
            print(f"[obs:ghcn] {begin}..{end}: store now holds {n:,} rows in touched months")
        except Exception as e:  # noqa: BLE001
            print(f"[obs:ghcn] {begin}..{end} FAILED: {e}")
            sys.exit(1)
