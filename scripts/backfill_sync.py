"""Share the backfill between PCs through the private bucket (src/cottonwoods/backfill_sync.py).

    python scripts/backfill_sync.py status --env-file C:/Users/patri/.cwx_r2.env   # what each direction would send
    python scripts/backfill_sync.py push   --env-file ...                          # this PC -> bucket
    python scripts/backfill_sync.py pull   --env-file ...                          # bucket -> this PC

Only data/local/backfill and data/local/bufkit, in their own part of the bucket: never the live archive, which
bucket_sync.py handles. Nothing is deleted on either side. Run backfills on one PC at a time; push may run during one,
pull not.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(Path.home() / ".cwx_pylib")]   # this copy's code; the laptop's boto3

from cottonwoods import backfill_sync, bucket  # noqa: E402
from cottonwoods.config import DATA_DIR  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("action", choices=["status", "push", "pull"])
ap.add_argument("--env-file", help="NAME=value lines with the four credentials")
args = ap.parse_args()

if args.env_file:
    bucket.load_env_file(args.env_file)
problems = bucket.check_env()
if problems:
    raise SystemExit("[backfill-sync] can't connect:\n  " + "\n  ".join(problems))
s3, name = bucket.client(), os.environ["CWX_BUCKET"].strip()
if args.action == "status":
    remote = backfill_sync.remote_files(s3, name)
    local = backfill_sync.local_files(DATA_DIR)
    up, down = backfill_sync.plan(DATA_DIR, remote)
    mb = sum(local[r].stat().st_size for r in up) / 1e6
    print(f"[backfill-sync] here {len(local)} files, bucket {len(remote)}: push would send {len(up)} ({mb:.0f} MB), "
          f"pull would fetch {len(down)} ({sum(remote[r][0] for r in down) / 1e6:.0f} MB)")
elif args.action == "push":
    print(f"[backfill-sync] pushed {backfill_sync.push(s3, name, DATA_DIR)} files")
else:
    print(f"[backfill-sync] pulled {backfill_sync.pull(s3, name, DATA_DIR)} files")
