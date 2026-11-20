"""Time-height features from BUFKIT point soundings (models/bufkit.py, timeheight.py)."""
from datetime import datetime

import numpy as np

from cottonwoods import timeheight
from cottonwoods.models import bufkit

from helpers import make_cfg

LEVELS = "PRES TMPC TMWC DWPC THTE DRCT SKNT OMEG\nCFRL HGHT\n"


def _hour(t: str, rows, cape=0.0) -> str:
    """One BUFKIT hour block; rows = (pres, tmpc, tmwc, dwpc, thte, drct, sknt, omeg, cfrl, hght)."""
    head = (f"STID = ALTA STNM = 298 TIME = {t}\nSLAT = 40.58 SLON = -111.63 SELV = 2929.0\nSTIM = 0\n\n"
            f"SHOW = -9999.00 LIFT = 8.52 CAPE = {cape:.2f}\nPWAT = 2.23\n\n")
    return head + LEVELS + "\n".join(" ".join(f"{v:.2f}" for v in r[:8]) + "\n" + " ".join(f"{v:.2f}" for v in r[8:])
                                     for r in rows) + "\n\n"


def _profile(t0_c, rh_moist_top_m, thetae, front_cold=0.0, dirn=200.0):
    """Levels every ~500 m from 2,930 m: T falls 6.5 C/km; saturated (Td = T) up to rh_moist_top_m, dry above."""
    rows = []
    for k in range(14):
        z = 2930.0 + 500 * k
        p = 710.0 * np.exp(-(z - 2930) / 8000)
        t = t0_c - front_cold - 6.5 * (z - 2930) / 1000
        td = t if z <= rh_moist_top_m else t - 20
        rows.append((p, t, (t + td) / 2, td, thetae(k), dirn, 30.0, -8.0 if t < -12 else 1.0, 0.0, z))
    return rows


def _text(hours) -> str:
    sfc = ("STN YYMMDD/HHMM PMSL PRES SKTC STC1 SNFL WTNS\nP01M C01M STC2 LCLD MCLD HCLD\n")
    rows = "".join(f"298 {t} 1015.30 710.70 -15.96 271.70 -9999.00 27.00\n{p:.2f} 0.00 271.90 65.00 0.00 0.00\n"
                   for t, p in hours)
    return ("SNPARM = PRES;TMPC;TMWC;DWPC;THTE;DRCT;SKNT;OMEG;CFRL;HGHT\nSTNPRM = SHOW;LIFT;CAPE;PWAT\n\n", sfc + rows)


def _file(blocks, precip):
    head, sfc = _text(precip)
    return head + "".join(blocks) + sfc


def test_bufkit_text_parses_into_levels_and_hours():
    stable = _profile(-5, 4500, lambda k: 300 + 2 * k)
    text = _file([_hour("261120/0000", stable), _hour("261120/0100", stable, cape=40)],
                 [("261120/0000", -9999.0), ("261120/0100", 2.5)])
    levels, hours = bufkit.parse(text)
    assert len(levels) == 28 and levels["time"].nunique() == 2
    assert abs(levels["hght"].iloc[1] - 3430) < 1e-9 and abs(levels["omeg"].iloc[0] - 1.0) < 1e-9
    assert hours["cape"].tolist() == [0.0, 40.0]
    assert np.isnan(hours["p01m"].iloc[0]) and abs(hours["p01m"].iloc[1] - 2.5) < 1e-9   # -9999 -> NaN
    cfg = make_cfg()
    u = bufkit.url("GFS", "alta", datetime(2025, 12, 4, 12), cfg)
    assert u.endswith("/2025/12/04/bufkit/12/gfs/gfs3_alta.buf"), u


