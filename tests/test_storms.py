"""Storm events (storms.py), window totals (windows.py) and report staleness (report.py)."""
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cottonwoods.archive import forecasts as fa

from cottonwoods.config import load_config
from helpers import make_cfg, hourly, snotel_prec


def test_twelve_hour_gap_keeps_lake_effect_bands_but_splits_separate_storms():
    from cottonwoods.storms import detect_events
    front = [2.0] * 10
    lake = [0.0] * 8 + [0.5] * 4            # 8 h lull, then backside bands: same storm
    later = [0.0] * 13 + [1.0] * 6          # 13 h dry, then rain again: a new storm
    ev = detect_events(hourly("2026-11-02 00:00", front + lake + later), 0.254, 12, 2.54)
    assert len(ev) == 2
    assert ev.start[0] == pd.Timestamp("2026-11-02 00:00") and ev.end[0] == pd.Timestamp("2026-11-02 22:00")
    assert abs(ev.total_mm[0] - 22.0) < 1e-9 and ev.start[1] == pd.Timestamp("2026-11-03 11:00")
    tiny = detect_events(hourly("2026-11-02", [0.3, 0.3]), 0.254, 12, 2.54)
    assert tiny.empty                       # 0.6 mm total is under 0.10": not a storm


def test_storm_window_is_the_longest_model_event_and_notes_outliers():
    from cottonwoods.storms import storm_windows
    ev = pd.DataFrame({"model": ["GFS", "IFS", "NAM"], "init": pd.Timestamp("2026-11-01"),
                       "start": pd.to_datetime(["2026-11-02 06:00", "2026-11-02 00:00", "2026-11-01 21:00"]),
                       "end": pd.to_datetime(["2026-11-03 00:00", "2026-11-03 06:00", "2026-11-02 03:00"]),
                       "total_mm": [10.0, 20.0, 3.0]})
    w = storm_windows(ev)
    assert len(w) == 1 and w[0]["defining_model"] == "IFS"       # 30 h beats GFS 18 h and NAM 6 h
    assert w[0]["fc_start"] == pd.Timestamp("2026-11-02 00:00") and "NAM starts 3 h earlier" in w[0]["outside_note"]


def test_window_total_prorates_steps_across_the_edge():
    from cottonwoods.storms import window_total
    steps = pd.DataFrame({"valid_start": pd.to_datetime(["2026-11-02 00:00", "2026-11-02 06:00"]),
                          "valid_end": pd.to_datetime(["2026-11-02 06:00", "2026-11-02 12:00"]), "qpf_mm": [6.0, 12.0]})
    assert window_total(steps, pd.Timestamp("2026-11-02 03:00"), pd.Timestamp("2026-11-02 09:00")) == 3.0 + 6.0


def test_storm_verifies_only_after_twelve_dry_observed_hours_and_widens_window():
    from cottonwoods import storms
    cfg = make_cfg()
    init = datetime(2026, 11, 1, 12)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # model: 1 mm/h from 11/02 00z to 11/02 12z
        def pull(i, fxx, points, s=None):
            rows = []
            for f in fxx:
                vs = pd.Timestamp(i) + pd.Timedelta(hours=f - 1)
                for name in points:
                    rows.append({"model": "HRRR", "init": pd.Timestamp(i), "lead_hr": f, "valid_start": vs,
                                 "valid_end": vs + pd.Timedelta(hours=1), "point": name, "grid_dist_km": 1.0,
                                 "qpf_mm": 1.0 if pd.Timestamp("2026-11-02") <= vs < pd.Timestamp("2026-11-02 12:00") else 0.0,
                                 **{f"t_{lev}_k": 265.0 for lev in (700, 600, 500)}})
            return pd.DataFrame(rows)
        fa.archive_run("HRRR", init, cfg, root=root, puller=pull)
        fc = fa.load_forecasts(cfg, root=root)
        # observed: snow 11/02 02z-16z (0.1"/h), i.e. it ends 4 h later than forecast
        wet = [0.0] * 14 + [0.1] * 14
        obs_short = snotel_prec("2026-11-01 12:00", wet + [0.0] * 6)       # only 6 dry hours so far
        t1 = storms.run(fc, obs_short, cfg, pd.Timestamp("2026-11-02 22:00"), root=root)
        assert (t1.status == "forecast").all()                              # not dry for 12 h yet: wait
        obs_done = snotel_prec("2026-11-01 12:00", wet + [0.0] * 13)
        t2 = storms.run(fc, obs_done, cfg, pd.Timestamp("2026-11-03 06:00"), root=root)
        st = t2.iloc[0]
        assert st.status == "verified" and st.final_start == pd.Timestamp("2026-11-02 00:00")
        assert st.final_end == pd.Timestamp("2026-11-02 16:00")             # widened to the observed end
        assert abs(st.obs_water_in - 1.4) < 1e-9
        sc = pd.read_csv(root / "storm_scores.csv")
        assert len(sc) == 1 and abs(sc.fc_water_in[0] - 12 / 25.4) < 1e-9


