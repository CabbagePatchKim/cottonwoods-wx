"""Storm events (storms.py), window totals (windows.py) and report staleness (report.py)."""
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

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
