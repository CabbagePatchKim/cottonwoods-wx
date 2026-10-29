"""Keep the archive in a cloud bucket (Cloudflare R2, S3-compatible), so a pass can run on a fresh machine
(GitHub Actions) and the laptop can fetch the same data for development.

    pull  bucket -> data/: only files that are missing locally or different. Model runs older than
          cloud.pull_forecast_days, and pattern maps older than cloud.pull_pattern_days, are skipped (a pass never
          reads them), so a fresh machine stays fast.
          A local file NEWER than the bucket's copy is never overwritten (the laptop may have archived more since
          the last push); it's counted as "kept".
    push  data/ -> bucket: only files that are new or changed. NOTHING is ever deleted from the bucket, and a
          bucket file changed AFTER the local copy was written is never overwritten (2026-10-29: a pass that pulled
          before the owner's Collins entry arrived pushed its stale copy back over it at the end of the pass).

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
RUN_FILE = re.compile(r"(^|/)(forecasts|patterns)/model=[^/]+/init=(\d{10})\.\w+$")


ENV_NAMES = ("CWX_BUCKET", "CWX_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
ENDPOINT_RE = re.compile(r"^https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com/?$")


def check_env(env=os.environ) -> list[str]:
    """Problems with the four settings, by NAME only (values are secrets and are never shown)."""
    problems = [f"{n} is not set" for n in ENV_NAMES if not env.get(n, "").strip()]
    ep = env.get("CWX_S3_ENDPOINT", "").strip()
    if ep and not ENDPOINT_RE.match(ep):
        problems.append("CWX_S3_ENDPOINT (GitHub secret R2_ENDPOINT) doesn't look like "
                        "https://<32-character account id>.r2.cloudflarestorage.com (no < > brackets, no spaces)")
    return problems


def load_env_file(path: Path) -> None:
    """NAME=value lines (e.g. ~/.cwx_r2.env) into the environment, for anything not already set there."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def client():
    import boto3   # only needed when syncing; the rest of the project doesn't depend on it
    return boto3.client("s3", endpoint_url=os.environ["CWX_S3_ENDPOINT"].strip(), region_name="auto",
                        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"].strip(),
                        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"].strip())


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


def too_old(rel: str, oldest: datetime, oldest_pattern: datetime | None = None) -> bool:
    """A model run (or pattern map, with its own oldest_pattern) from before the pull window."""
    m = RUN_FILE.search(rel)
    if not m:
        return False
    limit = oldest_pattern if m.group(2) == "patterns" and oldest_pattern is not None else oldest
    return datetime.strptime(m.group(3), "%Y%m%d%H") < limit


def remote_files(s3, bucket: str) -> dict[str, tuple[int, str, float]]:
    """{path under data/: (size, md5 or '', last modified as a Unix time)} for every object in the bucket."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for o in page.get("Contents", []):
            etag = o["ETag"].strip('"')
            out[o["Key"][len(PREFIX):]] = (o["Size"], "" if "-" in etag else etag,   # multipart ETags aren't MD5s
                                           o["LastModified"].timestamp())
    return out


def same(local: Path, size: int, etag: str) -> bool:
    return local.exists() and local.stat().st_size == size and (not etag or md5(local) == etag)


def pull(s3, bucket: str, data_dir: Path, ccfg: dict, now: datetime) -> tuple[int, int, int]:
    """Download what's missing or different. Returns (downloaded, skipped as too old, kept because the local
    file is newer)."""
    oldest = now - timedelta(days=ccfg["pull_forecast_days"])
    oldest_pattern = now - timedelta(days=ccfg.get("pull_pattern_days", ccfg["pull_forecast_days"]))
    got = old = kept = 0
    for rel, (size, etag, modified) in sorted(remote_files(s3, bucket).items()):
        if not wanted(rel, ccfg):
            continue
        if too_old(rel, oldest, oldest_pattern):
            old += 1
            continue
        dest = Path(data_dir) / rel
        if same(dest, size, etag):
            continue
        if dest.exists() and dest.stat().st_mtime > modified:
            kept += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, PREFIX + rel, str(dest))
        got += 1
    return got, old, kept


def push(s3, bucket: str, data_dir: Path, ccfg: dict, only: list[str] | None = None, log=print) -> int:
    """Upload what's new or changed. Returns the number uploaded. Never deletes anything in the bucket, and keeps a
    bucket file that changed after the local file was last written (someone else's newer copy).
    only: paths under data/ (e.g. ["processed/regime_factors.csv"]) to upload just those files. Use it from the
    laptop once the cloud runs the passes, so the laptop's older archive files never overwrite the cloud's. Files
    named this way are sent on purpose (send_entries.bat), so they go up even if the bucket's copy is newer."""
    remote = remote_files(s3, bucket)
    sent = 0
    for f in sorted(Path(data_dir).rglob("*")):
        rel = f.relative_to(data_dir).as_posix()
        if not f.is_file() or not wanted(rel, ccfg) or (only is not None and rel not in only):
            continue
        if rel in remote and same(f, *remote[rel][:2]):
            continue
        if only is None and rel in remote and remote[rel][2] > f.stat().st_mtime:
            log(f"[bucket] kept the bucket's newer {rel} (changed there after this copy was written)")
            continue
        s3.upload_file(str(f), bucket, PREFIX + rel)
        sent += 1
    return sent
