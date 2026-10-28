"""Daily-window verification (verify.py): pairing forecasts with observed windows."""
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from cottonwoods.archive import forecasts as fa
from cottonwoods.archive import obs as oa
from cottonwoods.verify import build_pairs, obs_sites, summarize

from helpers import make_cfg, fake_puller, gauge_frame, build_world


def test_verification_pairs_qpf_with_swe_change():
    cfg, fc, obs = build_world()
    manual = pd.DataFrame({"date": [pd.Timestamp("2026-11-03").date()], "site": ["alta"], "new_snow_in": [12.0],
                           "swe_in": [np.nan], "notes": [""]})
    pairs = build_pairs(fc, obs, cfg, manual)
    p = pairs[pairs.point == "alta"]
    assert len(p) == 3 and set(p.site) == {"atwater", "alta_collins", "alta_noaa"}  # one 24 h window per obs site
    r = p[p.site == "atwater"].iloc[0]
    assert r.obs_element == "PREC"                        # Atwater is scored on its gauge, not its pillow
    assert r.window_end == pd.Timestamp("2026-11-03 12:00") and r.lead_hr == 36 and r.lead_bin == 24
    assert abs(r.qpf_in - 24 / 25.4) < 1e-9
    assert abs(r.swe_obs_in - 0.05 * 24) < 1e-9           # SNOTEL rises 0.05"/h
    assert abs(r.snow_in_model - (24 / 25.4) * (12 + (271.16 - 268.0))) < 1e-9
    assert r.new_snow_obs_in == 12.0
    c = p[p.site == "alta_collins"].iloc[0]
    assert c.obs_element == "PREC" and abs(c.swe_obs_in - 0.04 * 24) < 1e-9  # gauge rises 0.04"/h
    s = summarize(pairs, cfg)
    assert set(s.site) == {"atwater", "alta_collins"}  # scored separately, not pooled (no NOAA data in this world)
    row = s[(s.lead_bin == 24) & (s.site == "atwater")].iloc[0]
    assert abs(row.qpf_bias_ratio - round((24 / 25.4) / 1.2, 2)) < 1e-9
    assert row.pod == 1.0 and row.far == 0.0 and row.n_snow_obs == 1


def test_points_without_matching_obs_still_pair_with_nan():
    cfg, fc, obs = build_world()
    pairs = build_pairs(fc, obs, cfg)
    sb = pairs[pairs.point == "snowbird"]
    assert len(sb) == 1 and sb["swe_obs_in"].isna().all()
    assert summarize(sb, cfg).empty                        # nothing to score without observations


def test_window_straddling_a_model_step_is_dropped():
    cfg, fc, obs = build_world(step_hours=3, hour_override=13)  # 13 UTC is not on a 3 h step edge
    assert build_pairs(fc, obs, cfg).empty
    cfg2, fc2, obs2 = build_world(step_hours=3, hour_override=12)
    assert len(build_pairs(fc2, obs2, cfg2)) >= 1


def test_gauge_reset_counts_only_positive_steps():
    cfg = make_cfg()
    cfg["points"]["alta"]["obs"] = ["alta_collins"]
    init = datetime(2026, 11, 2, 0)
    fxx = list(range(1, 49))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        fa.archive_run("HRRR", init, cfg, root=root, puller=lambda i, f, p, s=None: fake_puller()(i, fxx, p, s))
        # gauge emptied mid-window (window is 11-03 12z back to 11-02 12z; hour 20 = 11-03 03z)
        oa.archive_synoptic(cfg, "2026-11-02", "2026-11-05", root=root,
                            fetcher=lambda *a, **k: gauge_frame("CLN", "2026-11-02 07:00", 60, reset_at=20))
        pairs = build_pairs(fa.load_forecasts(cfg, root=root), oa.load_obs(root=root), cfg)
    r = pairs[pairs.point == "alta"].iloc[0]
    assert abs(r.swe_obs_in - 0.04 * 23) < 1e-9  # 24 hourly steps minus the one reset step


def test_noaa_site_pairs_on_its_own_6pm_window_with_snowfall():
    cfg = make_cfg()
    init = datetime(2026, 11, 2, 0)
    fxx = list(range(1, 49))
    noaa = pd.DataFrame({"station": "USC00420072", "element": ["PRCP", "SNOW"],
                         "time_utc": pd.Timestamp("2026-11-03 01:00"), "value": [0.9, 11.0], "trace": False})
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        fa.archive_run("HRRR", init, cfg, root=root, puller=lambda i, f, p, s=None: fake_puller()(i, fxx, p, s))
        oa.archive_ghcn(cfg, "2026-11-01", "2026-11-05", root=root, fetcher=lambda *a, **k: noaa)
        pairs = build_pairs(fa.load_forecasts(cfg, root=root), oa.load_obs(root=root), cfg)
    r = pairs[pairs.site == "alta_noaa"]
    assert len(r) == 1 and r.iloc[0].window_end == pd.Timestamp("2026-11-03 01:00")   # 6 pm MST, not 5 am
    assert r.iloc[0].swe_obs_in == 0.9 and r.iloc[0].new_snow_obs_in == 11.0
    assert abs(r.iloc[0].qpf_in - 24 / 25.4) < 1e-9


def test_solitude_and_brighton_are_scored_at_their_snotel_sites_not_mid_mountain():
    cfg = make_cfg()
    assert obs_sites(cfg, "solitude") == [] and obs_sites(cfg, "brighton") == []
    assert obs_sites(cfg, "solitude_snotel") == [("mill_d_north", "PREC", "snotel")]   # gauge, not pillow
    assert obs_sites(cfg, "brighton_snotel") == [("brighton", "WTEQ", "snotel")]
    for p, site in [("solitude_snotel", "mill_d_north"), ("brighton_snotel", "brighton")]:   # at the gauge itself
        assert all(cfg["points"][p][k] == cfg["snotel"][site][k] for k in ("lat", "lon", "elev_ft"))


def test_gauge_sites_score_rain_a_snow_pillow_never_sees():
    # Early season: rain on bare ground. The pillow stays at 0, the gauge catches 0.6"; Atwater is scored on its gauge.
    from cottonwoods.verify import _window_change
    cfg = make_cfg()
    assert obs_sites(cfg, "alta")[0] == ("atwater", "PREC", "snotel")
    assert obs_sites(cfg, "snowbird") == [("snowbird", "WTEQ", "snotel")]      # not listed: keeps the pillow
    t = pd.date_range("2026-10-20 00:00", periods=49, freq="h")
    gauge = pd.Series(8.8 + np.clip(np.arange(49) - 30, 0, 6) * 0.1, index=t)    # 6 wet hours, 0.1" each
    pillow = pd.Series(0.0, index=t)
    s, e = pd.Timestamp("2026-10-21 00:00"), pd.Timestamp("2026-10-22 00:00")
    assert abs(_window_change(gauge, s, e, "PREC", 1, cfg["gauge"]) - 0.6) < 1e-9
    assert _window_change(pillow, s, e, "WTEQ", 1, cfg["gauge"]) == 0.0