def test_moist_layer_freezing_level_growth_zone_and_convective_flag():
    cfg = make_cfg()
    tc = cfg["timeheight"]
    # warm at the ground (+3 C): freezing level ~460 m up; saturated to 6,430 m (3,500 m deep); theta-e falls 1 K
    # per level (500 m) above the bottom km -> potentially unstable, deep enough: convective
    unstable = _profile(3, 6430, lambda k: 310 - max(0, k - 2) * 1.0)
    levels, _ = bufkit.parse(_file([_hour("261120/0000", unstable)], [("261120/0000", 0.0)]))
    f = timeheight.hour_features(levels, tc)
    assert abs(f["fzl_m"] - (2930 + 3 / 6.5 * 1000)) < 1.0 and not f["fzl_below_ground"]
    assert abs(f["moist_base_m"] - 2930) < 1e-9 and abs(f["moist_top_m"] - 6430) < 1e-9
    assert abs(f["moist_depth_m"] - 3500) < 1e-9 and f["moist_top_c"] < -18
    assert f["thetae_drop_k"] >= tc["conv_min_thetae_drop_k"] and f["convective"]
    assert f["dgz_sat_m"] > 0 and f["dgz_omega"] < 0                       # saturated, rising growth zone
    # the same moisture with theta-e rising with height: stable, not convective
    stable = _profile(3, 6430, lambda k: 300 + 2 * k)
    f2 = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", stable)], [("261120/0000", 0.0)]))[0], tc)
    assert f2["thetae_drop_k"] <= 0 and not f2["convective"]
    # a shallow moist layer (1,000 m) is never convective, however unstable
    shallow = _profile(3, 3930, lambda k: 310 - k * 3.0)
    f3 = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", shallow)], [("261120/0000", 0.0)]))[0], tc)
    assert f3["moist_depth_m"] < tc["conv_min_depth_m"] and not f3["convective"]
    # cold all the way down: freezing level stored as the model's ground, flagged below ground
    cold = _profile(-8, 4000, lambda k: 300 + 2 * k)
    f4 = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", cold)], [("261120/0000", 0.0)]))[0], tc)
    assert f4["fzl_below_ground"] and abs(f4["fzl_m"] - 2930) < 1e-9
    # a deep low: the model's ground at ~689 mb, 700 mb underground (HRRR at Alta, 2/17/2026): stability and the
    # front level are still measured (ground -> stab_top_hpa; front_hpa above the ground)
    low = [(r[0] * 0.97, *r[1:]) for r in unstable]
    f5 = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", low)], [("261120/0000", 0.0)]))[0], tc)
    assert np.isfinite(f5["thetae_sfc_top_k"]) and np.isfinite(f5["lapse_sfc_top"]) and np.isfinite(f5["t_front_c"])


def test_moist_layer_aloft_does_not_count_and_front_is_found():
    cfg = make_cfg()
    tc = cfg["timeheight"]
    # moist only from 5,000 m up (well above moist_base_max_agl_m): no moist layer for the point
    rows = _profile(0, 99999, lambda k: 300 + 2 * k)
    rows = [r if r[9] >= 5000 else (r[0], r[1], r[1] - 5, r[1] - 10, *r[4:]) for r in rows]
    f = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", rows)], [("261120/0000", 0.0)]))[0], tc)
    assert f["moist_depth_m"] == 0.0 and np.isnan(f["moist_base_m"])
    # 8 hours: 700 mb cools 4 C between hours 3 and 5 and the wind veers 200 -> 290: the front, at hour 5
    blocks, precip = [], []
    for h in range(8):
        cold = 0.0 if h < 3 else 2.0 if h == 4 else 4.0 if h >= 5 else 0.0
        dirn = 200.0 if h < 3 else 290.0
        t = f"261120/{h:02d}00"
        blocks.append(_hour(t, _profile(-5, 4500, lambda k: 300 + 2 * k, front_cold=cold, dirn=dirn)))
        precip.append((t, 1.0))
    feats = timeheight.features(*bufkit.parse(_file(blocks, precip)), cfg)
    assert feats["front"].sum() == 1 and feats.loc[feats["front"], "time"].iloc[0].hour == 5
    assert not feats.attrs["front_at_end"]
    assert abs(feats["p01m"].iloc[2] - 1.0) < 1e-9


def test_wind_across_north_is_not_turned_south():
    tc = make_cfg()["timeheight"]
    rows = _profile(-5, 4500, lambda k: 300 + 2 * k)
    # 350 deg below 650 mb, 10 deg above: interpolating directions gave ~180 (south); components give ~0 (north)
    rows = [(*r[:5], 350.0 if r[0] > 650 else 10.0, *r[6:]) for r in rows]
    f = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", rows)], [("261120/0000", 0.0)]))[0], tc)
    assert min(f["dir_front"], 360 - f["dir_front"]) < 10.5 and abs(f["kt_front"] - 30) < 1.0


