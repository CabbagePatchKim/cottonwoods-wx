"""The owner's snowfall bins and the bin score (cottonwoods.bins)."""
import numpy as np
import pandas as pd

from helpers import make_cfg


def test_an_amount_goes_in_the_lowest_bin_that_contains_it():
    from cottonwoods.bins import bin_index, label, snow_bins
    v = make_cfg()["verify"]
    b = snow_bins(v)
    assert [label(b[bin_index(x, b)]) for x in (0, 2, 2.5, 4, 4.5, 40)] == ["0-2", "0-2", "1-3", "2-4", "3-6", "34-40"]
    big = snow_bins(v, 50)                                      # above 40: 6" wide, stepping by 4
    assert [label(x) for x in big[-4:]] == ["34-40", "38-44", "42-48", "46-52"]
    assert label(big[bin_index(41, big)]) == "38-44"


def test_bin_score_rewards_the_right_bin_and_dings_misses_by_two_or_more_bins():
    from cottonwoods.bins import bin_score
    v = make_cfg()["verify"]
    s = bin_score(4.0, 0.0, v)                                  # owner's example: 4" (2-4) and nothing fell (0-2)
    assert (s["fc_bin"], s["obs_bin"], s["bins_off"], s["hit"], s["points"]) == ("2-4", "0-2", 2, False, -1)
    assert bin_score(4.5, 0.0, v)["points"] == -2               # 3-6 vs 0-2: three bins off
    assert bin_score(3.0, 0.0, v)["points"] == 0                # 1-3 vs 0-2: one bin off
    right = bin_score(4.5, 4.0, v)                              # 3-6 forecast, 4" observed: inside the bin
    assert right["hit"] and right["points"] == 2 and right["obs_bin"] == "2-4"
    assert bin_score(0.0, 0.0, v)["points"] == 2                # calling a dry storm dry is right too
    assert bin_score(2.0, 12.0, v)["bins_off"] < 0              # too low: negative bins_off


def test_dry_storm_is_scored_without_hand_entries_and_wet_storm_waits_for_them():
    from cottonwoods.report import render_verified
    from cottonwoods.storms import bin_scores, observed_snow
    cfg = make_cfg()
    st = pd.Series({"storm_id": "2026102700", "point": "alta", "status": "verified",
                    "fc_start": pd.Timestamp("2026-10-28 00:00"), "fc_end": pd.Timestamp("2026-10-28 12:00"),
                    "final_start": pd.Timestamp("2026-10-28 00:00"), "final_end": pd.Timestamp("2026-10-28 12:00"),
                    "obs_water_in": 0.0})
    assert observed_snow(st, pd.DataFrame(), None, cfg) == (0.0, "SNOTEL dry")
    assert np.isnan(observed_snow(pd.Series({**st, "obs_water_in": 0.5}), pd.DataFrame(), None, cfg)[0])
    scores = pd.DataFrame({"storm_id": "2026102700", "point": "alta",
                           "model": ["NAMNEST", "NAMNEST", "HRRR", "GEFS"],
                           "init": pd.to_datetime(["2026-10-25 00:00", "2026-10-27 18:00", "2026-10-27 18:00",
                                                   "2026-10-27 18:00"]),
                           "member": [np.nan, np.nan, np.nan, 0], "lead_to_start_hr": [72, 6, 6, 6],
                           "fc_water_in": [0.1, 0.4, 0.0, 0.2], "fc_snow_in": [1.0, 4.0, 0.0, np.nan],
                           "obs_water_in": 0.0, "fc_event": [True, True, False, True], "obs_event": False})
    b = bin_scores(pd.DataFrame([st]), scores, pd.DataFrame(), None, cfg)
    assert len(b) == 3 and "GEFS" not in set(b.model)          # ensembles have no snow: not bin-scored
    assert b.set_index(["model", "lead_to_start_hr"])["points"].to_dict() == {
        ("NAMNEST", 72): 0, ("NAMNEST", 6): -1, ("HRRR", 6): 2}             # 1" in the right bin, but a false alarm
    text = render_verified(pd.DataFrame([st]), scores, pd.DataFrame(), cfg, pd.Timestamp("2026-10-29"), binned=b)
    assert "observed bin: 0-2" in text and "2-4 -1" in text and "0-2 +2" in text
    assert "-1 (0 of 1 right)" in text and "+2 (1 of 1 right)" in text   # NAMNEST: last run -1, 72 h run +2
    later = render_verified(pd.DataFrame([st]), scores, pd.DataFrame(), cfg, pd.Timestamp("2026-11-20"), binned=b)
    assert "none" in later and "+2 (1 of 1 right)" in later    # weeks later: no recent storm, season totals stay
    assert "NAMNEST" not in later                              # but retired 11/3 (archive.retired): off the tables


def test_season_scores_are_averaged_by_how_far_ahead_each_run_was_issued():
    from cottonwoods.report import bin_totals
    cfg = make_cfg()
    binned = pd.DataFrame({"storm_id": "s1", "point": "alta", "member": np.nan, "fc_bin": "0-2", "hit": True,
                           "model": ["GFS", "GFS", "GFS", "HRRR"], "init": pd.to_datetime(
                               ["2026-10-22 00:00", "2026-10-26 00:00", "2026-10-27 18:00", "2026-10-27 18:00"]),
                           "lead_to_start_hr": [144, 48, 6, 6], "points": [-1, 2, 2, 2],
                           "snow_err_in": [3.0, 0.5, 0.0, 0.0], "fc_water_in": 0.0, "fc_snow_in": [3.0, 0.5, 0.0, 0.0]})
    t = bin_totals(binned, cfg).set_index("model")
    assert t.at["GFS", "0-24 h"] == '+2.0 0.0" (1)' and t.at["GFS", "1-3 d"] == '+2.0 0.5" (1)'
    assert t.at["GFS", "3-7 d"] == '-1.0 3.0" (1)' and t.at["HRRR", "3-7 d"] == ""   # HRRR never reaches that far