def test_staleness_lists_only_models_with_missing_due_runs():
    from cottonwoods.report import staleness
    cfg = make_cfg()
    cfg["archive"]["models"] = ["HRRR", "GDPS"]
    now = datetime(2026, 11, 3, 12)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # every HRRR run due in the last 2 days archived (fake empty files are enough: it checks names only)
        from cottonwoods.models.registry import recent_cycles
        for t in recent_cycles("HRRR", cfg, now - pd.Timedelta(hours=4), 8):
            p = fa.run_path("HRRR", t, cfg, root); p.parent.mkdir(parents=True, exist_ok=True); p.touch()
        w = staleness(cfg, now, root=root)
    assert len(w) == 1 and w[0].startswith("WARNING GDPS: 8 of its last 8 runs missing")


# --- storm life cycle: dropped / merged / split / periods / parts ---------------------------------

def run_steps(model, init, wet, hours=120, point="alta"):
    """Hourly steps of one fake run: 1 mm/h inside each (start, end) wet range of hours after init, else dry."""
    t0 = pd.Timestamp(init)
    return pd.DataFrame([{"model": model, "init": t0, "point": point, "valid_start": t0 + pd.Timedelta(hours=h),
                          "valid_end": t0 + pd.Timedelta(hours=h + 1),
                          "qpf_mm": 1.0 if any(a <= h < b for a, b in wet) else 0.0} for h in range(hours)])


def tracked(fc, now, storms=None):
    from cottonwoods import storms as st
    cfg = load_config()
    base = storms if storms is not None else pd.DataFrame(columns=st.COLS)
    return st.update_forecast_storms(base, fc, cfg, pd.Timestamp(now))


def test_future_storm_no_model_shows_is_dropped_but_an_ongoing_one_is_kept():
    first = tracked(pd.concat([run_steps("GFS", "2026-11-01", [(10, 20), (60, 70)])]), "2026-11-01 01:00")
    assert len(first) == 2 and set(first.status) == {"forecast"}
    # newer run: the second storm is gone; the first is under way (now is past its start) and has no events left
    later = tracked(run_steps("GFS", "2026-11-01 18:00", []), "2026-11-01 18:30", first)
    st = later.set_index("storm_id").status
    assert st["2026110110"] == "forecast"           # started: waiting for verification, not dropped
    assert st["2026110312"] == "dropped"            # future and gone from every newest run


def test_one_event_spanning_two_tracked_storms_merges_them():
    first = tracked(run_steps("GFS", "2026-11-01", [(10, 20), (36, 46)]), "2026-11-01 01:00")
    assert len(first) == 2
    later = tracked(run_steps("GFS", "2026-11-01 06:00", [(4, 40)]), "2026-11-01 06:30", first)
    st = later.set_index("storm_id")
    assert st.loc["2026110212", "status"] == "merged" and "merged into 2026110110" in st.loc["2026110212", "note"]
    assert st.loc["2026110110", "status"] == "forecast"