def test_analyses_stack_first_hours_and_blocks_compare_observed_with_runs():
    import pandas as pd
    cfg = make_cfg()
    cfg["timeheight"]["block_hours"] = 3
    stable = _profile(-5, 4500, lambda k: 300 + 2 * k)
    # two hourly "RAP" runs, each with 2 hours: only each run's first hour is kept
    texts = [_file([_hour(f"261120/{h:02d}00", stable), _hour(f"261120/{h + 1:02d}00", stable)],
                   [(f"261120/{h:02d}00", 0.0)]) for h in (0, 1)]
    lv, hr = timeheight.first_hours(texts)
    assert sorted(lv["time"].unique()) == [pd.Timestamp("2026-11-20 00:00"), pd.Timestamp("2026-11-20 01:00")]
    assert timeheight.blocks(pd.Timestamp("2026-11-20 00:00"), pd.Timestamp("2026-11-20 07:00"), 3) == [
        (pd.Timestamp("2026-11-20 00:00"), pd.Timestamp("2026-11-20 03:00")),
        (pd.Timestamp("2026-11-20 03:00"), pd.Timestamp("2026-11-20 06:00")),
        (pd.Timestamp("2026-11-20 06:00"), pd.Timestamp("2026-11-20 07:00"))]
    # hourly features 01..06: convective in hours 01-03 only
    feats = pd.DataFrame({"time": pd.date_range("2026-11-20 01:00", periods=6, freq="h"),
                          "convective": [True] * 3 + [False] * 3, "moist_depth_m": [3000.0] * 3 + [500.0] * 3,
                          "moist_top_c": -20.0, "thetae_drop_k": [3.0] * 3 + [0.0] * 3, "thetae_sfc_top_k": 1.0,
                          "lapse_sfc_top": 7.0, "dgz_sat_m": 400.0, "dgz_omega": -5.0, "fzl_m": 2930.0,
                          "fzl_below_ground": True, "wbz_m": 2930.0, "t_front_c": -10.0, "front": False,
                          "cape": 0.0, "dir_front": [350.0, 10.0, 0.0, 300, 300, 300], "kt_front": 20.0})
    b1 = timeheight.block_features(feats, pd.Timestamp("2026-11-20 00:00"), pd.Timestamp("2026-11-20 03:00"), "x_")
    assert b1["x_hours"] == 3 and b1["x_conv_share"] == 1.0 and abs(b1["x_moist_depth_mean"] - 3000) < 1e-9
    assert min(b1["x_dir_front"], 360 - b1["x_dir_front"]) < 1e-6                  # circular mean of 350/10/0
    # a 6 h storm; observed 1 mm/h; a run covering only the first block, 2 mm/h: one run row there, a bare row after
    storm = pd.Series({"storm_id": "s1", "final_start": pd.Timestamp("2026-11-20 00:00"),
                       "final_end": pd.Timestamp("2026-11-20 06:00")})
    t = pd.date_range("2026-11-20 00:00", periods=6, freq="h")
    obs = pd.DataFrame({"valid_start": t, "valid_end": t + pd.Timedelta(hours=1), "qpf_mm": 1.0})
    run = obs.iloc[:3].assign(qpf_mm=2.0)
    rows = timeheight.storm_block_rows(storm, cfg, obs, feats,
                                       [{"model": "HRRR", "lead": 24, "init": t[0], "steps": run, "feats": feats}])
    assert len(rows) == 2 and rows[0]["model"] == "HRRR" and rows[1]["model"] is None
    assert abs(rows[0]["obs_in"] - 3 / 25.4) < 1e-9 and abs(rows[0]["fc_in"] - 6 / 25.4) < 1e-9
    assert rows[0]["an_conv_share"] == 1.0 and rows[0]["fc_conv_share"] == 1.0 and rows[1]["an_conv_share"] == 0.0


def test_overrunning_measures_the_dry_gap_under_a_cloud_aloft():
    tc = make_cfg()["timeheight"]
    # saturated only from 4,430 m up (1,500 m above the ground), 20 C dewpoint depression below: overrunning
    rows = _profile(-5, 99999, lambda k: 300 + 2 * k)
    rows = [r if r[9] >= 4430 else (r[0], r[1], r[1] - 5, r[1] - 20, *r[4:]) for r in rows]
    f = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", rows)], [("261120/0000", 0.0)]))[0], tc)
    assert abs(f["cloud_base_m"] - 4430) < 1e-9 and abs(f["subcloud_m"] - 1500) < 1e-9
    assert f["subcloud_rh"] < 30 and 1000 < f["subcloud_dry_m"] <= 1500          # very dry: most of the gap counts
    # cloud on the ground: no gap
    wet = _profile(-5, 6000, lambda k: 300 + 2 * k)
    g = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", wet)], [("261120/0000", 0.0)]))[0], tc)
    assert g["subcloud_m"] == 0.0 and g["subcloud_dry_m"] == 0.0


