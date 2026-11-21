"""The verification backfill (backfill.py): past storms found by the live rules, with their observed totals."""
import pandas as pd

from cottonwoods import backfill
from helpers import make_cfg


def _gauge(site, start, values):
    t = pd.date_range(start, periods=len(values), freq="h")
    return pd.DataFrame({"site": site, "element": "PREC", "time_utc": t, "value": values})


def test_past_storms_use_the_points_own_gauge_else_the_fallback_and_each_winter_alone():
    cfg = make_cfg()
    rain = [10.0] * 5 + [10.2, 10.4, 10.6, 10.8, 11.0] + [11.0] * 20        # a 1.0" storm, then dry
    old = _gauge("snowbird", "2019-01-10 00:00", rain)                        # before Atwater existed
    new = pd.concat([_gauge("atwater", "2024-01-10 00:00", rain),
                     _gauge("snowbird", "2024-01-10 00:00", [v + 5 for v in rain])])
    # next winter, after a summer gap, the gauge holds the summer's rain too (11.0 -> 11.8): never counted as a storm
    reset = _gauge("atwater", "2024-10-05 00:00", [11.8] * 24)
    st = backfill.observed_storms(pd.concat([old, new, reset]), cfg, "alta")
    assert list(st["gauge"]) == ["snowbird", "atwater"]                       # own gauge wins when it reports
    assert list(st["storm_id"]) == ["2019011004", "2024011004"]   # the first wet hour starts 04:00
    assert all(abs(w - 1.0) < 1e-9 for w in st["obs_water_in"])


def test_past_storms_get_their_nws_and_journal_totals():
    cfg = make_cfg()
    st = pd.DataFrame({"storm_id": ["2023120113"], "point": "alta", "final_start": [pd.Timestamp("2023-12-01 13:00")],
                       "final_end": [pd.Timestamp("2023-12-04 06:00")], "hours": [65.0], "obs_water_in": [4.5],
                       "gauge": "atwater"})
    totals = pd.DataFrame({"site": ["alta"], "source": "nws statement", "notes": "",
                           "start_utc": [pd.NaT], "end_utc": [pd.Timestamp("2023-12-04 11:25")],
                           "issued_utc": [pd.Timestamp("2023-12-04 17:18")], "new_snow_in": [50.0], "water_in": [4.66]})
    journal = pd.DataFrame({"storm_id": ["2023-12-01"], "start_date": ["2023-12-01"], "end_date": ["2023-12-03"],
                            "actual_in": [49.0]})
    out = backfill.attach_totals(st, totals, journal, cfg)
    assert out["nws_snow_in"].iloc[0] == 50.0 and out["journal_snow_in"].iloc[0] == 49.0


def test_backfill_plans_each_models_runs_before_a_storm_only_up_to_its_end():
    cfg = make_cfg()
    cfg["backfill"].update(models=["GFS", "HRRR"], leads_hours=[24, 48, 72], min_water_in=0.5)
    found = pd.DataFrame({"storm_id": ["2026021706", "2026030100"], "point": "alta",
                          "final_start": pd.to_datetime(["2026-02-17 06:00", "2026-03-01 00:00"]),
                          "final_end": pd.to_datetime(["2026-02-19 12:00", "2026-03-01 06:00"]),
                          "obs_water_in": [3.9, 0.2]})                    # the second is too small to fetch
    plan = backfill.plan_runs(found, cfg)
    gfs = plan[plan["model"] == "GFS"]
    assert list(gfs["init"]) == list(pd.to_datetime(["2026-02-14 06:00", "2026-02-15 06:00", "2026-02-16 06:00"]))
    assert list(gfs["max_lead_hours"]) == [126.0, 102.0, 78.0]          # each to the storm's end, not 384 h
    hrrr = plan[plan["model"] == "HRRR"]                                 # 48 h runs: only the 24 h one reaches it
    assert list(hrrr["init"]) == [pd.Timestamp("2026-02-16 06:00")] and hrrr["max_lead_hours"].iloc[0] == 48
    assert set(plan["storms"]) == {"2026021706"}


