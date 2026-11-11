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
