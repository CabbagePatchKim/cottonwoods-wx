"""Copy the archive between data/ and the cloud bucket (src/cottonwoods/bucket.py).

    python scripts/bucket_sync.py pull     # bucket -> data/ (what's missing or different; recent model runs only)
    python scripts/bucket_sync.py push     # data/ -> bucket (what's new or changed; never deletes)
    python scripts/bucket_sync.py push --env-file C:/Users/patri/.cwx_r2.env   # credentials from a file
    python scripts/bucket_sync.py push --only processed/regime_factors.csv     # just that file (laptop -> cloud)

Once the cloud runs the passes, a full push from the laptop would put the laptop's older storms.csv etc. over the
cloud's: from the laptop, use --only for the files you mean to send.

GitHub Actions runs pull before each pass and push after it. On the laptop: pull to develop against the live
archive. Credentials: environment variables CWX_BUCKET, CWX_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
or the same NAME=value lines in a file passed with --env-file (kept outside the project; never printed).
"""
import argparse
import os
from datetime import datetime, timezone

from cottonwoods import bucket
from cottonwoods.config import DATA_DIR, load_config

ap = argparse.ArgumentParser()
ap.add_argument("direction", choices=["pull", "push"])
ap.add_argument("--env-file", help="NAME=value lines with the four credentials")
ap.add_argument("--only", nargs="+", help="push: only these paths under data/, e.g. processed/regime_factors.csv")
args = ap.parse_args()

if args.env_file:
    bucket.load_env_file(args.env_file)

problems = bucket.check_env()
if problems:
    raise SystemExit("[bucket] can't connect:\n  " + "\n  ".join(problems))
ccfg = load_config()["cloud"]
s3, name = bucket.client(), os.environ["CWX_BUCKET"].strip()
if args.direction == "pull":
    got, old, kept = bucket.pull(s3, name, DATA_DIR, ccfg, datetime.now(timezone.utc).replace(tzinfo=None))
    print(f"[bucket] pulled {got} files ({old} model runs older than {ccfg['pull_forecast_days']} days left in the "
          f"bucket; {kept} local files newer than the bucket's kept)")
else:
    print(f"[bucket] pushed {bucket.push(s3, name, DATA_DIR, ccfg, only=args.only)} new or changed files")
