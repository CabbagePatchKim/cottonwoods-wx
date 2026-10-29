"""Offline tests for hole-filling (archive/fill.py, runner.fill_holes): synthetic runs, temp dirs, no network."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from cottonwoods.archive import fill
from cottonwoods.archive import forecasts as fa
from cottonwoods.archive import runner
from cottonwoods.archive.io import read_df

from helpers import make_cfg, fake_puller, fake_ensemble

INIT = datetime(2026, 11, 2, 12)


def _holey(base, field_at=None, skip_hour=None):
    """A puller that loses `field_at` = (column, lead hour) and/or a whole forecast hour, like a timed-out file."""
    def pull(init, fxx, points, save_dir=None):
        df = base(init, [f for f in fxx if f != skip_hour], points, save_dir)
        if field_at:
            df.loc[df["lead_hr"] == field_at[1], field_at[0]] = np.nan
        return df
    return pull


def _archive(cfg, root, puller, model="HRRR"):
    fa.archive_run(model, INIT, cfg, root=root, puller=puller)
    return read_df(fa.run_path(model, INIT, cfg, root))


def test_a_missing_field_is_filled_from_the_same_run_and_nothing_else_changes():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        old = _archive(cfg, root, _holey(fake_puller(), field_at=("t_700_k", 3)))
        assert fill.count_holes(old, "HRRR", INIT, cfg) == old["point"].nunique()
        status, got, left = fill.fill_run("HRRR", INIT, cfg, root=root, puller=fake_puller())
        new = read_df(fa.run_path("HRRR", INIT, cfg, root))
        assert (status, left) == ("filled", 0) and got == old["point"].nunique()
        assert new["t_700_k"].notna().all() and (new.loc[new["lead_hr"] == 3, "t_700_k"] == 268.0).all()
        same = old["lead_hr"] != 3
        pd.testing.assert_series_equal(old.loc[same, "snow_in"].reset_index(drop=True),
                                       new.loc[same.to_numpy(), "snow_in"].reset_index(drop=True))
        assert new["fetched_at"].iloc[0] == old["fetched_at"].iloc[0] and new["filled_values"].iloc[0] == got
        assert fill.fill_run("HRRR", INIT, cfg, root=root, puller=fake_puller())[0] == "complete"


def test_a_missing_step_is_split_back_only_when_its_precip_adds_up():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        old = _archive(cfg, root, _holey(fake_puller(), skip_hour=5))
        merged = old[(old["point"] == "alta") & (old["lead_hr"] == 6)]
        assert merged["qpf_mm"].iloc[0] == 2.0                 # hours 4-6 in one step
        assert fill.fill_run("HRRR", INIT, cfg, root=root, puller=fake_puller())[2] == 0
        new = read_df(fa.run_path("HRRR", INIT, cfg, root))
        alta = new[new["point"] == "alta"].set_index("lead_hr")
        assert alta.loc[5, "qpf_mm"] == 1.0 and alta.loc[6, "qpf_mm"] == 1.0 and len(alta) == 48
        assert abs(new["qpf_mm"].sum() - old["qpf_mm"].sum()) < 1e-9

        fa.archive_run("HRRR", INIT, cfg, root=root, force=True, puller=_holey(fake_puller(), skip_hour=5))

        def wetter(i, f, p, s=None):                          # hours 4-5 and 5-6 add to 3 mm, the archive has 2
            df = fake_puller()(i, f, p, s)
            df.loc[df["lead_hr"].isin([5, 6]), "qpf_mm"] = 1.5
            return df
        try:
            fill.fill_run("HRRR", INIT, cfg, root=root, puller=wetter)
            raise AssertionError("expected FillMismatch")
        except fill.FillMismatch as e:
            assert "add to" in str(e)


def test_a_pull_that_disagrees_with_the_archive_changes_nothing():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _archive(cfg, root, _holey(fake_puller(), field_at=("t_700_k", 3)))
        path = fa.run_path("HRRR", INIT, cfg, root)
        before = path.read_bytes()
        try:
            fill.fill_run("HRRR", INIT, cfg, root=root, puller=fake_puller(tK=270.0))
            raise AssertionError("expected FillMismatch")
        except fill.FillMismatch as e:
            assert "t_850_k" in str(e)
        assert path.read_bytes() == before


def test_ensemble_members_are_filled_per_member():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        full = fake_ensemble(31)

        def lose(i, f, p, s=None):
            df = full(i, f, p, s)
            df.loc[(df["member"] == 1) & (df["lead_hr"] == df["lead_hr"].min()), "qpf_mm"] = np.nan
            return df
        old = _archive(cfg, root, lose, model="GEFS")
        assert fill.count_holes(old, "GEFS", INIT, cfg) == old["point"].nunique()
        assert fill.fill_run("GEFS", INIT, cfg, root=root, puller=full)[0] == "filled"
        assert read_df(fa.run_path("GEFS", INIT, cfg, root))["qpf_mm"].notna().all()


def test_pass_fills_only_recent_runs_newest_first_and_stops_after_max_tries():
    cfg = make_cfg()
    cfg["archive"]["fill"] = {"max_age_hours": 72, "per_pass": 1, "max_tries": 2}
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for init in (INIT, INIT - timedelta(hours=6), INIT - timedelta(days=4)):
            fa.archive_run("HRRR", init, cfg, root=root, puller=_holey(fake_puller(), field_at=("t_700_k", 3)))
        tries = runner.FailureLog(root / "fills.csv")
        calls = []

        def filler(m, init, cfg, root=None, save_dir=None):
            calls.append(init)
            return "unchanged", 0, 6                          # the source still lacks the file
        now = INIT + timedelta(hours=3)
        for _ in range(3):
            runner.fill_holes(["HRRR"], cfg, now, tries, root=root, filler=filler, log=lambda *_: None)
        six = INIT - timedelta(hours=6)
        assert calls == [INIT, INIT, six]                     # newest first, one per pass, max_tries each, and
        assert tries.tries("HRRR", INIT) == 2                 # never the 4-day-old run

        n = runner.fill_holes(["HRRR"], cfg, now, runner.FailureLog(root / "f2.csv"), root=root,
                              filler=lambda m, i, c, root=None, save_dir=None: fill.fill_run(m, i, c, root=root,
                                                                                             puller=fake_puller()),
                              log=lambda *_: None)
        assert n["filled"] == 1 and fill.count_holes(read_df(fa.run_path("HRRR", INIT, cfg, root)), "HRRR", INIT, cfg) == 0
