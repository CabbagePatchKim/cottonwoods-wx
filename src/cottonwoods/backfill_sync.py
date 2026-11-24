"""Share the verification backfill (data/local/backfill + the BUFKIT cache) between the owner's PCs through the private
bucket (owner 2026-11-23). Separate from bucket.py on purpose: it lives under its own bucket prefix (PREFIX), which the
cloud passes never list, pull or push, and nothing here touches data/archive or the live bucket files.

    push  this PC -> bucket: files that are new or changed here. Never overwrites a bucket file changed after the local
          copy was written. Safe while a backfill runs here (it changes no local file).
    pull  bucket -> this PC: files missing here, or changed in the bucket after the local copy was written. Not while a
          backfill runs here.
Nothing is ever deleted, on either side. The append-only logs (LOGS) are MERGED both ways, so a PC's lines are never
lost to the other's copy. A pulled file takes the bucket's time, so "changed after" means a real change since the pull.
"""
from __future__ import annotations

import fnmatch
import io
import os
import tempfile
from pathlib import Path

import pandas as pd

from .bucket import same

PREFIX = "backfill/"                       # NOT bucket.PREFIX ("data/")
DIRS = ("local/backfill", "local/bufkit")  # under data/
EXCLUDE = ("*.lock", "*.tmp")
LOGS = {"local/backfill/runs_log.csv": "logged", "local/backfill/archive/manifest.csv": "fetched_at"}  # time column


def wanted(rel: str) -> bool:
    """rel (under data/, '/' separated) is in a shared folder, isn't excluded, and can't climb out of it."""
    parts = rel.split("/")
    return any(rel.startswith(d + "/") for d in DIRS) and ".." not in parts and "\\" not in rel and ":" not in rel \
        and not any(fnmatch.fnmatch(rel, p) for p in EXCLUDE)


def remote_files(s3, bucket: str) -> dict[str, tuple[int, str, float]]:
    """{path under data/: (size, md5 or '', last modified)} of the shared backfill in the bucket."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for o in page.get("Contents", []):
            rel, etag = o["Key"][len(PREFIX):], o["ETag"].strip('"')
            if wanted(rel):
                out[rel] = (o["Size"], "" if "-" in etag else etag, o["LastModified"].timestamp())
    return out


def local_files(data_dir: Path) -> dict[str, Path]:
    out = {}
    for d in DIRS:
        for f in sorted((Path(data_dir) / d).rglob("*")):
            rel = f.relative_to(data_dir).as_posix()
            if f.is_file() and wanted(rel):
                out[rel] = f
    return out


def merge_log(a: bytes, b: bytes, by: str) -> bytes:
    """Two copies of an append-only CSV -> every row once, in time order (column `by`)."""
    frames = [pd.read_csv(io.BytesIO(x), dtype=str, keep_default_na=False) for x in (a, b) if x.strip()]
    if not frames:
        return b""
    both = pd.concat(frames, ignore_index=True).fillna("").drop_duplicates().sort_values(by, kind="stable")
    return both.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _get(s3, bucket: str, rel: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f"
        s3.download_file(bucket, PREFIX + rel, str(p))
        return p.read_bytes()


def plan(data_dir: Path, remote: dict) -> tuple[list[str], list[str]]:
    """(to push, to pull): what each direction would send. Logs that differ are in both: they get merged."""
    local = local_files(data_dir)
    up, down = [], []
    for rel in sorted(set(local) | set(remote)):
        if rel not in remote:
            up.append(rel)
        elif rel not in local:
            down.append(rel)
        elif not same(local[rel], *remote[rel][:2]):
            newer_there = remote[rel][2] > local[rel].stat().st_mtime
            if rel in LOGS or not newer_there:
                up.append(rel)
            if rel in LOGS or newer_there:
                down.append(rel)
    return up, down


def push(s3, bucket: str, data_dir: Path, log=print) -> int:
    """Upload what's new or changed here; returns the number of files sent."""
    remote = remote_files(s3, bucket)
    up, _ = plan(data_dir, remote)
    # run files first, the logs last: a log never names a run the bucket doesn't have yet
    for n, rel in enumerate(sorted(up, key=lambda r: r in LOGS), 1):
        path = Path(data_dir) / rel
        if rel in LOGS and rel in remote:
            merged = merge_log(_get(s3, bucket, rel), path.read_bytes(), LOGS[rel])
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "f").write_bytes(merged)
                s3.upload_file(str(Path(d) / "f"), bucket, PREFIX + rel)
        else:
            s3.upload_file(str(path), bucket, PREFIX + rel)
        if n % 200 == 0:
            log(f"[backfill-sync] {n} of {len(up)} sent")
    return len(up)


def pull(s3, bucket: str, data_dir: Path, log=print) -> int:
    """Download what's missing or newer in the bucket; returns the number of files fetched."""
    remote = remote_files(s3, bucket)
    _, down = plan(data_dir, remote)
    for n, rel in enumerate(sorted(down, key=lambda r: r in LOGS), 1):
        path = Path(data_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        if rel in LOGS and path.exists():
            tmp.write_bytes(merge_log(_get(s3, bucket, rel), path.read_bytes(), LOGS[rel]))
        else:
            s3.download_file(bucket, PREFIX + rel, str(tmp))
            os.utime(tmp, (remote[rel][2], remote[rel][2]))
        tmp.replace(path)                  # a stopped pull never leaves a half file
        if n % 200 == 0:
            log(f"[backfill-sync] {n} of {len(down)} fetched")
    return len(down)