def test_a_storm_newer_runs_show_as_two_events_splits():
    first = tracked(run_steps("GFS", "2026-11-01", [(10, 50)]), "2026-11-01 01:00")
    later = tracked(run_steps("GFS", "2026-11-01 06:00", [(4, 14), (30, 44)]), "2026-11-01 06:30", first)
    new = later[later.note.astype(str).str.contains("split from 2026110110")]
    assert len(later) == 2 and len(new) == 1 and new.status.iloc[0] == "forecast"


def test_model_disagreement_and_parts_are_reported():
    from cottonwoods import storms as st
    cfg = load_config()
    fc = pd.concat([run_steps("GFS", "2026-11-01", [(10, 50)]),            # one long event
                    run_steps("IFS", "2026-11-01", [(10, 20), (36, 50)])])  # two events inside it (16 h gap)
    w = st.storm_windows(st.model_events(fc, "alta", cfg["storms"]))
    assert len(w) == 1 and w[0]["defining_model"] == "GFS" and w[0]["fc_periods"].startswith("IFS shows 2 separate periods")
    # GFS with an 8 h lull inside its storm: one storm (gap < 12 h), reported as two parts (gap >= 6 h)
    g = run_steps("GFS", "2026-11-01", [(10, 20), (28, 40)])
    parts = st.run_subperiods(g, "alta", pd.Timestamp("2026-11-01 10:00"), pd.Timestamp("2026-11-01 16:00") + pd.Timedelta(hours=24), cfg)
    assert parts.count("|") == 1 and parts.endswith("55%")      # 10 h then 12 h of water: 45% | 55%


def test_a_dropped_storm_that_comes_back_is_revived_not_duplicated():
    from cottonwoods.storms import update_forecast_storms
    cfg = make_cfg()
    now = pd.Timestamp("2026-11-01 00:00")
    first = run_steps("GFS", pd.Timestamp("2026-10-31 18:00"), [(30, 40)])
    st = tracked(first, now)
    sid = st.storm_id.iloc[0]
    dry = run_steps("GFS", pd.Timestamp("2026-11-01 00:00"), [])
    st = update_forecast_storms(st, dry, cfg, now)
    assert st.status.tolist() == ["dropped"]
    back = run_steps("GFS", pd.Timestamp("2026-11-01 06:00"), [(24, 34)])
    st = update_forecast_storms(st, back, cfg, now + pd.Timedelta(hours=6))
    assert len(st) == 1 and st.status.iloc[0] == "forecast" and st.storm_id.iloc[0] == sid
    assert "back in the models" in st.note.iloc[0]


