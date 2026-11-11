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
