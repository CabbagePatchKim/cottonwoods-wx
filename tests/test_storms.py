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


def test_unmodified_runs_are_scored_beside_the_blended_ones_but_never_shape_storms():
    from cottonwoods import storms
    from cottonwoods.report import model_rank
    cfg = make_cfg()
    i = pd.Timestamp("2026-11-01 12:00")
    def run(model, qpf, end):
        vs = pd.date_range(i, "2026-11-03 12:00", freq="h", inclusive="left")
        wet = (vs >= pd.Timestamp("2026-11-02")) & (vs < pd.Timestamp(end))
        return pd.DataFrame({"model": model, "init": i, "lead_hr": range(1, len(vs) + 1), "valid_start": vs,
                             "valid_end": vs + pd.Timedelta(hours=1), "point": "alta", "qpf_mm": np.where(wet, qpf, 0.0)})
    fc, raw = run("GFS", 1.0, "2026-11-02 12:00"), run("GFS raw", 2.0, "2026-11-02 20:00")   # raw: longer + wetter
    obs = snotel_prec("2026-11-01 12:00", [0.0] * 12 + [0.1] * 12 + [0.0] * 14)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        t = storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 06:00"), root=root, fc_raw=raw)
        assert len(t) == 1 and t.iloc[0].fc_end == pd.Timestamp("2026-11-02 12:00")   # the window ignores the raw run
        sc = pd.read_csv(root / "storm_scores.csv").set_index("model")
        assert abs(sc.loc["GFS", "fc_water_in"] - 12 / 25.4) < 1e-9
        assert abs(sc.loc["GFS raw", "fc_water_in"] - 24 / 25.4) < 1e-9
    assert model_rank("GFS") < model_rank("GFS raw") < model_rank("GDPS")


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
    scores = pd.DataFrame({"storm_id": "2026103112", "point": "alta", "obs_water_in": 1.52, "fc_event": True,
                           "obs_event": True, "model": ["GFS", "GFS", "GEFS", "GEFS"],
                           "init": pd.to_datetime(["2026-10-30 12:00", "2026-10-31 06:00", "2026-10-31 06:00", "2026-10-31 06:00"]),
                           "member": [np.nan, np.nan, 0, 1], "lead_to_start_hr": [24, 6, 6, 6],
                           "fc_water_in": [1.0, 1.2, 0.5, 0.7], "fc_snow_in": [12, 14, np.nan, np.nan]})
    text = render_verified(pd.DataFrame([st]), scores, totals, cfg, pd.Timestamp("2026-11-03"))
    assert 'SNOTEL 1.52" water | collins: 18" snow, 1.45" water' in text
    assert "10/31 06z" in text and '1.20"' in text and '14"' in text           # GFS: its LAST run before the storm
    assert '0.60"' in text                                                      # GEFS: member mean
    assert render_verified(pd.DataFrame([st]), scores, totals.iloc[2:], cfg, pd.Timestamp("2026-11-03")).count("Collins: no total yet") == 1


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


