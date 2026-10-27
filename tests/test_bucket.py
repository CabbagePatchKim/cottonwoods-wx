"""The cloud archive sync (bucket.py), against an in-memory fake bucket."""
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

from cottonwoods import bucket

CCFG = {"dirs": ["archive", "processed"], "exclude": ["*.lock", "processed/golden/*"], "pull_forecast_days": 25}
NOW = datetime(2026, 11, 20)


class FakeS3:
    """The four calls bucket.py makes, storing objects in a dict. Counts uploads/downloads."""

    def __init__(self):
        self.objects, self.uploads, self.downloads = {}, 0, 0

    def get_paginator(self, _):
        fake = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v), "ETag": f'"{hashlib.md5(v).hexdigest()}"'}
                                    for k, v in fake.objects.items() if k.startswith(Prefix)]}
        return Pages()

    def upload_file(self, path, _bucket, key):
        self.objects[key] = Path(path).read_bytes()
        self.uploads += 1

    def download_file(self, _bucket, key, path):
        Path(path).write_bytes(self.objects[key])
        self.downloads += 1


def _write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_push_sends_only_new_or_changed_synced_files_and_never_deletes():
    s3 = FakeS3()
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        _write(data, "archive/storms.csv", "a")
        _write(data, "archive/forecasts/model=GFS/init=2026111900.parquet", "run")
        _write(data, "archive/archive_forecasts.lock", "123")         # excluded
        _write(data, "processed/golden/x.pkl", "g")                   # excluded
        _write(data, "raw/herbie/big.grib2", "grib")                  # not a synced folder
        assert bucket.push(s3, "b", data, CCFG) == 2
        assert set(s3.objects) == {"data/archive/storms.csv", "data/archive/forecasts/model=GFS/init=2026111900.parquet"}
        assert bucket.push(s3, "b", data, CCFG) == 0                  # nothing changed
        _write(data, "archive/storms.csv", "b")
        (data / "archive/forecasts/model=GFS/init=2026111900.parquet").unlink()
        assert bucket.push(s3, "b", data, CCFG) == 1                  # the change goes up...
        assert "data/archive/forecasts/model=GFS/init=2026111900.parquet" in s3.objects   # ...and nothing is deleted


def test_pull_fetches_missing_or_changed_files_but_skips_old_model_runs():
    s3 = FakeS3()
    s3.objects = {"data/archive/storms.csv": b"new",
                  "data/archive/forecasts/model=GFS/init=2026111900.parquet": b"recent",
                  "data/archive/forecasts/model=GFS/init=2026100100.parquet": b"old",   # 50 days before NOW
                  "data/archive/obs/snotel/2025.parquet": b"obs"}                         # obs: always pulled
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        _write(data, "archive/storms.csv", "old")
        assert bucket.pull(s3, "b", data, CCFG, NOW) == (3, 1)
        assert (data / "archive/storms.csv").read_text() == "new"
        assert not (data / "archive/forecasts/model=GFS/init=2026100100.parquet").exists()
        assert bucket.pull(s3, "b", data, CCFG, NOW) == (0, 1)        # already up to date
