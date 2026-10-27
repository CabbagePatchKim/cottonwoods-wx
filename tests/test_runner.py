"""Tests for the archive pass logic (src/cottonwoods/archive/runner.py): no network, temp folders only."""
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from cottonwoods.archive import runner
from cottonwoods.config import load_config

NOW = datetime(2026, 10, 27, 6)


def test_pass_lock_blocks_a_second_pass_and_ignores_a_stale_one():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "x.lock"
        with runner.PassLock(path) as first:
            with runner.PassLock(path) as second:
                assert first and not second            # a live lock stops the second pass
            assert path.exists()                       # the blocked pass must not delete the holder's lock
        assert not path.exists()                       # released on exit
        path.write_text("old")
        old = time.time() - 7 * 3600
        os.utime(path, (old, old))
        with runner.PassLock(path) as got:
            assert got                                 # a 7-hour-old lock is from a crash: ignored


def test_build_jobs_default_is_newest_first_with_long_canada_catchup():
    cfg = load_config()
    jobs = runner.build_jobs(["HRRR", "RDPS", "GDPS"], cfg, NOW)
    assert jobs == sorted(jobs, key=lambda j: j[1], reverse=True)           # newest first across models
    per = pd.Series([m for m, _ in jobs]).value_counts()
    assert per["HRRR"] == cfg["archive"]["recent_cycles"]
    assert per["RDPS"] == cfg["archive"]["catchup_days"]["RDPS"] * 4         # 4 cycles a day
    assert per["GDPS"] == cfg["archive"]["catchup_days"]["GDPS"] * 2         # 00/12z only
    assert runner.build_jobs(["HRRR", "GFS"], cfg, NOW, init="2026-10-26T12") == \
        [("HRRR", datetime(2026, 10, 26, 12)), ("GFS", datetime(2026, 10, 26, 12))]
    rng = runner.build_jobs(["GFS"], cfg, NOW, start="2026-10-25", end="2026-10-25 23:00")
    assert [t.hour for _, t in rng] == [0, 6, 12, 18]


def test_failure_log_gives_up_only_on_old_runs_after_max_tries():
    with tempfile.TemporaryDirectory() as d:
        log = runner.FailureLog(Path(d) / "f.csv")
        old, new = datetime(2026, 10, 20, 0), datetime(2026, 10, 27, 0)
        for _ in range(runner.MAX_TRIES):
            log.failed("RDPS", old, "missing hour")
            log.failed("RDPS", new, "still posting")
        log.save()
        again = runner.FailureLog(Path(d) / "f.csv")            # survives between passes
        assert again.give_up("RDPS", old, NOW)                  # old and failed 4 times: skip it
        assert not again.give_up("RDPS", new, NOW)              # under a day old: keep trying
        again.succeeded("RDPS", old)
        assert again.tries("RDPS", old) == 0


def test_run_jobs_counts_and_keeps_going_after_a_failure():
    cfg = load_config()

    def fake_archive(model, init, cfg, force=False, save_dir=None):
        if model == "BAD":
            raise RuntimeError("boom")
        return Path("x"), "exists" if model == "OLD" else "archived"

    with tempfile.TemporaryDirectory() as d:
        log = runner.FailureLog(Path(d) / "f.csv")
        lines = []
        jobs = [("BAD", NOW), ("NEW", NOW), ("OLD", NOW)]
        n = runner.run_jobs(jobs, cfg, fake_archive, log, NOW, log=lines.append)
        assert n == {"archived": 1, "present": 1, "failed": 1, "given_up": 0}
        assert log.tries("BAD", NOW) == 1 and any("[fail]" in s for s in lines)


def test_split_streams_groups_models_by_server():
    groups = runner.split_streams(["HRRR", "IFS", "RDPS", "GEFS"])
    assert groups == {"noaa": ["HRRR", "GEFS"], "ecmwf": ["IFS"], "canada": ["RDPS"]}
    assert runner.split_streams(["HRRR"]) == {"noaa": ["HRRR"]}