def test_session_reminder_warns_only_when_the_workers_collins_readings_fall_behind_during_a_storm():
    # the Collins page only goes back ~2 days (owner 2026-11-01); since 2026-11-10 the Worker records it and the
    # owner no longer enters readings, so the reminder speaks up only when the Worker's hours are late
    import importlib.util
    spec = importlib.util.spec_from_file_location("collins_due", Path(__file__).resolve().parents[1] / "scripts" / "collins_due.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = make_cfg()
    cfg["storms"]["collins_page_hours"] = 48
    t = pd.DataFrame({"storm_id": ["live", "old", "future", "gone"], "point": "alta",
                      "status": ["forecast", "verified", "forecast", "merged"],
                      "fc_start": pd.to_datetime(["2026-10-31 18:00", "2026-10-20 00:00", "2026-11-04 00:00",
                                                  "2026-10-31 00:00"]),
                      "fc_end": pd.to_datetime(["2026-11-02 18:00", "2026-10-21 00:00", "2026-11-05 00:00",
                                                "2026-11-02 00:00"]),
                      "final_start": pd.NaT, "final_end": pd.NaT})
    readings = pd.DataFrame({"end_utc": [pd.Timestamp("2026-11-01 04:00")]})
    now = pd.Timestamp("2026-11-01 07:00")
    assert mod.collins_progress(t, cfg, now, readings) == ""                  # 3 h old: the Worker is current
    late = now + pd.Timedelta(hours=cfg["storms"]["collins_worker_late_hours"])
    msg = mod.collins_progress(t, cfg, late, readings)
    assert "WORKER BEHIND" in msg and "live" in msg and "old" not in msg and "future" not in msg and "gone" not in msg
    assert "recorded through Sat 10/31 2200 MDT" in msg and "already be off" not in msg
    # two days later with nothing new: the hours older than the page's reach are flagged as lost
    msg = mod.collins_progress(t, cfg, pd.Timestamp("2026-11-03 12:00"), readings)
    assert "already be off the page" in msg
    assert mod.collins_progress(t, cfg, pd.Timestamp("2026-11-08"), readings) == ""   # no storm on the page
    assert "none recorded yet" in mod.collins_progress(t, cfg, now, pd.DataFrame())


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


def test_gauge_spikes_and_dips_that_come_straight_back_and_impossible_jumps_are_not_precip():
    # found by the verification backfill (2026-11-10): older SNOTEL hourly data spikes for one hour at 08Z
    from cottonwoods.gauge import gauge_increments
    g = make_cfg()["gauge"]
    t = pd.date_range("2023-04-10 04:00", periods=12, freq="h")
    spike = pd.Series([34.6, 34.6, 34.6, 34.6, 63.5, 34.6, 34.6, 34.7, 34.6, 34.6, 34.6, 34.6], index=t)   # Atwater
    assert abs(gauge_increments(spike, g).sum() - 0.1) < 1e-9        # only the real 0.1" wobble, not 28.9"
    dip = pd.Series([20.0, 20.1, 0.4, 20.1, 20.2, 20.3, 20.3, 20.3, 20.4, 20.4, 20.4, 20.4], index=t)
    assert abs(gauge_increments(dip, g).sum() - 0.4) < 1e-9          # a one-hour dip is not a gauge emptying
    jump = pd.Series([34.6, 35.4, 36.8, 48.7, 48.7, 48.8, 48.8, 48.9, 48.9, 49.2, 49.4, 49.4], index=t)   # 4/17/2023
    assert abs(gauge_increments(jump, g).sum() - (0.8 + 0.7)) < 1e-9 # the 1.4" and 11.9" hours aren't precip
    emptied = pd.Series([12.0, 12.1, 12.2, 0.0, 0.0, 0.1, 0.2, 0.3, 0.3, 0.3, 0.4, 0.4], index=t)   # stays low: real
    assert abs(gauge_increments(emptied, g).sum() - 0.6) < 1e-9


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
    assert abs(sum(v for v, _ in got) - window_total(steps, s, e, "snow_in")) < 1e-9


def test_a_short_or_late_run_fills_the_blocks_it_reaches():
    from cottonwoods import blocks
    blk = blocks.storm_blocks(pd.Timestamp("2026-10-30 12:00"), pd.Timestamp("2026-11-01 12:00"))
    def run(a, b):
        t = pd.date_range(a, b, freq="1h")
        return pd.DataFrame({"valid_start": t[:-1], "valid_end": t[1:], "snow_in": 0.5})
    short = blocks.run_blocks(run("2026-10-30 12:00", "2026-10-31 18:00"), blk, "snow_in")    # ends mid-block
    assert [c for _, c in short] == [blocks.FULL, blocks.FULL, blocks.PART, blocks.NONE]
    assert abs(short[2][0] - 3.0) < 1e-9                                                  # its 6 hours only
    late = blocks.run_blocks(run("2026-10-31 06:00", "2026-11-02 00:00"), blk, "snow_in")     # began mid-storm
    assert [c for _, c in late] == [blocks.NONE, blocks.PART, blocks.FULL, blocks.FULL]


def test_every_gauged_point_is_scored_and_runs_are_scored_on_the_blocks_they_cover():
    from cottonwoods import storms
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        def pull(i, fxx, points, s=None):                 # 0.5 mm/h from 11/02 00z to 11/03 00z, everywhere
            rows = []
            for f in fxx:
                vs = pd.Timestamp(i) + pd.Timedelta(hours=f - 1)
                for name in points:
                    rows.append({"model": "HRRR", "init": pd.Timestamp(i), "lead_hr": f, "valid_start": vs,
                                 "valid_end": vs + pd.Timedelta(hours=1), "point": name, "grid_dist_km": 1.0,
                                 "qpf_mm": 0.5 if pd.Timestamp("2026-11-02") <= vs < pd.Timestamp("2026-11-03") else 0.0,
                                 **{f"t_{lev}_k": 265.0 for lev in (700, 600, 500)}})
            return pd.DataFrame(rows)
        fa.archive_run("HRRR", datetime(2026, 11, 1, 12), cfg, root=root, puller=pull)
        storms.run(fa.load_forecasts(cfg, root=root), pd.DataFrame(), cfg, pd.Timestamp("2026-11-01 15:00"), root=root)
        fa.archive_run("HRRR", datetime(2026, 11, 2, 6), cfg, root=root, puller=pull)   # began mid-storm
        fc = fa.load_forecasts(cfg, root=root)
        wet = [0.0] * 12 + [0.1] * 3 + [0.0] * 9 + [0.1] * 6 + [0.0] * 20                # 0.3" then 0.6" (12-18z)
        obs = pd.concat([snotel_prec("2026-11-01 12:00", wet), snotel_prec("2026-11-01 12:00", [0.0] * 50, site="snowbird")])
        storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 14:00"), root=root)
        sc = pd.read_csv(root / "storm_scores.csv")
        assert set(sc["point"]) == {"alta", "snowbird"}                       # every point with a gauge
        snowbird = sc[sc["point"] == "snowbird"].iloc[0]
        assert snowbird.fc_event and not snowbird.obs_event                    # its gauge stayed dry: a false alarm
        bl = storms.load_block_scores(root)
        alta = bl[bl["point"] == "alta"].sort_values(["init", "block_start"])
        assert len(alta[alta["init"] == pd.Timestamp("2026-11-01 12:00")]) == 2   # both blocks of the storm
        late = alta[alta["init"] == pd.Timestamp("2026-11-02 06:00")]
        assert list(late["block_start"]) == [pd.Timestamp("2026-11-02 12:00")]   # only the block after its start
        assert abs(late["fc_water_in"].iloc[0] - 12 * 0.5 / 25.4) < 1e-9
        assert abs(late["obs_water_in"].iloc[0] - 0.6) < 1e-9


def test_a_storm_under_way_shows_observed_water_so_far_and_each_models_rest_of_storm():
    from cottonwoods.report import storm_block_lines
    cfg = make_cfg()
    t = pd.date_range("2026-11-01 12:00", "2026-11-03 12:00", freq="1h")
    fc = pd.DataFrame([{"model": "GFS", "init": pd.Timestamp("2026-11-01 12:00"), "point": "alta",
                        "valid_start": a, "valid_end": b, "qpf_mm": 1.0, "snow_in": 0.5} for a, b in zip(t[:-1], t[1:])])
    st = pd.Series({"fc_start": pd.Timestamp("2026-11-02 00:00"), "fc_end": pd.Timestamp("2026-11-03 00:00")})
    osteps = hourly("2026-11-02 00:00", [0.254] * 18)                   # 0.01" water an hour so far
    now = pd.Timestamp("2026-11-02 18:00")
    lines = storm_block_lines(st, fc, cfg, now, osteps)
    gfs = next(line for line in lines if line.startswith("GFS"))
    assert gfs.split()[-1] == "3.0"                                     # 6 h left x 0.5"
    observed = next(line for line in lines if line.startswith("observed"))
    assert "0.12" in observed and "0.06*" in observed                    # first block done, second still going
    assert not any(line.startswith("observed") for line in storm_block_lines(st, fc, cfg, st.fc_start - pd.Timedelta(hours=1), osteps))


def _rap(start, hours, dirs):
    """Fake RAP analyses at alta: 20 m/s from each hour's direction (deg), -2 C."""
    t = pd.date_range(start, periods=hours, freq="h")
    d = np.radians(np.resize(dirs, hours))
    return pd.DataFrame({"time_utc": t, "point": "alta", "u_700_ms": -20 * np.sin(d), "v_700_ms": -20 * np.cos(d),
                         "t_700_k": 271.15})


def test_observed_regime_is_rap_weighted_by_observed_precip_with_balloons_as_fallback():
    from cottonwoods import regimes
    cfg = make_cfg()
    s, e = pd.Timestamp("2026-11-02 00:00"), pd.Timestamp("2026-11-03 00:00")
    rap = _rap(s, 24, [220] * 16 + [270] * 8)                           # 16 h SW, then 8 h W
    wet_late = hourly(s, [0.0] * 16 + [2.0] * 8)                        # all the snow fell in the W hours
    o = regimes.observed_regime(s, e, "alta", rap, None, wet_late, cfg)
    assert o["source"] == "rap" and regimes.dominant(o["flow"]) == "W warm"
    dry = regimes.observed_regime(s, e, "alta", rap, None, hourly(s, [0.0] * 24), cfg)
    assert regimes.dominant(dry["flow"]) == "SW warm"                   # nothing fell: every hour counts the same
    snd = pd.DataFrame({"time_utc": [s, s + pd.Timedelta(hours=12)], "wdir700": [300.0, 300.0],
                        "wspd700_kt": [30.0, 30.0], "t700_c": [-8.0, -8.0]})
    sparse = regimes.observed_regime(s, e, "alta", rap.iloc[:6], snd, wet_late, cfg)
    assert sparse["source"] == "balloons" and regimes.dominant(sparse["flow"]).startswith("NW")
    assert regimes.dominant("SW warm 1C 30% -> W warm -2C 70%") == "W warm" and regimes.dominant(None) == ""


def test_rap_hours_are_fetched_once_and_only_after_they_post():
    from cottonwoods.archive.obs import archive_rap, load_rap
    cfg = make_cfg()
    asked = []

    def fetcher(hours, points, cfg, save_dir=None, workers=4):
        asked.append(list(hours))
        return pd.concat([_rap(h, 1, [250]).assign(point=p) for h in hours for p in points], ignore_index=True)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        now = pd.Timestamp("2026-11-02 06:30")
        archive_rap(cfg, "2026-11-02", "2026-11-03", root=root, fetcher=fetcher, now=now)
        assert asked[0][-1] == pd.Timestamp("2026-11-02 04:00")         # 2 h lag: 05z and later not posted yet
        archive_rap(cfg, "2026-11-02", "2026-11-03", root=root, fetcher=fetcher, now=now + pd.Timedelta(hours=2))
        assert asked[1] == [pd.Timestamp("2026-11-02 05:00"), pd.Timestamp("2026-11-02 06:00")]   # only the new ones
        assert load_rap(root=root)["time_utc"].nunique() == 7


def test_every_model_is_credited_under_the_storms_forecast_regime_even_when_it_called_it_dry():
    from cottonwoods.report import regime_table
    cfg = make_cfg()
    storms_t = pd.DataFrame({"storm_id": ["s1"], "fc_flow": ["SW warm 0C 20% -> W warm -1C 80%"]})
    binned = pd.DataFrame({"storm_id": "s1", "point": "alta", "member": np.nan, "lead_to_start_hr": [6, 6],
                           "model": ["RDPS", "NAMNEST"], "points": [2, 0]})
    t = regime_table(binned, storms_t, cfg).set_index("model")
    assert list(t.columns) == ["W warm"] and t.at["RDPS", "W warm"] == "+2.0 (1)" and t.at["NAMNEST", "W warm"] == "+0.0 (1)"


def test_local_regime_scores_rescore_verified_storms_with_regime_adjusted_snow():
    # owner 2026-10-29: plain vs regime-adjusted snow, scored by the pass's own rules, on the laptop only
    from cottonwoods import storms
    from cottonwoods.archive.obs import load_storm_totals
    from cottonwoods.report import regime_score_table
    cfg = make_cfg()
    init = datetime(2026, 11, 1, 12)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        def pull(i, fxx, points, s=None):                    # 1 mm/h 11/02 00z-12z, cold, south-west wind
            rows = []
            for f in fxx:
                vs = pd.Timestamp(i) + pd.Timedelta(hours=f - 1)
                for name in points:
                    rows.append({"model": "HRRR", "init": pd.Timestamp(i), "lead_hr": f, "valid_start": vs,
                                 "valid_end": vs + pd.Timedelta(hours=1), "point": name, "grid_dist_km": 1.0,
                                 "qpf_mm": 1.0 if pd.Timestamp("2026-11-02") <= vs < pd.Timestamp("2026-11-02 12:00") else 0.0,
                                 "u_700_ms": 10.0, "v_700_ms": 10.0, **{f"t_{lev}_k": 262.0 for lev in (700, 600, 500)}})
            return pd.DataFrame(rows)
        fa.archive_run("HRRR", init, cfg, root=root, puller=pull)
        fc = fa.load_forecasts(cfg, root=root)
        flows = fc.loc[fc["point"] == "alta", "flow"].unique()
        factors = pd.DataFrame({"point": "alta", "flow": flows, "thermal": "", "band": "", "factor": 1.5, "n_days": 99})
        obs = snotel_prec("2026-11-01 12:00", [0.0] * 12 + [0.1] * 12 + [0.0] * 14)
        storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 06:00"), root=root)
        table = storms.load_storms(root)
        p = root / "storm_totals.csv"
        p.write_text("start,end,site,source,new_snow_in,water_in,notes\n2026-11-01 18:00,2026-11-02 12:00,alta,collins,10,1.2,\n")
        both = storms.regime_bin_scores(table, fc, factors, storms.point_steps(obs, cfg), load_storm_totals(p), None, cfg)
    alta = both[both["point"] == "alta"].set_index("snow")
    assert set(both["snow"]) == {"plain", "regime"} and alta.loc["plain", "fc_snow_in"] > 0
    assert abs(alta.loc["regime", "fc_snow_in"] - 1.5 * alta.loc["plain", "fc_snow_in"]) < 1e-9
    assert alta.loc["regime", "obs_snow_in"] == alta.loc["plain", "obs_snow_in"] == 10
    t = regime_score_table(both, cfg).set_index("model")
    assert t.loc["HRRR", "runs"] >= 1 and t.loc["HRRR", "plain score"] != "" and t.loc["HRRR", "regime score"] != ""


def test_guidance_is_listed_apart_in_the_storm_and_block_tables_and_never_in_a_median():
    # dry run 2026-10-30: U of U guidance rows were counted in the 12 h block medians, under a "guidancerun" header
    from cottonwoods.report import GUIDANCE_CAPTION, storm_block_lines
    cfg = make_cfg()
    i = pd.Timestamp("2026-11-01 12:00")
    s, e = pd.Timestamp("2026-11-02 00:00"), pd.Timestamp("2026-11-03 00:00")
    def run(model, snow_per_h):
        vs = pd.date_range(i, "2026-11-03 12:00", freq="h", inclusive="left")
        wet = (vs >= s) & (vs < e)
        return pd.DataFrame({"model": model, "init": i, "valid_start": vs, "valid_end": vs + pd.Timedelta(hours=1),
                             "point": "alta", "qpf_mm": np.where(wet, 1.0, 0.0), "snow_in": np.where(wet, snow_per_h, 0.0)})
    fc = pd.concat([run("HRRR", 0.1), run("RRFS", 0.3), run("UUHRRR", 5.0)], ignore_index=True)
    st = pd.Series({"storm_id": "x", "point": "alta", "fc_start": s, "fc_end": e})
    lines = storm_block_lines(st, fc, cfg)
    median = next(line for line in lines if line.startswith("median"))
    assert "2.4" in median and "60" not in median                 # median of 1.2" and 3.6" per 12 h; not U of U's 60"
    assert lines.index(GUIDANCE_CAPTION) > lines.index(median)    # guidance listed after the median, captioned
    assert any(line.startswith("UUHRRR") for line in lines) and not any("guidancerun" in line for line in lines)


def test_a_retired_models_last_run_sets_no_storm_window_and_leaves_the_storm_table():
    # 11/03: NAM Nest's frozen 06z run (retired 12z, config archive.retired) set Wednesday's storm window
    from cottonwoods.report import storm_runs
    cfg = load_config()
    fc = pd.concat([run_steps("NAMNEST", "2026-11-03 06:00", [(20, 40)], hours=60),
                    run_steps("HRRR", "2026-11-03 12:00", [(18, 26)], hours=48)], ignore_index=True)
    st = tracked(fc, "2026-11-03 15:00")
    live = st[st["status"] == "forecast"]
    assert len(live) == 1 and live["defining_model"].iloc[0] == "HRRR"
    assert live["fc_start"].iloc[0] == pd.Timestamp("2026-11-04 06:00")
    assert [m for m, _ in storm_runs(fc, cfg, pd.Timestamp("2026-11-04 06:00"))] == ["HRRR"]
    assert sorted(m for m, _ in storm_runs(fc, cfg, pd.Timestamp("2026-11-03 09:00"))) == ["HRRR", "NAMNEST"]  # began before
    from cottonwoods.report import model_days                              # and the daily model table
    assert [r["model"] for r in model_days(fc, cfg, "alta", start=pd.Timestamp("2026-11-03 15:00"))] == ["HRRR"]


def test_a_snow_level_at_the_model_ground_shows_as_below_it():
    from cottonwoods.report import _storm_tail
    r = {"snow_level_m": (2829.0, 2400.0), "snow_level_below": (True, False), "flow": "", "modes": ""}
    assert _storm_tail(r)["snow level"] == "<9.3k->7.9k ft"
    assert _storm_tail({**r, "snow_level_below": None})["snow level"] == "9.3k->7.9k ft"   # runs without a ground


def _collins_hours(start_utc, hours, water=0.05, snow=0.5):
    """Hourly Collins readings (load_collins_readings columns) starting at start_utc."""
    s = pd.date_range(start_utc, periods=hours, freq="h")
    return pd.DataFrame({"start_utc": s, "end_utc": s + pd.Timedelta(hours=1), "period_h": 1.0,
                         "new_snow_in": snow, "water_in": water, "depth_in": np.nan})


def test_a_verified_window_starts_when_collins_saw_precip_even_if_snotel_saw_it_later():
    # owner 2026-11-03, option B: the weekend storm began at Collins 15 h before Alta SNOTEL's 0.1" gauge showed it
    from cottonwoods import storms
    from cottonwoods.storms import hand_obs_totals
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        def pull(i, fxx, points, s=None):
            rows = []
            for f in fxx:
                vs = pd.Timestamp(i) + pd.Timedelta(hours=f - 1)
                for name in points:
                    rows.append({"model": "HRRR", "init": pd.Timestamp(i), "lead_hr": f, "valid_start": vs,
                                 "valid_end": vs + pd.Timedelta(hours=1), "point": name, "grid_dist_km": 1.0,
                                 "qpf_mm": 1.0 if pd.Timestamp("2026-11-01 20:00") <= vs < pd.Timestamp("2026-11-02 12:00") else 0.0,
                                 **{f"t_{lev}_k": 265.0 for lev in (700, 600, 500)}})
            return pd.DataFrame(rows)
        fa.archive_run("HRRR", datetime(2026, 11, 1, 12), cfg, root=root, puller=pull)
        fc = fa.load_forecasts(cfg, root=root)
        obs = snotel_prec("2026-11-01 12:00", [0.0] * 14 + [0.1] * 14 + [0.0] * 13)   # SNOTEL wet 11/02 02z-16z
        collins = pd.concat([_collins_hours("2026-10-31 00:00", 3),                    # an earlier, separate storm
                             _collins_hours("2026-11-01 18:00", 22)], ignore_index=True)   # 18z -> 16z
        # verified without Collins first: the window starts at the forecast's 20z
        t = storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 06:00"), root=root)
        assert t.iloc[0].final_start == pd.Timestamp("2026-11-01 20:00")
        before = pd.read_csv(root / "storm_scores.csv")
        # the readings arrive a pass later: the window widens to Collins's 18z start and the storm is re-scored
        t = storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 09:00"), root=root, readings=collins)
        st = t.iloc[0]
        assert st.final_start == pd.Timestamp("2026-11-01 18:00") and st.final_end == pd.Timestamp("2026-11-02 16:00")
        assert st.updated_at == pd.Timestamp("2026-11-03 09:00") and abs(st.obs_water_in - 1.4) < 1e-9
        after = pd.read_csv(root / "storm_scores.csv")
        assert len(after) == len(before)                                  # replaced, not added
        tot = hand_obs_totals(st, collins, cfg)                           # the separate 10/31 hours are left out
        assert abs(tot["water_in"] - 1.10) < 1e-9 and abs(tot["new_snow_in"] - 11.0) < 1e-9
        again = storms.run(fc, obs, cfg, pd.Timestamp("2026-11-03 12:00"), root=root, readings=collins)
        assert again.iloc[0].updated_at == pd.Timestamp("2026-11-03 09:00")   # nothing new: not touched again


def test_hourly_collins_readings_count_only_inside_the_window_but_boards_keep_the_slack():
    from cottonwoods.storms import hand_obs_totals
    cfg = make_cfg()
    st = pd.Series({"point": "alta", "fc_start": pd.Timestamp("2026-11-02 00:00"), "fc_end": pd.Timestamp("2026-11-02 12:00"),
                    "final_start": pd.Timestamp("2026-11-02 00:00"), "final_end": pd.Timestamp("2026-11-02 12:00")})
    hours = _collins_hours("2026-11-01 20:00", 4, water=0.1, snow=1.0)            # all before the window
    assert hand_obs_totals(st, hours, cfg) is None                              # was counted with the 12 h slack
    board = pd.DataFrame({"start_utc": [pd.Timestamp("2026-11-01 18:00")], "end_utc": [pd.Timestamp("2026-11-02 06:00")],
                          "period_h": [12.0], "new_snow_in": [6.0], "water_in": [0.5], "depth_in": [np.nan]})
    assert hand_obs_totals(st, board, cfg)["new_snow_in"] == 6.0                # a board reaching past the start still counts


def test_a_lone_light_shower_ahead_of_the_storm_does_not_stretch_the_collins_start():
    # the weekend storm: Sat 1500 MDT 0.05", Sat 2100 0.01", then steady from Sun 0100 MST; the span starts Sunday
    from cottonwoods.storms import collins_span
    cfg = make_cfg()
    st = pd.Series({"final_start": pd.Timestamp("2026-11-02 00:00"), "final_end": pd.Timestamp("2026-11-02 21:00")})
    r = pd.concat([_collins_hours("2026-10-31 21:00", 1, water=0.05), _collins_hours("2026-11-01 03:00", 1, water=0.01),
                   _collins_hours("2026-11-01 08:00", 24, water=0.06), _collins_hours("2026-11-02 08:00", 1, water=0.01)],
                  ignore_index=True)
    r = pd.concat([_collins_hours("2026-11-01 07:00", 1, water=0.0), r], ignore_index=True)   # a dry hour just before
    # starts at the first wet steady hour (not the dry one before it); a light hour right after steady precip counts
    assert collins_span(st, r, cfg) == (pd.Timestamp("2026-11-01 08:00"), pd.Timestamp("2026-11-02 09:00"))
    showers = pd.concat([_collins_hours("2026-11-01 20:00", 1, water=0.02), _collins_hours("2026-11-02 02:00", 1, water=0.02)],
                        ignore_index=True)
    assert pd.isna(collins_span(st, showers, cfg)[0])                # never steady: the window is left alone


def test_nws_statement_total_is_the_latest_reading_in_the_window_from_a_matching_start():
    from cottonwoods.storms import pns_totals
    cfg = make_cfg()
    st = pd.Series({"point": "alta", "fc_start": pd.Timestamp("2026-02-16 15:00"), "fc_end": pd.Timestamp("2026-02-19 10:00"),
                    "final_start": pd.Timestamp("2026-02-16 15:00"), "final_end": pd.Timestamp("2026-02-19 10:00")})
    t = pd.DataFrame({"site": "alta", "source": "nws statement", "notes": "from 8am Monday",
                      "start_utc": pd.to_datetime(["2026-02-16 15:00", "2026-02-16 15:00", "2026-02-16 15:00", None,
                                                   "2026-02-10 00:00", "2026-02-19 22:00"]),
                      "end_utc": pd.to_datetime(["2026-02-18 23:00", "2026-02-19 11:00", "2026-02-19 11:00",
                                                 "2026-02-19 12:00", "2026-02-19 13:00", "2026-02-20 12:00"]),
                      "issued_utc": pd.to_datetime(["2026-02-19 00:05", "2026-02-19 14:51", "2026-02-19 13:00",
                                                    "2026-02-19 13:30", "2026-02-19 14:00", "2026-02-20 16:07"]),
                      "new_snow_in": [43.0, 47.0, 46.0, 45.5, 60.0, 2.0], "water_in": [4.42, 4.72, 4.70, 4.71, 6.0, 0.18]})
    # rows: 43" earlier update | 47" final (latest issued for 11Z) | 46" same reading issued earlier |
    # 45.5" no stated start, reading 12Z (within 12 h of the end: kept) | 60" stated start 6 days early: another span |
    # 2" the next storm's statement, reading > 12 h past this window
    got = pns_totals(st, t.drop(index=[3]), cfg)
    assert got["new_snow_in"] == 47.0 and got["water_in"] == 4.72 and "nws statement" in got["sources"]
    assert pns_totals(st, t, cfg)["new_snow_in"] == 45.5        # a later reading with no stated start still counts
    assert pns_totals(st, t[t["new_snow_in"] == 60.0], cfg) is None
    assert pns_totals(st, t[t["new_snow_in"] == 2.0], cfg) is None


def test_observed_snow_takes_the_worker_then_the_nws_statement_then_a_hand_total():
    # owner 2026-11-10: no more hand entries; the Worker's hours first, then the NWS statement
    from cottonwoods.storms import observed_snow
    cfg = make_cfg()
    st = pd.Series({"point": "alta", "fc_start": pd.Timestamp("2026-11-01 08:00"), "fc_end": pd.Timestamp("2026-11-02 21:00"),
                    "final_start": pd.Timestamp("2026-11-01 08:00"), "final_end": pd.Timestamp("2026-11-02 21:00"),
                    "obs_water_in": 2.6})
    worker = pd.DataFrame({"start_utc": pd.date_range("2026-11-01 09:00", periods=4, freq="h"),
                           "end_utc": pd.date_range("2026-11-01 10:00", periods=4, freq="h"), "period_h": 1,
                           "new_snow_in": 4.0, "water_in": 0.5, "depth_in": 20.0, "source": "collins (hourly)"})
    nws = {"site": "alta", "source": "nws statement", "start_utc": pd.Timestamp("2026-11-01 01:00"),
           "end_utc": pd.Timestamp("2026-11-02 21:00"), "issued_utc": pd.Timestamp("2026-11-02 22:38"),
           "new_snow_in": 18.0, "water_in": 2.43, "notes": "beginning 6 PM Saturday October 31st"}
    hand = {"site": "alta", "source": "collins", "start_utc": pd.Timestamp("2026-11-01 06:00"),
            "end_utc": pd.Timestamp("2026-11-02 21:00"), "new_snow_in": 20.0, "water_in": 2.5, "notes": ""}
    both = pd.DataFrame([nws, hand])
    assert observed_snow(st, both, worker, cfg)[0] == 16.0                         # Worker hours: 4 x 4"
    snow, src = observed_snow(st, both, pd.DataFrame(), cfg)
    assert snow == 18.0 and src.startswith("nws statement")                       # no Worker hours: the statement
    assert observed_snow(st, pd.DataFrame([hand]), pd.DataFrame(), cfg)[0] == 20.0  # then an old hand total
    water_only = pd.DataFrame([{**nws, "new_snow_in": np.nan}, hand])
    assert observed_snow(st, water_only, pd.DataFrame(), cfg)[0] == 20.0          # a water-only statement gives no snow


def _wet_run(model, init, wet_from, wet_to, hours=48):
    """One deterministic run at Alta: 1 mm/h from wet_from to wet_to, dry otherwise."""
    init = pd.Timestamp(init)
    vs = pd.date_range(init, periods=hours, freq="h")
    wet = (vs >= pd.Timestamp(wet_from)) & (vs < pd.Timestamp(wet_to))
    return pd.DataFrame({"model": model, "init": init, "lead_hr": range(1, hours + 1), "valid_start": vs,
                         "valid_end": vs + pd.Timedelta(hours=1), "point": "alta", "member": np.nan,
                         "qpf_mm": np.where(wet, 1.0, 0.0)})


def _label_runs(storms_mod):
    """Stub the flow / modes / parts text as the run's own name, so a test can see which run labelled a storm."""
    name = lambda g, *a: f"{g['model'].iloc[0]} {g['init'].iloc[0]:%d %Hz}" if len(g) else ""   # noqa: E731
    saved = (storms_mod.run_flow, storms_mod.run_modes, storms_mod.run_subperiods)
    storms_mod.run_flow = storms_mod.run_modes = storms_mod.run_subperiods = name
    return saved


def test_a_storms_forecast_regime_freezes_once_it_starts_while_its_window_keeps_updating():
    # owner 2026-11-10: the weekend storm was labelled "N mild 100%" by a run made in its last hours
    from cottonwoods import storms
    cfg = make_cfg()
    saved = _label_runs(storms)
    try:
        ifs = _wet_run("IFS", "2026-11-01 12:00", "2026-11-02 00:00", "2026-11-02 12:00")
        t = storms.update_forecast_storms(pd.DataFrame(columns=storms.COLS), ifs, cfg, pd.Timestamp("2026-11-01 18:00"))
        assert len(t) == 1 and t.fc_flow.iloc[0] == "IFS 01 12z" and t.regime_model.iloc[0] == "IFS"
        # before the start, a newer defining run still relabels it
        ifs2 = _wet_run("IFS", "2026-11-01 18:00", "2026-11-02 00:00", "2026-11-02 14:00")
        t = storms.update_forecast_storms(t, ifs2, cfg, pd.Timestamp("2026-11-01 23:00"))
        assert t.fc_flow.iloc[0] == "IFS 01 18z"
        # under way: a mid-storm HRRR run now defines a longer window, but the regime stays as forecast
        hrrr = _wet_run("HRRR", "2026-11-02 06:00", "2026-11-02 06:00", "2026-11-02 22:00")
        t = storms.update_forecast_storms(t, pd.concat([ifs2, hrrr]), cfg, pd.Timestamp("2026-11-02 07:00"))
        assert len(t) == 1 and t.defining_model.iloc[0] == "HRRR" and t.fc_end.iloc[0] == pd.Timestamp("2026-11-02 22:00")
        assert t.fc_flow.iloc[0] == "IFS 01 18z" and t.regime_init.iloc[0] == pd.Timestamp("2026-11-01 18:00")
        assert t.fc_modes.iloc[0] == "IFS 01 18z" and t.fc_subperiods.iloc[0] == "IFS 01 18z"
    finally:
        storms.run_flow, storms.run_modes, storms.run_subperiods = saved


def test_a_storm_verified_before_the_fix_is_relabelled_from_runs_issued_before_it_began():
    from cottonwoods import storms
    cfg = make_cfg()
    saved = _label_runs(storms)
    try:
        fc = pd.concat([_wet_run("IFS", "2026-11-01 12:00", "2026-11-02 00:00", "2026-11-02 12:00"),
                        _wet_run("GFS", "2026-10-31 12:00", "2026-11-02 00:00", "2026-11-02 18:00"),   # older, longer
                        _wet_run("IFS", "2026-11-02 12:00", "2026-11-02 12:00", "2026-11-02 18:00"),   # mid-storm
                        _wet_run("GDPS raw", "2026-10-31 00:00", "2026-11-01 12:00", "2026-11-03 00:00")])  # unblended
        st = pd.Series({"point": "alta", "status": "verified", "defining_model": "IFS",
                        "defining_init": pd.Timestamp("2026-11-02 12:00"), "regime_model": None, "regime_init": pd.NaT,
                        "fc_start": pd.Timestamp("2026-11-02 12:00"), "fc_end": pd.Timestamp("2026-11-02 18:00"),
                        "final_start": pd.Timestamp("2026-11-02 00:00"), "final_end": pd.Timestamp("2026-11-02 21:00")})
        got = storms.regime_before_start(st, fc, cfg)
        assert got["regime_model"] == "GFS" and got["fc_flow"] == "GFS 31 12z"   # longest window of the pre-start runs
        assert storms.regime_before_start(st, fc[fc["init"] >= "2026-11-02"], cfg) is None
        assert storms.regime_run(pd.Series({**st.to_dict(), **got})) == ("GFS", pd.Timestamp("2026-10-31 12:00"))
        assert storms.regime_run(st) == ("IFS", pd.Timestamp("2026-11-02 12:00"))      # not relabelled yet
    finally:
        storms.run_flow, storms.run_modes, storms.run_subperiods = saved


def test_storm_section_ends_with_each_points_snow_trend_since_the_previous_runs():
    # owner 2026-11-10: a line at the end of each storm saying whether its snow is trending up, down or the same
    from cottonwoods.report import storm_trend_lines
    cfg = make_cfg()
    st = pd.Series({"fc_start": pd.Timestamp("2026-11-11 00:00"), "fc_end": pd.Timestamp("2026-11-11 12:00")})

    def run(model, init, snow_per_hour):
        vs = pd.date_range("2026-11-10 12:00", "2026-11-11 23:00", freq="h")
        inside = (vs >= st.fc_start) & (vs < st.fc_end)
        return pd.DataFrame({"model": model, "init": pd.Timestamp(init), "point": "alta", "member": np.nan,
                             "lead_hr": 1, "valid_start": vs, "valid_end": vs + pd.Timedelta(hours=1),
                             "qpf_mm": np.where(inside, 2.0, 0.0), "snow_in": np.where(inside, snow_per_hour, 0.0)})
    # 12 storm hours: GFS 6 -> 9.6" (up), IFS 6 -> 6.6" (same), GDPS 6 -> 3.6" (down); median +0.6" = SAME
    fc = pd.concat([run("GFS", "2026-11-10 00:00", 0.5), run("GFS", "2026-11-10 06:00", 0.8),
                    run("IFS", "2026-11-10 00:00", 0.5), run("IFS", "2026-11-10 06:00", 0.55),
                    run("GDPS", "2026-11-10 00:00", 0.5), run("GDPS", "2026-11-10 12:00", 0.3)])
    text = "\n".join(storm_trend_lines(st, fc, cfg))
    assert "trend: storm snow vs each model's previous run" in text
    alta = next(ln for ln in text.splitlines() if ln.startswith("alta"))
    assert "SAME" in alta and '+0.6"' in alta and "1 / 1 / 1" in alta
    up = pd.concat([fc[fc["model"] != "IFS"], run("IFS", "2026-11-10 00:00", 0.5), run("IFS", "2026-11-10 06:00", 0.9)])
    alta = next(ln for ln in storm_trend_lines(st, up, cfg) if ln.startswith("alta"))
    assert "UP" in alta and '+3.6"' in alta and "2 / 1 / 0" in alta
    snowbird = next(ln for ln in storm_trend_lines(st, fc, cfg) if ln.startswith("snowbird"))
    assert snowbird.split()[1:] == ["-", "-", "-"]                          # no runs there: nothing to compare


def test_storm_trend_counts_only_runs_that_cover_the_whole_storm():
    # owner 2026-11-10: a run that reaches more of the storm than the one before naturally "trends up"; only runs
    # covering the whole window may be compared
    from cottonwoods.report import storm_trend_lines
    cfg = make_cfg()
    st = pd.Series({"fc_start": pd.Timestamp("2026-11-11 00:00"), "fc_end": pd.Timestamp("2026-11-11 12:00")})

    def run(model, init, last_valid, snow_per_hour=0.5):
        vs = pd.date_range("2026-11-10 12:00", last_valid, freq="h", inclusive="left")
        inside = (vs >= st.fc_start) & (vs < st.fc_end)
        return pd.DataFrame({"model": model, "init": pd.Timestamp(init), "point": "alta", "member": np.nan,
                             "lead_hr": 1, "valid_start": vs, "valid_end": vs + pd.Timedelta(hours=1),
                             "qpf_mm": np.where(inside, 2.0, 0.0), "snow_in": np.where(inside, snow_per_hour, 0.0)})
    # HRRR's older run ends 6 h into the storm (3"), the newer covers it all (6"): not a trend, left out
    partial = pd.concat([run("HRRR", "2026-11-10 00:00", "2026-11-11 06:00"),
                         run("HRRR", "2026-11-10 06:00", "2026-11-12 00:00")])
    alta = next(ln for ln in storm_trend_lines(st, partial, cfg) if ln.startswith("alta"))
    assert alta.split()[1:] == ["-", "-", "-"]
    # the newest run partial (ends early) and the older one full: also left out
    late = pd.concat([run("HRRR", "2026-11-10 00:00", "2026-11-12 00:00"),
                      run("HRRR", "2026-11-10 06:00", "2026-11-11 06:00")])
    assert next(ln for ln in storm_trend_lines(st, late, cfg) if ln.startswith("alta")).split()[1:] == ["-", "-", "-"]
    # once two runs cover it all, it counts: same snow = SAME
    both = pd.concat([partial, run("HRRR", "2026-11-10 12:00", "2026-11-12 00:00")])
    alta = next(ln for ln in storm_trend_lines(st, both, cfg) if ln.startswith("alta"))
    assert "SAME" in alta and '+0.0"' in alta and "0 / 0 / 1" in alta


def test_block_table_explains_the_dot_and_marks_blocks_the_storm_only_partly_fills():
    # plan 10/30: "." means under 0.05" of SNOW (dry or rain), and "Fri day" was only 6 h of 12
    from cottonwoods.report import storm_block_lines
    cfg = make_cfg()
    t = pd.date_range("2026-11-01 12:00", "2026-11-03 12:00", freq="1h")
    fc = pd.DataFrame([{"model": "GFS", "init": pd.Timestamp("2026-11-01 12:00"), "point": "alta",
                        "valid_start": a, "valid_end": b, "qpf_mm": 1.0, "snow_in": 0.0 if a < pd.Timestamp("2026-11-02 12:00") else 0.5}
                       for a, b in zip(t[:-1], t[1:])])
    st = pd.Series({"fc_start": pd.Timestamp("2026-11-02 06:00"), "fc_end": pd.Timestamp("2026-11-03 00:00")})   # 6 h + 12 h
    lines = storm_block_lines(st, fc, cfg)
    assert '"." = under 0.05" of snow (dry or rain)' in lines[0]
    head = next(line for line in lines if line.startswith("model"))
    assert head.split()[2:] == ["Sun", "ngt", "6h", "Mon", "day"]       # first block 6 of 12 h; the second whole
    gfs = next(line for line in lines if line.startswith("GFS"))
    assert gfs.split()[3:] == [".", "6.0"]                               # rain all of the first block: "."