def test_hand_entered_collins_totals_match_a_verified_storm_and_show_in_the_report():
    from cottonwoods.archive.obs import load_storm_totals
    from cottonwoods.report import render_verified
    from cottonwoods.storms import manual_totals
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "storm_totals.csv"
        p.write_text("start,end,site,source,new_snow_in,water_in,notes\n"
                     "2026-10-31,2026-11-01,Alta,Collins,10,0.80,day 1\n"
                     "2026-11-01,2026-11-02 06:00,alta,collins,8,0.65,day 2\n"
                     "2026-11-20,2026-11-21,alta,collins,5,,another storm\n")
        totals = load_storm_totals(p)
    assert totals["start_utc"].iloc[0] == pd.Timestamp("2026-10-31 12:00")    # 0600 MDT
    assert totals["end_utc"].iloc[1] == pd.Timestamp("2026-11-02 13:00")      # 0600 MST: DST ended 11/1
    st = pd.Series({"storm_id": "2026103112", "point": "alta", "status": "verified",
                    "fc_start": pd.Timestamp("2026-10-31 12:00"), "fc_end": pd.Timestamp("2026-11-02 06:00"),
                    "final_start": pd.Timestamp("2026-10-31 12:00"), "final_end": pd.Timestamp("2026-11-02 08:00"),
                    "obs_water_in": 1.52})
    m = manual_totals(st, totals, cfg)
    assert m["new_snow_in"] == 18 and abs(m["water_in"] - 1.45) < 1e-9 and m["sources"] == "collins"
    scores = pd.DataFrame({"storm_id": "2026103112", "model": ["GFS", "GFS", "GEFS", "GEFS"],
                           "init": pd.to_datetime(["2026-10-30 12:00", "2026-10-31 06:00", "2026-10-31 06:00", "2026-10-31 06:00"]),
                           "member": [np.nan, np.nan, 0, 1], "lead_to_start_hr": [24, 6, 6, 6],
                           "fc_water_in": [1.0, 1.2, 0.5, 0.7], "fc_snow_in": [12, 14, np.nan, np.nan]})
    text = render_verified(pd.DataFrame([st]), scores, totals, cfg, pd.Timestamp("2026-11-03"))
    assert 'SNOTEL 1.52" water | collins: 18" snow, 1.45" water' in text
    assert "10/31 06z" in text and '1.20"' in text and '14"' in text           # GFS: its LAST run before the storm
    assert '0.60"' in text                                                      # GEFS: member mean
    assert render_verified(pd.DataFrame([st]), scores, totals.iloc[2:], cfg, pd.Timestamp("2026-11-03")).count("not entered yet") == 1


def test_hand_copied_collins_readings_prefer_boards_and_never_double_count():
    from cottonwoods.archive.obs import load_hand_obs
    from cottonwoods.storms import hand_obs_totals
    cfg = make_cfg()
    rows = np.array([[2026, 10, 31, 18, 12, 7.0, 0.60, 14, 25, 95],     # board: 0600-1800 MDT 10/31
                     [2026, 11, 1, 6, 12, 5.0, np.nan, 18, 20, 96],     # board without water
                     [2026, 10, 31, 7, 1, 1.0, 0.08, 9, 24, 97],        # hourly readings
                     [2026, 10, 31, 8, 1, 1.5, 0.10, 10, 24, 97],
                     [2026, 10, 31, 8, 1, 2.0, 0.12, 10, 24, 97]])      # same hour typed again: the last one wins
    with tempfile.TemporaryDirectory() as d:
        np.save(Path(d) / "collins_obs.npy", rows)
        r = load_hand_obs(Path(d))
        bad = rows.copy()
        bad[0, 5] = -1
        np.save(Path(d) / "collins_obs.npy", bad)
        try:
            load_hand_obs(Path(d))
            raise AssertionError("a negative amount should be refused")
        except ValueError:
            pass
    assert len(r) == 4 and r["end_utc"].iloc[0] == pd.Timestamp("2026-10-31 13:00")   # 0700 MDT
    st = pd.Series({"point": "alta", "fc_start": pd.Timestamp("2026-10-31 12:00"), "fc_end": pd.Timestamp("2026-11-01 13:00"),
                    "final_start": pd.Timestamp("2026-10-31 12:00"), "final_end": pd.Timestamp("2026-11-01 14:00")})
    t = hand_obs_totals(st, r, cfg)
    assert t["new_snow_in"] == 12.0                  # boards 7 + 5; the hourly 1 + 2 are NOT added on top
    assert abs(t["water_in"] - 0.60) < 1e-9          # water from the one board that has it
    assert t["max_depth_in"] == 18 and t["sources"] == "collins (board)"
    hourly_only = hand_obs_totals(st, r[r["period_h"] == 1], cfg)
    assert hourly_only["new_snow_in"] == 3.0 and hourly_only["sources"] == "collins (hourly)"


