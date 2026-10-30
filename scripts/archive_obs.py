"""Archive SNOTEL observations into data/archive/obs/snotel, and Synoptic stations (Alta Collins) into
data/archive/obs/synoptic, and NOAA coop stations (ALTA) into data/archive/obs/ghcn.
Synoptic needs SYNOPTIC_TOKEN set; without it those stations are skipped. NOAA needs no token, but its daily
values post 1-3 weeks late, so a scheduled fetch re-asks the last obs_fetch.ghcn_days for it.

Long NOAA backfill (ALTA goes back to 1905; one request per chunk, so keep chunks to a few years):
    python scripts/archive_obs.py --start 2004-10-01 --end 2026-06-30

Scheduled usage (every pass runs --days 3; revisions overwrite earlier values, missed hours back to the newest
stored one are filled in, and each source is retried per config obs_fetch):
    python scripts/archive_obs.py

Backfill:
    python scripts/archive_obs.py --start 2024-11-01 --end 2025-04-30
    python scripts/archive_obs.py --journal
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone


from cottonwoods import timefmt
from cottonwoods.archive.obs import (archive_ghcn, archive_rap, archive_snotel, archive_synoptic, catchup_begin, ghcn_begin,
                                     latest_obs, with_retries)
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
ghcn_spans = None      # NOAA coop: the same spans, except a scheduled fetch reaches back further (ghcn_begin)
if args.journal:
    from cottonwoods.journal import load_storms

    for _, s in load_storms().iterrows():
        # pad each storm by 3 days so the SWE before and after the storm is in the data too
        spans.append(((s.start_date - timedelta(days=3)).strftime("%Y-%m-%d"), (s.end_date + timedelta(days=3)).strftime("%Y-%m-%d")))
elif args.start:
    spans.append((args.start, args.end or today.strftime("%Y-%m-%d")))
else:
    # re-fetch the trailing --days each pass (SNOTEL sometimes revises recent values, and newer ones replace older),
    # reaching further back when earlier passes missed hours (obs_fetch.catchup_max_days at most)
    begin = catchup_begin(cfg, today, args.days)
    if begin < today - timedelta(days=args.days):
        print(f"[obs] catching up: newest stored SNOTEL hour is before the trailing {args.days} days; fetching from {begin}")
    spans.append((begin.strftime("%Y-%m-%d"), (today + timedelta(days=1)).strftime("%Y-%m-%d")))
    ghcn_spans = [(ghcn_begin(cfg, today, begin).strftime("%Y-%m-%d"), spans[0][1])]

# Each source is tried obs_fetch.retries times per span. A failure doesn't stop the other sources (the storm step
# after this one uses whatever is stored); the exit code is 1 if anything still failed, so the log shows it.
sources = [("obs", archive_snotel, spans)]
status = {}                # source tag -> what happened, shown in the table at the end
# Synoptic/MesoWest stations (Alta Collins). cfg.get() returns None instead of erroring if the section is missing.
# With no token we skip rather than fail, so the SNOTEL half still counts as success.
if cfg.get("synoptic") and not synoptic.token():
    status["obs:synoptic"] = "skipped (no SYNOPTIC_TOKEN)"
elif cfg.get("synoptic"):
    sources.append(("obs:synoptic", archive_synoptic, spans))
# NOAA GHCN-Daily (ALTA). Daily values, so even a 20-year span is only ~7,000 rows per element.
if cfg.get("ghcn"):
    sources.append(("obs:ghcn", archive_ghcn, ghcn_spans or spans))
# Hourly RAP analyses: the observed 700 mb flow for storm regimes (only hours not stored yet are fetched)
sources.append(("obs:rap", archive_rap, spans))

failed = False
for tag, archive, source_spans in sources:
    for begin, end in source_spans:
        try:
            with_retries(lambda: archive(cfg, begin, end), cfg, f"{tag} {begin}..{end}")
            status.setdefault(tag, f"ok ({begin}..{end})")
        except Exception as e:  # noqa: BLE001  (failures print in full; the tables follow)
            print(f"[{tag}] {begin}..{end} FAILED after {cfg['obs_fetch']['retries']} tries: {e}")
            status[tag] = "FAILED (see above)"
            failed = True

# The step's output is two tables (owner, 2026-10-30): each source's status and newest value, then each site's latest
now = datetime.now(timezone.utc).replace(tzinfo=None)
newest, sites = latest_obs(cfg, now)
names = {"SNOTEL": "obs", "NOAA ALTA": "obs:ghcn", "RAP (observed flow)": "obs:rap"}
when = lambda t: timefmt.clock(t, weekday=True) if t == t else "-"   # noqa: E731  (t != t: NaT)
print(f"Observations, {timefmt.now_line(now)}. Times Mountain.")
rows = [(r.source, status.get(names[r.source], "not fetched"),
         f"{timefmt.local(r.newest):%a %m/%d} (observer day)" if r.source == "NOAA ALTA" and r.newest == r.newest
         else when(r.newest)) for r in newest.itertuples()]
rows += [("Synoptic (Collins)", status["obs:synoptic"], "-")] if "obs:synoptic" in status and "skipped" in status["obs:synoptic"] else []
print(f"{'source':<22}{'status':<30}newest")
print("\n".join(f"{a:<22}{b:<30}{c}" for a, b, c in rows))
if "NOAA ALTA" in set(newest["source"]):
    print("  (NOAA ALTA = the observer's day ending 6 pm MST; it posts 1-3 weeks late)")
print(f"\n{'site':<20}{'elev ft':>8}  {'newest':<22}{'water 6 h':>10}{'24 h':>7}{'depth':>7}{'pillow':>8}{'temp':>6}")
f = lambda x, spec, unit="": "-" if x != x else format(x, spec) + unit   # noqa: E731
for r in sites.itertuples():
    print(f"{r.site:<20}{r.elev_ft:>8,}  {when(r.newest):<22}{f(r.water_6h_in, '.2f', chr(34)):>10}"
          f"{f(r.water_24h_in, '.2f', chr(34)):>7}{f(r.depth_in, '.0f', chr(34)):>7}{f(r.pillow_in, '.1f', chr(34)):>8}"
          f"{f(r.temp_f, '.0f', 'F'):>6}")
print("  water = gauge precip (noise-filtered, gauge.py); pillow = snow water on the pillow (blank without snowpack)")
sys.exit(1 if failed else 0)
