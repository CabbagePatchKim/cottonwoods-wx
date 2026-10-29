"""Archive the long-range models' regional pattern maps (src/cottonwoods/patterns.py) for their newest cycles.
Run by every pass after the point archive; runs already saved are skipped, so a missed pass is caught up.
    python scripts/archive_patterns.py
    python scripts/archive_patterns.py --models GFS --init 2026-10-29T00
"""
import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

from cottonwoods import patterns
from cottonwoods.config import RAW_DIR, ensure_dirs, load_config
from cottonwoods.models.registry import recent_cycles, stream_of

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+", help="default: the models in config patterns.models")
ap.add_argument("--init", help="one cycle, e.g. 2026-10-29T00")
args = ap.parse_args()

ensure_dirs()
cfg = load_config()
pcfg = cfg["patterns"]
now = datetime.now(timezone.utc).replace(tzinfo=None)
done = failed = 0
for model in [m.upper() for m in (args.models or pcfg["models"])]:
    cycles = pcfg["models"][model]["cycles_utc"]
    inits = ([pd.Timestamp(args.init).to_pydatetime()] if args.init else
             [t for t in recent_cycles(model, cfg, now, pcfg["recent_cycles"]) if t.hour in cycles])
    stream = stream_of(model)
    for init in inits:
        try:
            path, status = patterns.archive_pattern(
                model, init, cfg, save_dir=str(RAW_DIR / f"herbie_patterns_{stream}"),
                workers=cfg["archive"]["download_workers"][stream], throttle=cfg["archive"]["throttle"])
            if status == "archived":
                done += 1
                print(f"[pattern] {model} {init:%Y-%m-%d %Hz} -> {path}", flush=True)
        except Exception as e:  # noqa: BLE001  (one run failing must not stop the others)
            failed += 1
            print(f"[pattern] {model} {init:%Y-%m-%d %Hz} not archived: {e}", flush=True)
print(f"[pattern] done: {done} archived, {failed} not yet (retried next pass)")
sys.exit(1 if failed and not done and args.init else 0)
