"""Sharing the backfill between PCs (backfill_sync.py), against an in-memory fake bucket."""
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cottonwoods import backfill_sync as bs
from cottonwoods import bucket

RUN = "local/backfill/archive/forecasts/model=GEFS/init=2025010100.parquet"
LOG = "local/backfill/runs_log.csv"
HEAD = "logged,model,status\n"


class FakeS3:
    def __init__(self):
        self.objects, self.times, self.clock = {}, {}, 1_800_000_000

    def get_paginator(self, _):
        fake = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v), "ETag": f'"{hashlib.md5(v).hexdigest()}"',
                                     "LastModified": datetime.fromtimestamp(fake.times[k], timezone.utc)}
                                    for k, v in fake.objects.items() if k.startswith(Prefix)]}
        return Pages()

    def upload_file(self, path, _bucket, key):
        self.clock += 100
        self.objects[key], self.times[key] = Path(path).read_bytes(), self.clock

    def download_file(self, _bucket, key, path):
        Path(path).write_bytes(self.objects[key])


def _write(root, rel, text, mtime):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())
    os.utime(p, (mtime, mtime))


def test_backfill_goes_to_the_other_pc_and_stays_out_of_the_live_prefix():
    s3, t = FakeS3(), 1_800_000_000
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        _write(a, RUN, "run", t)
        _write(a, "local/bufkit/GFS/alta_1.buf", "buf", t)
        _write(a, "local/backfill/archive/manifest.lock", "x", t)          # excluded
        _write(a, "local/backfill/half.parquet.tmp", "x", t)               # excluded
        _write(a, "local/regime_bin_scores.csv", "x", t)                   # not shared
        _write(a, "archive/storms.csv", "live", t)                         # the live archive: never
        assert bs.push(s3, "b", Path(a)) == 2
        assert set(s3.objects) == {bs.PREFIX + RUN, bs.PREFIX + "local/bufkit/GFS/alta_1.buf"}
        assert bucket.remote_files(s3, "b") == {}                          # the cloud passes see nothing of it
        assert bs.pull(s3, "b", Path(b)) == 2 and (Path(b) / RUN).read_text() == "run"
        assert bs.plan(Path(b), bs.remote_files(s3, "b")) == ([], [])      # in step
        assert not list(Path(b).rglob("*.tmp"))


def test_backfill_newer_copy_wins_both_ways_and_nothing_is_deleted():
    s3, t = FakeS3(), 1_800_000_000
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        _write(a, RUN, "one", t)
        bs.push(s3, "b", Path(a))
        bs.pull(s3, "b", Path(b))
        _write(b, RUN, "two", s3.clock + 50)                               # re-fetched on b
        assert bs.pull(s3, "b", Path(b)) == 0                              # b's newer copy is kept
        assert bs.push(s3, "b", Path(b)) == 1
        assert bs.push(s3, "b", Path(a)) == 0                              # a's older copy never goes back over it
        assert bs.pull(s3, "b", Path(a)) == 1 and (Path(a) / RUN).read_text() == "two"
        (Path(a) / RUN).unlink()
        assert bs.push(s3, "b", Path(a)) == 0 and bs.PREFIX + RUN in s3.objects


def test_backfill_logs_merge_instead_of_overwriting():
    s3, t = FakeS3(), 1_800_000_000
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        _write(a, LOG, HEAD + "2026-11-21 01:00,ENS,archived\n", t)
        bs.push(s3, "b", Path(a))
        bs.pull(s3, "b", Path(b))
        _write(a, LOG, HEAD + "2026-11-21 01:00,ENS,archived\n2026-11-23 09:00,GFS,\"failed: x, y\"\n", t + 5)
        _write(b, LOG, HEAD + "2026-11-21 01:00,ENS,archived\n2026-11-22 03:00,GEFS,archived\n", t + 9_000)
        bs.push(s3, "b", Path(b))
        bs.push(s3, "b", Path(a))                                          # older file, but its line still gets in
        bs.pull(s3, "b", Path(b))
        bs.pull(s3, "b", Path(a))
        want = (HEAD + "2026-11-21 01:00,ENS,archived\n2026-11-22 03:00,GEFS,archived\n"
                "2026-11-23 09:00,GFS,\"failed: x, y\"\n")
        assert (Path(a) / LOG).read_text() == want and (Path(b) / LOG).read_text() == want


def test_backfill_sync_refuses_paths_outside_its_folders():
    assert bs.wanted(RUN) and bs.wanted(LOG)
    for bad in ("local/backfill/../../archive/storms.csv", "archive/storms.csv", "local/backfillx/a.csv",
                "local/backfill/..\\..\\a", "local/backfill/C:/a"):
        assert not bs.wanted(bad), bad
