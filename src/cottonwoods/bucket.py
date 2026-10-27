"""Keep the archive in a cloud bucket (Cloudflare R2, S3-compatible), so a pass can run on a fresh machine
(GitHub Actions) and the laptop can fetch the same data for development.

    pull  bucket -> data/: only files that are missing locally or different. Model runs older than
          cloud.pull_forecast_days are skipped (a pass never reads them), so a fresh machine stays fast.
    push  data/ -> bucket: only files that are new or changed. NOTHING is ever deleted from the bucket.

What's synced is config `cloud` (dirs, exclude patterns). Credentials and the bucket come from the environment,
never from the code or config: CWX_BUCKET, CWX_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY.
"Different" means a different MD5 (the bucket's ETag for single-part uploads) or size.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

PREFIX = "data/"   # everything sits under data/ in the bucket, mirroring the project folder
RUN_FILE = re.compile(r"(^|/)forecasts/model=[^/]+/init=(\d{10})\.\w+$")


def client():
    import boto3   # only needed when syncing; the rest of the project doesn't depend on it
    return boto3.client("s3", endpoint_url=os.environ["CWX_S3_ENDPOINT"], region_name="auto",
                        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"])


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def wanted(rel: str, ccfg: dict) -> bool:
    """rel is a path under data/ ('archive/storms.csv'): inside a synced dir and not excluded."""
    return any(rel.startswith(d.rstrip("/") + "/") for d in ccfg["dirs"]) and \
        not any(fnmatch.fnmatch(rel, pat) for pat in ccfg["exclude"])


def too_old(rel: str, oldest: datetime) -> bool:
    m = RUN_FILE.search(rel)
    return bool(m) and datetime.strptime(m.group(2), "%Y%m%d%H") < oldest


def remote_files(s3, bucket: str) -> dict[str, tuple[int, str]]:
    """{path under data/: (size, md5 or '')} for every object in the bucket."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for o in page.get("Contents", []):
            etag = o["ETag"].strip('"')
            out[o["Key"][len(PREFIX):]] = (o["Size"], "" if "-" in etag else etag)   # multipart ETags aren't MD5s
    return out


def same(local: Path, size: int, etag: str) -> bool:
    return local.exists() and local.stat().st_size == size and (not etag or md5(local) == etag)


def pull(s3, bucket: str, data_dir: Path, ccfg: dict, now: datetime) -> tuple[int, int]:
    """Download what's missing or different. Returns (downloaded, skipped as too old)."""
    oldest = now - timedelta(days=ccfg["pull_forecast_days"])
    got = old = 0
    for rel, (size, etag) in sorted(remote_files(s3, bucket).items()):
        if not wanted(rel, ccfg):
            continue
        if too_old(rel, oldest):
            old += 1
            continue
        dest = Path(data_dir) / rel
        if not same(dest, size, etag):
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, PREFIX + rel, str(dest))
            got += 1
    return got, old


def push(s3, bucket: str, data_dir: Path, ccfg: dict) -> int:
    """Upload what's new or changed. Returns the number uploaded. Never deletes anything in the bucket."""
    remote = remote_files(s3, bucket)
    sent = 0
    for f in sorted(Path(data_dir).rglob("*")):
        rel = f.relative_to(data_dir).as_posix()
        if not f.is_file() or not wanted(rel, ccfg):
            continue
        if rel in remote and same(f, *remote[rel]):
            continue
        s3.upload_file(str(f), bucket, PREFIX + rel)
        sent += 1
    return sent