def test_a_board_missing_water_is_filled_from_its_hours_and_overlapping_boards_count_once():
    from cottonwoods.archive.obs import load_hand_obs
    from cottonwoods.storms import hand_obs_totals
    cfg = make_cfg()
    rows = np.array([[2026, 10, 31, 6, 12, 4.0, 0.35, 11, 25, 97],      # board 1800 Fri - 0600 Sat MDT
                     [2026, 10, 31, 18, 12, 6.0, 0.48, 16, 21, 95],     # board 0600 - 1800 Sat
                     [2026, 10, 31, 18, 24, 11.0, 0.90, 16, 21, 95],    # 24 h board over both: skipped
                     [2026, 11, 1, 6, 12, 7.0, np.nan, 22, 17, 90],     # board 1800 Sat - 0600 Sun MST: no water
                     [2026, 10, 31, 23, 1, 0.8, 0.05, 18, 19, 92],      # hours inside that board
                     [2026, 11, 1, 6, 1, 0.5, 0.03, 22, 17, 90],
                     [2026, 11, 1, 8, 1, 0.4, 0.02, 22, 17, 90]])       # after the last board: added
    with tempfile.TemporaryDirectory() as d:
        np.save(Path(d) / "collins_obs.npy", rows)
        r = load_hand_obs(Path(d))
    st = pd.Series({"point": "alta", "fc_start": pd.Timestamp("2026-10-31 00:00"), "fc_end": pd.Timestamp("2026-11-01 15:00"),
                    "final_start": pd.Timestamp("2026-10-31 00:00"), "final_end": pd.Timestamp("2026-11-01 15:00")})
    t = hand_obs_totals(st, r, cfg)
    assert abs(t["new_snow_in"] - (4 + 6 + 7 + 0.4)) < 1e-9          # 12 h boards, plus the hour after the last one
    assert abs(t["water_in"] - (0.35 + 0.48 + 0.05 + 0.03 + 0.02)) < 1e-9
    assert t["sources"] == "collins (board, hourly)"


