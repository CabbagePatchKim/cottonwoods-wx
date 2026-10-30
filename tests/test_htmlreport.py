"""The HTML storm report (htmlreport.py) and its numbers: probabilities (probs.py) and timelines / trends (trends.py)."""
import numpy as np
import pandas as pd

from cottonwoods import htmlreport, probs, trends

from helpers import make_cfg

S, E = pd.Timestamp("2026-11-02 00:00"), pd.Timestamp("2026-11-03 00:00")


def _run(model, init, point="alta", qpf=None, snow_per_mm=0.4, member=None, step=3, hours=72):
    """A run at one point in `step` h steps from init: qpf (mm per step) inside the storm window (S-E), else 0."""
    init = pd.Timestamp(init)
    ends = pd.date_range(init + pd.Timedelta(hours=step), init + pd.Timedelta(hours=hours), freq=f"{step}h")
    starts = ends - pd.Timedelta(hours=step)
    q = np.array([qpf if S <= s < E else 0.0 for s in starts], float) if qpf is not None else np.zeros(len(ends))
    df = pd.DataFrame({"model": model, "init": init, "lead_hr": (ends - init) / pd.Timedelta(hours=1),
                       "valid_start": starts, "valid_end": ends, "point": point, "qpf_mm": q})
    if member is None:
        df["snow_in"] = q * snow_per_mm
    else:
        df["member"] = float(member)
    return df


def test_member_shares_use_the_owners_lowest_bin():
    cfg = make_cfg()
    assert probs.bin_shares([4.0, 4.5, 7.0, np.nan], cfg["verify"]) == {"2-4": 1 / 3, "3-6": 1 / 3, "4-8": 1 / 3}
    w = probs.water_shares([0.2, 0.6, 1.1, 2.5], [0.25, 0.5, 1.0, 2.0])
    assert w == {0.25: 0.75, 0.5: 0.75, 1.0: 0.5, 2.0: 0.25}


def test_ensemble_members_borrow_their_familys_snow_ratio():
    cfg = make_cfg()
    st = pd.Series({"fc_start": S, "fc_end": E})
    gfs = _run("GFS", "2026-11-01 00:00", qpf=2.54, snow_per_mm=0.5)      # 0.1"/step water, 12.7" snow per inch
    gefs = pd.concat([_run("GEFS", "2026-11-01 00:00", qpf=q, member=m, step=6)
                      for m, q in enumerate([0.0, 2.54, 5.08, 10.16])], ignore_index=True)   # 0, 0.4, 0.8, 1.6" water
    e = probs.ensemble_probs(st, pd.concat([gfs, gefs], ignore_index=True), cfg, "alta")[0]
    assert e["model"] == "GEFS" and e["members"] == 4 and e["full"] and e["family"][0] == "GFS"
    assert abs(e["water"][0.25] - 0.75) < 1e-9 and abs(e["water"][1.0] - 0.25) < 1e-9
    # snow = water x GFS's 12.7"/in: 0, 5.1, 10.2, 20.3 -> the lowest bins holding them: 0-2, 3-6, 8-12, 18-24
    assert e["snow_bins"] == {"0-2": 0.25, "3-6": 0.25, "8-12": 0.25, "18-24": 0.25}


def test_time_lagged_high_res_set_counts_only_runs_covering_the_storm_and_block_by_block():
    cfg = make_cfg()
    st = pd.Series({"fc_start": S, "fc_end": E})
    runs = [_run("HRRR", "2026-11-01 12:00", qpf=1.0, step=1, hours=48),
            _run("HRRR", "2026-11-01 18:00", qpf=2.0, step=1, hours=48),
            _run("NAMNEST", "2026-11-01 12:00", qpf=1.0, step=1, hours=26)]       # ends 11/02 14z, before the storm
    fc = pd.concat(runs, ignore_index=True)
    lg = probs.lagged_probs(st, fc, cfg, "alta")
    assert lg["members"] == 2 and lg["short"] == 1
    blk = probs.lagged_block_probs(st, fc, cfg, "alta")
    assert blk[0]["members"] == 3 and blk[1]["members"] == 2                        # NAM Nest covers 00-12z only


