"""Archive model runs into data/archive/forecasts: HRRR, NAM Nest, NAM, RDPS, GFS, GDPS, ECMWF HRES (IFS),
and the GEFS and ECMWF ENS ensembles (precip only). Only 00/06/12/18z runs (GDPS: 00/12z).

Scheduled every 3 hours by Windows Task Scheduler (task "Cottonwoods models", scripts/run_models.bat). Each run
tries the latest archive.recent_cycles cycles of every model and skips ones already archived, so a missed run
(PC off) is caught up later. A lock file stops two runs overlapping.
    python scripts/archive_forecasts.py
The pass runs one child process per server STREAM at the same time (registry.STREAMS: noaa / ecmwf / canada);
lines in the log are prefixed with the stream. --serial runs everything in this one process instead.

Backfill (HRRR/GFS can be recovered from public archives; check which models in the registry have long archives):
    python scripts/archive_forecasts.py --start 2024-12-04 --end 2024-12-09 --models HRRR GFS
    python scripts/archive_forecasts.py --journal            # every storm window in data/journal/storms.csv (-3 d lead)
    python scripts/archive_forecasts.py --init 2026-11-02T12 --models HRRR --force

The logic (to-do list, retries, streams, lock, caches) lives in src/cottonwoods/archive/runner.py.
"""
import argparse
import sys
from datetime import datetime, timezone

from cottonwoods.archive import runner
from cottonwoods.archive.forecasts import archive_run
from cottonwoods.config import ARCHIVE_DIR, RAW_DIR, ensure_dirs, load_config
from cottonwoods.models.registry import REGISTRY

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+", help="default: archive.models from config")
ap.add_argument("--recent", type=int, help="latest N cycles per model (default: archive.recent_cycles)")
ap.add_argument("--init", help="a single init time, e.g. 2026-11-02T12")
ap.add_argument("--start", help="backfill start date/time (UTC)")
ap.add_argument("--end", help="backfill end date/time (UTC)")
ap.add_argument("--journal", action="store_true", help="backfill every storm window in the journal")
ap.add_argument("--force", action="store_true", help="re-pull runs that are already archived")
ap.add_argument("--serial", action="store_true", help="one process for all models (no parallel streams)")
ap.add_argument("--stream", help=argparse.SUPPRESS)   # internal: this process is one stream of a parallel pass
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
# Use the models named on the command line, otherwise the archive.models list in stations.yaml.
models = [m.upper() for m in (args.models or cfg["archive"]["models"])]
for m in models:
    if m not in REGISTRY:
        sys.exit(f"Unknown model {m}. Known: {list(REGISTRY)}")


def log(line: str) -> None:
    print(line, flush=True)


def archive_here() -> dict:
    """Archive this process's share of the work (all models, or one stream's)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)   # model init times are always UTC
    storms = None
    if args.journal:
        from cottonwoods.journal import load_storms
        storms = load_storms()
    jobs = runner.build_jobs(models, cfg, now, init=args.init, start=args.start, end=args.end,
                             journal_storms=storms, recent=args.recent)
    # One failure record per stream (streams run at the same time; each writes only its own file)
    failures = runner.FailureLog(ARCHIVE_DIR / (f"failures_{args.stream}.csv" if args.stream else "failures.csv"))
    scheduled = not (args.init or args.start or args.journal or args.force)
    save_dir = str(RAW_DIR / (f"herbie_{args.stream}" if args.stream else "herbie"))
    n = runner.run_jobs(jobs, cfg, archive_run, failures, now, force=args.force, scheduled=scheduled,
                        save_dir=save_dir, log=log)
    if scheduled:   # then put back values recent runs lost to download timeouts (archive/fill.py)
        fills = runner.FailureLog(ARCHIVE_DIR / (f"fills_{args.stream}.csv" if args.stream else "fills.csv"))
        n["fill"] = runner.fill_holes(models, cfg, now, fills, save_dir=save_dir, log=log)
    return n


def summary(n: dict) -> int:
    log(f"\ndone: {n['archived']} archived, {n['present']} already present, {n['failed']} failed, "
        f"{n['given_up']} skipped after {runner.MAX_TRIES} failed tries (data/archive/failures*.csv)"
        + (f"; holes: {n['fill']['filled']} runs filled, {n['fill']['failed']} fills failed" if "fill" in n else ""))
    # Exit code 1 tells Task Scheduler the run failed, but only when nothing worked at all. One missing run
    # (still posting) is normal and shouldn't count as a failure.
    return 1 if n["failed"] and not n["archived"] and not n["present"] else 0


if args.stream:                       # a child of a parallel pass: its parent holds the lock and cleans up
    sys.exit(summary(archive_here()))

lock = runner.PassLock(ARCHIVE_DIR / "archive_forecasts.lock")
with lock as got:
    if not got:
        sys.exit(f"[skip] another archive pass is running (lock {lock.path}, started {lock.started()})")
    try:
        groups = runner.split_streams(models)
        if not args.serial and len(groups) > 1:
            passthrough = []
            for flag in ("recent", "init", "start", "end"):
                if getattr(args, flag):
                    passthrough += [f"--{flag}", str(getattr(args, flag))]
            passthrough += [f"--{flag}" for flag in ("journal", "force") if getattr(args, flag)]
            codes = runner.run_streams(groups, __file__, passthrough, log=log)
            code = 1 if all(c != 0 for c in codes) else 0
        else:
            code = summary(archive_here())
    finally:
        runner.clear_caches(RAW_DIR, log=log)
sys.exit(code)