def test_backfill_plans_use_each_runs_own_length_and_scoring_leaves_ensembles_out():
    # 2026-11-19: the run's length came from the storm START's hour: an IFS 12z run (360 h) took the 06z length
    # (144 h) for a storm starting at 14z, and ENS (no runs at 14z) had no length at all
    cfg = make_cfg()
    cfg["backfill"].update(models=["IFS", "ENS"], leads_hours=[72], min_water_in=0.5, first_init={})
    found = pd.DataFrame({"storm_id": ["s"], "point": "alta", "final_start": [pd.Timestamp("2025-01-04 14:00")],
                          "final_end": [pd.Timestamp("2025-01-08 06:00")], "obs_water_in": [2.0]})
    plan = backfill.plan_runs(found, cfg).set_index("model")
    assert plan.loc["IFS", "init"] == pd.Timestamp("2025-01-01 12:00")
    assert plan.loc["IFS", "max_lead_hours"] == 162.0                      # to the storm's end, past 144 h
    assert plan.loc["ENS", "max_lead_hours"] == 162.0
    runs = backfill.runs_by_lead(found.iloc[0], cfg)
    assert [m for m, _, _ in runs] == ["IFS"]                              # an ensemble's members aren't one total
    assert {m for m, _, _ in backfill.runs_by_lead(found.iloc[0], cfg, ensembles=True)} == {"IFS", "ENS"}


def test_a_backfill_run_fetches_only_the_hours_it_needs():
    import tempfile
    from datetime import datetime
    from pathlib import Path
    from cottonwoods.archive import forecasts as fa
    from helpers import fake_puller
    cfg, asked = make_cfg(), []

    def pull(init, fxx, points, save_dir=None):
        asked.append(list(fxx))
        return fake_puller()(init, fxx, points, save_dir)
    with tempfile.TemporaryDirectory() as d:
        fa.archive_run("HRRR", datetime(2026, 2, 16, 6), cfg, root=Path(d), puller=pull, max_lead_hours=30)
    assert asked == [list(range(1, 31))]


def test_backfill_gfs_comes_from_aws_first_and_falls_back_to_gdex_while_live_stays_gdex_first():
    from cottonwoods.models import gfs, gfs_gdex, registry
    cfg = make_cfg()
    assert registry.get_puller("GFS", cfg).keywords["aws_first"] is False          # live passes: unchanged
    cfg["archive"]["gfs_aws_first"] = True
    assert registry.get_puller("GFS", cfg).keywords["aws_first"] is True
    saved, calls = (gfs._pull_run_aws, gfs_gdex.pull_run), []

    def aws(*a, **k):
        calls.append("aws")
        raise RuntimeError("no such run on AWS (before 2021)")

    def gdex(*a, **k):
        calls.append("gdex")
        return pd.DataFrame({"qpf_mm": [1.0]})
    gfs._pull_run_aws, gfs_gdex.pull_run = aws, gdex
    try:
        out = gfs.pull_run(None, [6], {}, workers=1, gdex_workers=1, aws_first=True)
    finally:
        gfs._pull_run_aws, gfs_gdex.pull_run = saved
    assert calls == ["aws", "gdex"] and len(out) == 1


def test_live_gfs_falls_back_to_aws_when_gdex_refuses():
    """GDEX started answering 403 Forbidden (11/19 18z): live GFS must go to AWS, not fail."""
    import requests
    from cottonwoods.models import gfs, gfs_gdex
    saved, calls = (gfs._pull_run_aws, gfs_gdex.pull_run), []

    def gdex(*a, **k):
        calls.append("gdex")
        raise requests.HTTPError("403 Client Error: Forbidden")

    def aws(*a, **k):
        calls.append("aws")
        return pd.DataFrame({"qpf_mm": [1.0]})
    gfs._pull_run_aws, gfs_gdex.pull_run = aws, gdex
    try:
        out = gfs.pull_run(None, [6], {}, workers=1, gdex_workers=1)
    finally:
        gfs._pull_run_aws, gfs_gdex.pull_run = saved
    assert calls == ["gdex", "aws"] and len(out) == 1