def test_spillover_cross_barrier_wind_froude_and_drift():
    tc = make_cfg()["timeheight"]
    # 30 kt (15.4 m/s) from the west, stable theta-e (+2 K per 500 m), saturated to 6,430 m (-26 C at the top)
    west = _profile(3, 6430, lambda k: 300 + 2 * k, dirn=270.0)
    f = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", west)], [("261120/0000", 0.0)]))[0], tc)
    assert abs(f["ucross_ms"] - 30 * 0.514444) < 1e-6 and f["n2"] > 0
    n = np.sqrt(f["n2"])
    assert abs(f["froude"] - f["ucross_ms"] / (n * tc["barrier_height_m"])) < 1e-9
    assert -18 <= f["gen_c"] <= -12                                               # made in the growth zone
    vf = tc["fall_speed_ms"]["cold"]
    assert abs(f["drift_km"] - f["ucross_ms"] * (f["gen_m"] - 2930) / vf / 1000) < 1e-6
    # wind from the east: nothing carried over to the back side; theta-e falling with height: unstable -> 99
    east = _profile(3, 6430, lambda k: 310 - 2 * k, dirn=90.0)
    e = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", east)], [("261120/0000", 0.0)]))[0], tc)
    assert e["ucross_ms"] < 0 and e["froude"] == 0.0 and e["drift_km"] == 0.0
    unstable = _profile(3, 6430, lambda k: 310 - 2 * k, dirn=270.0)
    u = timeheight.hour_features(bufkit.parse(_file([_hour("261120/0000", unstable)], [("261120/0000", 0.0)]))[0], tc)
    assert u["n2"] < 0 and u["froude"] == 99.0


def test_onset_time_error_and_pillow_gain():
    import pandas as pd
    cfg = make_cfg()
    t0 = pd.Timestamp("2026-11-20 00:00")
    hrs = pd.date_range(t0, periods=12, freq="h")
    # observed: dry 4 h, then 1.27 mm/h (0.05"/h): 0.1" reached 2 h into the wet part = 06:00
    obs = pd.DataFrame({"valid_start": hrs, "valid_end": hrs + pd.Timedelta(hours=1),
                        "qpf_mm": [0.0] * 4 + [1.27] * 8})
    assert timeheight.onset(obs, t0, t0 + pd.Timedelta(hours=12), 0.1) == pd.Timestamp("2026-11-20 06:00")
    # a 3-hourly run wet from the start, 2.54 mm per 3 h: 0.1" reached at the end of the first step (03:00)
    s3 = pd.date_range(t0, periods=4, freq="3h")
    run = pd.DataFrame({"valid_start": s3, "valid_end": s3 + pd.Timedelta(hours=3), "qpf_mm": 2.54})
    assert timeheight.onset(run, t0, t0 + pd.Timedelta(hours=12), 0.1) == pd.Timestamp("2026-11-20 03:00")
    assert timeheight.onset(run.assign(qpf_mm=0.0), t0, t0 + pd.Timedelta(hours=12), 0.1) is None
    storm = pd.Series({"storm_id": "s1", "final_start": t0 + pd.Timedelta(hours=cfg["timeheight"]["pad_hours"]),
                       "final_end": t0 + pd.Timedelta(hours=12)})
    row = timeheight.onset_row(storm, cfg, obs, pd.DataFrame(),
                               {"model": "GFS", "lead": 24, "init": t0, "steps": run, "feats": None})
    assert abs(row["onset_err_h"] - 3.0) < 1e-9                                  # model 3 h early
    # pillow: 10.0" before, 10.1 flicker, 11.5" after -> gained 1.5"
    p = pd.DataFrame({"site": "thaynes_canyon", "element": "WTEQ",
                      "time_utc": pd.to_datetime(["2026-11-19 22:00", "2026-11-19 23:00", "2026-11-20 00:00",
                                                  "2026-11-20 12:00", "2026-11-20 13:00", "2026-11-20 14:00"]),
                      "value": [10.0, 10.1, 10.0, 11.5, 11.4, 11.5]})
    assert abs(timeheight.pillow_gain(p, "thaynes_canyon", t0, t0 + pd.Timedelta(hours=12)) - 1.5) < 1e-9
    assert np.isnan(timeheight.pillow_gain(p, "brighton", t0, t0 + pd.Timedelta(hours=12)))