def test_timeline_finds_onset_peak_and_end_from_three_hour_rates():
    cfg = make_cfg()
    hours = pd.date_range(S, E, freq="h", inclusive="left")
    snow = np.zeros(len(hours))
    snow[3:9] = 0.5                                                     # 0.5"/h from 03z to 09z
    snow[6] = 2.0                                                       # a burst at 06z (that hour: 2.0, not 0.5)
    steps = pd.DataFrame({"init": S - pd.Timedelta(hours=6), "valid_start": hours, "valid_end": hours + pd.Timedelta(hours=1),
                          "qpf_mm": snow * 2.54, "snow_in": snow})
    t = trends.timeline(steps, S, E, cfg)
    assert t["onset"] == pd.Timestamp("2026-11-02 01:00")               # first 3 h window reaching 0.1"/h: 01-04z
    assert t["end"] == pd.Timestamp("2026-11-02 11:00")                 # last one: 08-11z
    assert t["peak_start"] in (pd.Timestamp("2026-11-02 04:00"), pd.Timestamp("2026-11-02 05:00"),
                               pd.Timestamp("2026-11-02 06:00")) and abs(t["peak_rate"] - 1.0) < 1e-9
    assert abs(t["snow"] - 4.5) < 1e-9                                  # 5 hours x 0.5 + 2.0


def test_trends_compare_the_newest_full_run_with_the_previous_and_read_the_direction():
    cfg = make_cfg()
    st = pd.Series({"fc_start": S, "fc_end": E})
    fc = pd.concat([_run("GFS", f"2026-11-01 {h:02d}:00", qpf=q) for h, q in [(0, 1.0), (6, 2.0), (12, 3.0)]],
                   ignore_index=True)
    m = trends.model_trends(st, fc, cfg, "alta")[0]
    assert m["model"] == "GFS" and len(m["runs"]) == 3 and m["runs"][0]["init"] == pd.Timestamp("2026-11-01 12:00")
    assert abs(m["change"]["snow_in"] - 8 * 0.4) < 1e-9 and m["direction"] == "up"       # 8 wet steps x 1 mm x 0.4
    assert trends.direction([5.0, 5.4, 4.8], 1.0) == "steady" and trends.direction([5, 9, 4], 1.0) == "mixed"


def test_confidence_from_spread_and_ensemble_agreement():
    c = make_cfg()["html"]["confidence"]
    assert htmlreport.confidence(0.3, 0.9, c) == "high"
    assert htmlreport.confidence(0.7, 0.9, c) == "medium" and htmlreport.confidence(0.3, 0.6, c) == "medium"
    assert htmlreport.confidence(1.2, 0.9, c) == "low" and htmlreport.confidence(0.3, 0.4, c) == "low"


def test_the_page_renders_a_storm_with_its_summary_chances_timeline_and_noaa_section():
    cfg = make_cfg()
    pts = list(cfg["points"])
    fc = pd.concat([_run(m, f"2026-11-01 {h:02d}:00", point=p, qpf=q) for p in pts
                    for m, h, q in [("GFS", 0, 1.0), ("GFS", 6, 2.0), ("IFS", 0, 1.5), ("IFS", 6, 1.5)]]
                   + [_run("GEFS", "2026-11-01 06:00", point=p, qpf=q, member=k, step=6)
                      for p in pts for k, q in enumerate([0.5, 2.0, 4.0])], ignore_index=True)
    storms = pd.DataFrame([{"storm_id": "2026110200", "point": "alta", "status": "forecast", "fc_start": S, "fc_end": E,
                            "defining_model": "GFS", "defining_init": pd.Timestamp("2026-11-01 06:00"),
                            "fc_flow": "SW warm -2C 100%", "outside_note": "", "fc_modes": "", "fc_subperiods": "",
                            "fc_periods": "", "note": ""}])
    ghcn = pd.DataFrame({"site": "alta_noaa", "element": ["PRCP", "SNOW", "SNWD"], "value": [0.5, 6.0, 10.0],
                         "trace": False, "time_utc": pd.Timestamp("2026-10-30 01:00")})
    html = htmlreport.render_html(storms, fc, cfg, pd.Timestamp("2026-11-01 12:00"), "full text <here>", ghcn)
    assert html.startswith("<!doctype html>") and html.count("<h2>") == 2      # the storm + NOAA ALTA
    assert "Alta" in html and 'class="bin"' in html and "uncalibrated" in html
    assert "Timeline at Alta" in html and "snow via GFS" in html and "Thu 10/29" in html
    assert "full text &lt;here&gt;" in html                                     # the text report, escaped
