"""Storm events (storms.py), window totals (windows.py) and report staleness (report.py)."""
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from cottonwoods.archive import forecasts as fa

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