def test_session_reminder_lists_only_recent_verified_storms_without_collins_entries():
    import importlib.util
    spec = importlib.util.spec_from_file_location("collins_due", Path(__file__).resolve().parents[1] / "scripts" / "collins_due.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = make_cfg()
    now = pd.Timestamp("2026-11-05")
    t = pd.DataFrame({"storm_id": ["a", "b", "c", "d"], "point": "alta", "status": ["verified", "verified", "forecast", "verified"],
                      "fc_start": pd.to_datetime(["2026-10-31", "2026-11-02", "2026-11-06", "2026-09-01"]),
                      "fc_end": pd.to_datetime(["2026-11-01", "2026-11-03", "2026-11-07", "2026-09-02"]),
                      "final_start": pd.to_datetime(["2026-10-31", "2026-11-02", None, "2026-09-01"]),
                      "final_end": pd.to_datetime(["2026-11-01", "2026-11-03", None, "2026-09-02"])})
    totals = pd.DataFrame({"site": ["alta"], "source": ["collins"], "new_snow_in": [9.0], "water_in": [0.7],
                           "start_utc": [pd.Timestamp("2026-10-31 06:00")], "end_utc": [pd.Timestamp("2026-11-01")]})
    need = mod.due(t, cfg, now, totals, pd.DataFrame())
    assert need["storm_id"].tolist() == ["b"]          # a has entries, c isn't over, d is too old


# --- lead limits: storms and deterministic days to 168 h, ensembles to 240 h (owner, 2026-10-27) ----------------

def test_storms_ignore_events_past_the_lead_limit_and_totals_stop_there():
    from cottonwoods import storms as st
    from cottonwoods.report import storm_models
    cfg = load_config()
    assert cfg["storms"]["max_lead_hours"] == 168
    # day 2 event, an event straddling hour 168, and a day 9 event (never a storm)
    fc = run_steps("GFS", "2026-11-01", [(30, 40), (160, 176), (200, 210)], hours=240)
    w = st.storm_windows(st.model_events(fc, "alta", cfg["storms"]))
    assert [x["fc_start"] for x in w] == [pd.Timestamp("2026-11-02 06:00"), pd.Timestamp("2026-11-07 16:00")]
    assert w[1]["fc_end"] <= pd.Timestamp("2026-11-08 00:00")        # cut at init + 168 h
    storm = pd.Series({"fc_start": pd.Timestamp("2026-11-07 16:00"), "fc_end": pd.Timestamp("2026-11-08 08:00"),
                       "point": "alta"})
    (r,) = storm_models(storm, fc, cfg)
    assert not r["full"] and r["valid_to"] == pd.Timestamp("2026-11-08 00:00")
    assert abs(r["points"]["alta"]["water_in"] - 8 / 25.4) < 1e-9     # hours 160-168 only


def test_a_run_starting_after_the_storm_began_does_not_cover_it():
    # 10/28: a storm that ended at 06 MDT stayed listed (awaiting obs) and runs made after it showed 0.00 "full"
    from cottonwoods.report import storm_models
    cfg = load_config()
    fc = run_steps("HRRR", "2026-11-01 18:00", [], hours=48)
    for start, end in (("2026-11-01 06:00", "2026-11-01 12:00"),      # over before the run starts
                       ("2026-11-01 12:00", "2026-11-02 06:00")):     # run starts mid-storm
        storm = pd.Series({"fc_start": pd.Timestamp(start), "fc_end": pd.Timestamp(end), "point": "alta"})
        (r,) = storm_models(storm, fc, cfg)
        assert r["late"] and not r["full"]
    storm = pd.Series({"fc_start": pd.Timestamp("2026-11-01 18:00"), "fc_end": pd.Timestamp("2026-11-02 06:00"),
                       "point": "alta"})
    (r,) = storm_models(storm, fc, cfg)
    assert r["full"] and not r["late"]                                  # starting exactly at the storm is fine


def test_daily_table_shows_deterministic_to_7_days_and_ensembles_to_10():
    from cottonwoods.report import model_days
    cfg = make_cfg()
    det = run_steps("GFS", "2026-11-01", [(0, 240)], hours=240)
    ens = pd.concat([run_steps("GEFS", "2026-11-01", [(0, 240)], hours=240).assign(member=m) for m in range(3)])
    rows = {r["model"]: r for r in model_days(pd.concat([det, ens]), cfg, "alta", start=pd.Timestamp("2026-11-01"))}
    assert len(rows["GFS"]["windows"]) == cfg["report"]["days"] == 10
    ends = [e for _, e in rows["GFS"]["windows"]]
    shown = [e for e, c in zip(ends, rows["GFS"]["days"]) if c is not None]
    assert max(shown) <= pd.Timestamp("2026-11-08")                   # nothing past init + 168 h
    tail = [c for e, c in zip(ends, rows["GEFS"]["days"]) if e > pd.Timestamp("2026-11-08")]
    assert tail and all(c is not None and c["members_wet"] == 3 for c in tail)   # 8-10 day tail stays


def test_a_storm_tracked_past_the_lead_limit_is_dropped_as_out_of_range_and_comes_back():
    # tracked before the limit existed (e.g. the IFS 282 h storm of 11/09): its window starts past init + 168 h
    first = tracked(run_steps("GFS", "2026-11-01", [(150, 160)], hours=240), "2026-11-01 01:00")
    first[["fc_start", "fc_end"]] += pd.Timedelta(hours=100)
    later = tracked(run_steps("GFS", "2026-11-01 06:00", [], hours=240), "2026-11-01 06:30", first)
    assert later.status.iloc[0] == "dropped" and "lead limit" in later.note.iloc[0]
    # once a newer run reaches it and shows it, it's the same storm again
    back = tracked(run_steps("GFS", "2026-11-05 00:00", [(154, 164)], hours=240), "2026-11-05 00:30", later)
    assert len(back) == 1 and back.status.iloc[0] == "forecast" and "back in the models" in back.note.iloc[0]


def test_gauge_noise_is_not_precip_but_real_rises_and_resets_are_handled():
    from cottonwoods.gauge import gauge_increments
    g = make_cfg()["gauge"]
    t = pd.date_range("2026-10-27 04:00", periods=24, freq="h")
    wobble = pd.Series([8.8, 8.9, 9.0, 8.9, 9.0, 8.8, 8.9, 9.0, 9.0, 8.9, 8.8, 8.9] * 2, index=t)  # Atwater, dry 10/27
    assert wobble.diff().clip(lower=0).sum() > 0.5                 # what the old rule counted as precip
    inc = gauge_increments(wobble, g)
    assert abs(inc.sum() - 0.2) < 1e-9 and inc.iloc[3:].sum() == 0   # noise counts once (to the band top), then never
    storm = pd.Series([9.0, 8.9, 9.0, 9.1, 9.3, 9.6, 9.8, 9.7, 9.8, 9.9, 9.8, 9.9], index=t[:12])  # a real 0.9" rise
    assert abs(gauge_increments(storm, g).sum() - 0.9) < 1e-9
    emptied = pd.Series([12.0, 12.1, 12.2, 0.0, 0.0, 0.1, 0.2, 0.3], index=t[:8])   # serviced, then it keeps raining
    assert abs(gauge_increments(emptied, g).sum() - 0.5) < 1e-9


def test_dry_day_with_gauge_wobble_is_not_an_observed_event():
    from cottonwoods.storms import detect_events, obs_steps
    cfg = make_cfg()
    t = pd.date_range("2026-10-27 04:00", periods=48, freq="h")
    obs = pd.DataFrame({"site": "atwater", "station": "x", "element": "PREC", "time_utc": t,
                        "value": [8.8, 8.9, 9.0, 8.9, 9.0, 8.8, 8.9, 9.0, 9.0, 8.9, 8.8, 8.9] * 4})
    st = obs_steps(obs, ["atwater"], cfg["gauge"])
    later = st[st.valid_end > t[11]]                               # after the gauge's level is known (the store
    assert later["qpf_mm"].sum() == 0                              # holds weeks of it, so this is the normal case)
    assert detect_events(later, cfg["storms"]["obs_wet_in"] * 25.4, cfg["storms"]["gap_hours"], 0.0).empty


def test_storm_report_lists_an_ended_storm_on_one_line_and_late_runs_as_dashes():
    # 10/28: the report kept a full table of 0.00s for a dry storm that had ended, from runs made after it
    from cottonwoods.report import render_storms
    cfg = load_config()
    fc = run_steps("GFS", "2026-11-01 00:00", [(6, 12)], hours=48)
    st = tracked(fc, "2026-11-01 00:00")
    sid = st.loc[st["status"] == "forecast", "storm_id"].iloc[0]
    late = pd.concat([fc, run_steps("GFS", "2026-11-01 08:00", [], hours=48)])
    text = render_storms(st, late, cfg, pd.Timestamp("2026-11-01 10:00"))           # under way
    row = next(line for line in text.splitlines() if line.startswith("GFS"))
    assert f"=== Storm {sid}" in text and "from " in row and "0.00" not in row and "-" in row.split()
    text = render_storms(st, late, cfg, pd.Timestamp("2026-11-01 13:00"))           # over
    assert f"=== Storm {sid}" not in text and f"--- Storm {sid}" in text and "awaiting verification" in text


def test_storm_table_puts_every_number_under_its_own_label():
    from cottonwoods.report import _storm_table
    init = pd.Timestamp("2026-10-30 12:00")
    det = {"model": "GFS", "init": init, "valid_to": init, "full": True, "late": False, "ensemble": False,
           "points": {"alta": {"water_in": 1.2, "snow_in": 12.0, "snow_in_regime": 14.0}, "alta_snotel": None},
           "snow_level_m": (2000.0, 1800.0), "flow": "NW cold -12C 100%", "modes": "frontal 100%"}
    ens = {"model": "GEFS", "init": init, "valid_to": init, "full": False, "late": False, "ensemble": True,
           "points": {"alta": {"members_wet": 25, "members": 31, "mean_water_in": 0.6}, "alta_snotel": None},
           "snow_level_m": None, "flow": "", "modes": ""}
    tail_w = {"snow level": 13, "flow": 18, "modes": 12}
    lines = _storm_table([det, ens], ["alta", "alta_snotel"], tail_w)
    assert lines[0].split() == ["alta", "alta", "SNTL"]
    named = _storm_table([det, ens], ["alta", "alta_snotel"], tail_w, {"alta_snotel": "Mill-D North SNOTEL"})
    assert named[0].split() == ["alta", "Mill-D", "SNTL"]          # a gauge's own name, not the resort's
    sub_det, gfs, sub_ens, gefs = lines[1:]
    assert gfs.split()[:6] == ["GFS", "10/30", "12z", "full", "storm", "1.20"] and gfs.split()[6] == "14"   # regime snow
    assert gfs.index("1.20") + 4 == sub_det.index("water") + 5 and gfs.index("14") + 2 == sub_det.index("snow") + 4
    assert gefs.index("25/31") + 5 == sub_ens.index("wet/N") + 5 and gefs.index("0.60") + 4 == sub_ens.index("mean") + 4
    assert gfs.index("6.6k->5.9k ft") == sub_det.index("snow level") and "flow" not in sub_ens
    assert "to " in gefs and "-" in gefs.split()                 # short run; a point the run predates shows "-"


def test_storm_blocks_sit_on_the_fixed_00z_12z_grid_and_add_up_to_the_storm():
    from cottonwoods import blocks
    from cottonwoods.windows import window_total
    s, e = pd.Timestamp("2026-10-30 15:00"), pd.Timestamp("2026-11-02 18:00")
    blk = blocks.storm_blocks(s, e)
    assert [b[:2] for b in blk][:2] == [(s, pd.Timestamp("2026-10-31 00:00")),
                                        (pd.Timestamp("2026-10-31 00:00"), pd.Timestamp("2026-10-31 12:00"))]
    assert blk[-1][:2] == (pd.Timestamp("2026-11-02 12:00"), e) and len(blk) == 7
    assert [blocks.label(b[2]) for b in blk[:3]] == ["Fri day", "Fri ngt", "Sat day"]
    hours = pd.date_range("2026-10-30 12:00", "2026-11-03 00:00", freq="3h")
    steps = pd.DataFrame({"valid_start": hours[:-1], "valid_end": hours[1:], "snow_in": np.arange(len(hours) - 1.0)})
    got = blocks.run_blocks(steps, blk, "snow_in")
    assert {c for _, c in got} == {blocks.FULL}
    assert sum(v for v, _ in got) == pytest.approx(window_total(steps, s, e, "snow_in"))


def test_a_short_or_late_run_fills_the_blocks_it_reaches():
    from cottonwoods import blocks
    blk = blocks.storm_blocks(pd.Timestamp("2026-10-30 12:00"), pd.Timestamp("2026-11-01 12:00"))
    def run(a, b):
        t = pd.date_range(a, b, freq="1h")
        return pd.DataFrame({"valid_start": t[:-1], "valid_end": t[1:], "snow_in": 0.5})
    short = blocks.run_blocks(run("2026-10-30 12:00", "2026-10-31 18:00"), blk, "snow_in")    # ends mid-block
    assert [c for _, c in short] == [blocks.FULL, blocks.FULL, blocks.PART, blocks.NONE]
    assert short[2][0] == pytest.approx(3.0)                                                  # its 6 hours only
    late = blocks.run_blocks(run("2026-10-31 06:00", "2026-11-02 00:00"), blk, "snow_in")     # began mid-storm
    assert [c for _, c in late] == [blocks.NONE, blocks.PART, blocks.FULL, blocks.FULL]
