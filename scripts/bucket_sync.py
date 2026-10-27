"""Copy the archive between data/ and the cloud bucket (src/cottonwoods/bucket.py).

    python scripts/bucket_sync.py pull     # bucket -> data/ (what's missing or different; recent model runs only)
    python scripts/bucket_sync.py push     # data/ -> bucket (what's new or changed; never deletes)
    python scripts/bucket_sync.py push --env-file C:/Users/patri/.cwx_r2.env   # credentials from a file

GitHub Actions runs pull before each pass and push after it. On the laptop: pull to develop against the live
archive. Credentials: environment variables CWX_BUCKET, CWX_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
or the same NAME=value lines in a file passed with --env-file (kept outside the project; never printed).
"""
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from cottonwoods import bucket
from cottonwoods.config import DATA_DIR, load_config

ap = argparse.ArgumentParser()
ap.add_argument("direction", choices=["pull", "push"])
ap.add_argument("--env-file", help="NAME=value lines with the four credentials")
args = ap.parse_args()

if args.env_file:
    for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

ccfg = load_config()["cloud"]
s3, name = bucket.client(), os.environ["CWX_BUCKET"]
if args.direction == "pull":
    got, old = bucket.pull(s3, name, DATA_DIR, ccfg, datetime.now(timezone.utc).replace(tzinfo=None))
    print(f"[bucket] pulled {got} files ({old} model runs older than {ccfg['pull_forecast_days']} days left in the bucket)")
else:
    print(f"[bucket] pushed {bucket.push(s3, name, DATA_DIR, ccfg)} new or changed files")
