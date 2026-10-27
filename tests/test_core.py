import numpy as np
import pandas as pd

from cottonwoods import journal, modes, regimes, slr
from cottonwoods.config import load_config
from cottonwoods.models.points import nearest_index


# Each test_ function checks one behavior with `assert`. Floats are compared with a small tolerance
# (abs(a - b) < 1e-9) rather than ==, because decimal math like 0.1 + 0.2 isn't exact in binary.

def test_kuchera_values():
    assert abs(slr.kuchera_slr(271.16) - 12.0) < 1e-9
    assert abs(slr.kuchera_slr(265.16) - 18.0) < 1e-9          # 6 K colder -> +6
    assert abs(slr.kuchera_slr(275.16) - (12 - 2 * 4.0)) < 1e-9  # 4 K warmer -> -8 (double slope)


def test_kuchera_clamp_and_array():
    out = slr.kuchera_slr(np.array([250.0, 280.0]), floor=3, cap=25)
    assert out[0] == 25 and out[1] == 3


def test_layer_tmax_uses_only_requested_levels():
    temps = pd.DataFrame({1000: [280.0], 850: [270.0], 700: [265.0], 500: [250.0]})
    assert slr.layer_tmax(temps, 850, 500).iloc[0] == 270.0


def test_wind_direction():
    assert abs(regimes.wind_dir_deg(0.0, -10.0) - 0.0) < 1e-6      # wind from the north
    assert abs(regimes.wind_dir_deg(-10.0, 0.0) - 90.0) < 1e-6     # from the east
    assert abs(regimes.wind_dir_deg(10.0, 0.0) - 270.0) < 1e-6     # from the west
    assert abs(regimes.wind_dir_deg(0.0, 10.0) - 180.0) < 1e-6     # from the south


def test_nearest_index_2d_and_lon_convention():
    lats = np.array([[40.0, 40.0], [41.0, 41.0]])
    lons = np.array([[-112.0, -111.0], [-112.0, -111.0]])
    idx, d = nearest_index(lats, lons, 40.9, -111.1)
    assert idx == (1, 1) and d < 20
    # grid in 0-360 convention should still match a -180..180 request
    idx2, _ = nearest_index(lats, lons % 360, 40.9, -111.1)
    assert idx2 == (1, 1)


def test_journal_loads_and_scores():
    fc = journal.score_journal()
    assert {"storm_id", "model", "mid_in", "error_in"} <= set(fc.columns)
    row = fc[(fc.storm_id == "2023-11-23") & (fc.model == "HRRR")].iloc[0]
    assert row.actual_in == 10 and row.hit and row.error_in == 0
    # open-ended forecasts keep a NaN high bound
    assert fc[fc.open_ended].high_in.isna().all()


def test_swe_increase_ignores_sensor_flicker_and_later_melt():
    from cottonwoods.obs.snotel import swe_increase
    t = pd.date_range("2026-02-16", periods=60, freq="h")
    flicker = 5.0 + 0.1 * (np.arange(60) % 2)            # dry: 5.0, 5.1, 5.0, 5.1, ...
    df = pd.DataFrame({"station": "s", "element": "WTEQ", "time": t, "value": flicker})
    assert swe_increase(df, "s") < 0.15                  # summing upticks would give ~3"
    storm = np.concatenate([np.full(20, 5.0), np.linspace(5.0, 7.0, 20), np.linspace(7.0, 6.5, 20)])
    df["value"] = storm + 0.1 * (np.arange(60) % 2)
    assert abs(swe_increase(df, "s") - 2.0) < 0.15       # rise to the peak, not reduced by the melt after it


def test_layer_tmax_ignores_underground_levels_and_slr_never_negative():
    temps = pd.DataFrame({850: [285.0], 750: [270.0], 700: [265.0], 500: [250.0]})   # 850 is extrapolated
    assert slr.layer_tmax(temps, 850, 500).iloc[0] == 285.0                           # old behavior
    assert slr.layer_tmax(temps, 850, 500, pd.Series([760.0])).iloc[0] == 270.0       # surface at 760 hPa
    assert slr.kuchera_slr(285.0) == 0.0                                              # rain, not -16:1



# --- station profile, wet-bulb, snow level ---------------------------------------------------------

from cottonwoods import profile  # noqa: E402

LEVS = [850, 800, 750, 700, 650, 600, 550, 500]
PCFG = load_config()["precip_type"]


def make_row(t700_c, rh=60.0, psfc=780.0, t2m_c=None, td2m_c=None, lapse=6.5, z500=5600.0):
    """A step like a coarse model at Alta: ground near 780 mb (~2,150 m), temps falling `lapse` K/km aloft."""
    z700 = 3050.0
    row = {"psfc_hpa": psfc, "z_500_m": z500}
    for lev in LEVS:
        z = z700 + (np.log(700 / lev) * 287.05 / 9.80665 * 265.0)
        row[f"t_{lev}_k"] = t700_c + 273.15 - lapse * (z - z700) / 1000
        row[f"rh_{lev}"] = rh
    row["t2m_k"] = (t2m_c if t2m_c is not None else row["t_750_k"] - 273.15 + 3.0) + 273.15
    row["td2m_k"] = (td2m_c if td2m_c is not None else row["t2m_k"] - 273.15 - 4.0) + 273.15
    return row


def test_wet_bulb_known_values():
    td = profile.dewpoint_c(20.0, 50.0)                                     # textbook: 20 C at 50% RH -> 13.7 C
    assert abs(profile.wet_bulb_c(20.0, td, 1013.25) - 13.7) < 0.3
    assert abs(profile.wet_bulb_c(5.0, 5.0, 750.0) - 5.0) < 0.01            # saturated air: Tw = T
    assert profile.wet_bulb_c(2.0, -8.0, 750.0) < 0.0                       # dry +2 C air: wet-bulb below freezing


def test_station_profile_snow_in_dry_air_above_freezing_with_dry_nudge():
    # station air ~+3 C: humid air -> rain; very dry air -> mostly snow (evaporative cooling), but the NWS-style
    # dry-air nudge keeps it from counting as pure snow; the snow level sits between wet-bulb and freezing level
    humid = profile.station_profile(make_row(t700_c=1.0, rh=75.0), LEVS, z_station_m=2652.0, pcfg=PCFG)
    dry = profile.station_profile(make_row(t700_c=1.0, rh=30.0), LEVS, z_station_m=2652.0, pcfg=PCFG)
    assert humid["t_stn_c"] > 2.5 and humid["snow_frac"] == 0.0
    assert dry["t_stn_c"] > 2.5 and dry["tw_stn_c"] < 0 and dry["dry_nudge"] == 0.5 and 0.5 < dry["snow_frac"] < 1.0
    assert dry["snow_level_m"] < dry["freezing_level_m"]


def test_station_profile_moves_kuchera_to_the_station_and_snow_level_falls():
    # warm valley air below Alta: model ground ~2,150 m at +8 C, colder at the station
    warm_below = make_row(t700_c=-4.0, rh=90.0, t2m_c=8.0, td2m_c=6.0)
    out = profile.station_profile(warm_below, LEVS, z_station_m=2652.0, pcfg=PCFG)
    assert out["tmax_stn_k"] < 273.15                       # the +8 C valley air no longer counts for Kuchera
    assert out["z_ground_m"] < 2652.0
    # rain at 12,000 ft freezing level, then colder: snow level drops below the station
    warm = profile.station_profile(make_row(t700_c=6.0, rh=95.0), LEVS, z_station_m=2945.0, pcfg=PCFG)
    cold = profile.station_profile(make_row(t700_c=-6.0, rh=95.0), LEVS, z_station_m=2945.0, pcfg=PCFG)
    assert warm["snow_frac"] == 0.0 and warm["snow_level_m"] > 3300
    assert cold["snow_frac"] == 1.0 and cold["snow_level_m"] < 2945.0


def test_level_heights_fills_from_500_mb():
    temps = {lev: 260.0 for lev in LEVS}
    z = profile.level_heights(LEVS, temps, {500: 5600.0})
    # isothermal 260 K: 500 -> 700 mb is Rd/g * 260 * ln(700/500) ~ 2,560 m thinner
    assert abs((z[500] - z[700]) - 287.05 / 9.80665 * 260 * np.log(1.4)) < 1e-6 and len(z) == len(LEVS)


def test_point_fields_reads_levels_and_surface_from_nested_lists():
    import xarray as xr
    from cottonwoods.models.fields import point_fields
    lat, lon = np.array([40.5, 40.75]), np.array([248.25, 248.5])
    lev = xr.Dataset({"t": (("isobaricInhPa", "latitude", "longitude"), np.full((2, 2, 2), 260.0)),
                      "r": (("isobaricInhPa", "latitude", "longitude"), np.full((2, 2, 2), 80.0))},
                     coords={"isobaricInhPa": [700, 500], "latitude": lat, "longitude": lon})
    sfc = xr.Dataset({"sp": (("latitude", "longitude"), np.full((2, 2), 78000.0)),
                      "t2m": (("latitude", "longitude"), np.full((2, 2), 271.0))},
                     coords={"latitude": lat, "longitude": lon})
    out = point_fields([[lev], sfc], {"alta": {"lat": 40.59, "lon": -111.64}})["alta"]
    assert out["t_700_k"] == 260.0 and out["rh_500"] == 80.0 and out["psfc_hpa"] == 780.0 and out["t2m_k"] == 271.0


# --- regimes through a storm ------------------------------------------------------------------

def regime_steps(dirs_temps, start="2026-10-31 00:00", mm=1.0):
    """Hourly steps with a 700 mb wind of 15 m/s from each given (direction, 700 mb temp C)."""
    t = pd.Timestamp(start)
    rows = []
    for i, (d, tc) in enumerate(dirs_temps):
        rows.append({"valid_start": t + pd.Timedelta(hours=i), "valid_end": t + pd.Timedelta(hours=i + 1), "point": "alta",
                     "qpf_mm": mm, "u_700_ms": -15 * np.sin(np.radians(d)), "v_700_ms": -15 * np.cos(np.radians(d)),
                     "t_700_k": tc + 273.15})
    return pd.DataFrame(rows)


def test_flow_bins_wrap_through_north():
    cfg = load_config()["regimes"]
    assert [regimes.flow_label(d, cfg) for d in (220, 260, 300, 350, 10, 100)] == ["SW", "W", "NW", "N", "N", "S/E"]


def test_storm_phases_track_a_veer_and_ignore_a_one_hour_flip():
    cfg = load_config()["regimes"]
    # 10 h SW warm, a 1 h W blip, 2 more h SW, then 8 h NW cold: two phases, not four
    steps = regime_steps([(220, -3)] * 10 + [(260, -3)] + [(220, -3)] * 2 + [(300, -12)] * 8)
    ph = regimes.phases(regimes.add_step_regimes(steps, cfg), cfg)
    assert [p["regime"] for p in ph] == ["SW warm", "NW cold"]
    assert abs(ph[0]["share"] - 13 / 21) < 1e-9 and ph[1]["t700_c"] < -11
    assert regimes.phase_string(ph) == "SW warm 62% -> NW cold 38%"


def test_regime_factors_apply_per_step():
    cfg = load_config()["regimes"]
    steps = regimes.add_step_regimes(regime_steps([(220, -3)] * 2 + [(300, -12)] * 2), cfg)
    steps["snow_in"] = 1.0
    f = pd.DataFrame({"point": "alta", "flow": ["SW", "NW", "NW"], "thermal": ["", "", "cold"],
                      "factor": [0.9, 1.1, 1.2], "n_days": [100, 50, 25]})
    out = regimes.apply_factors(steps, f, cfg)
    assert list(out.regime_factor) == [0.9, 0.9, 1.2, 1.2]     # SW steps get SW; cold NW steps get NW-cold
    assert abs(out.snow_in_adj.sum() - 4.2) < 1e-9



def test_precip_modes_frontal_then_postfrontal_cellular_and_lake_effect():
    cfg = load_config()["regimes"]
    # 6 h SW warm ahead of the front, sharp cooling + veer to NW, then cold NW flow off the lake in December
    seq = [(220, -2)] * 6 + [(250, -5), (290, -9)] + [(310, -14)] * 8
    steps = regime_steps(seq, start="2026-12-05 00:00")
    steps["t_500_k"] = steps["t_700_k"] - 14.0          # stable-ish 700-500 lapse (~5 C/km)
    steps["z_500_m"], steps["z_700_m"] = 5600.0, 3000.0
    steps["rh_700"], steps["cape_jkg"] = 90.0, 0.0
    out = modes.add_precip_modes(regimes.add_step_regimes(steps, cfg), cfg)
    got = list(out.sort_values("valid_end").precip_mode)
    assert got[0] == "frontal"                         # stable, ahead of the front
    assert "frontal" in got[6:9]                       # the passage itself
    # December lake ~3 C vs 700 mb -14 C: delta 17 C >= 16, flow 310 off the lake, moist -> lake-effect
    assert got[-1] == "lake-effect"
    # same post-frontal steps but unstable and dry at 700 mb -> cellular, not lake-effect
    steps2 = steps.copy(); steps2["rh_700"] = 40.0; steps2["cape_jkg"] = 150.0
    out2 = modes.add_precip_modes(regimes.add_step_regimes(steps2, cfg), cfg)
    assert out2.sort_values("valid_end").precip_mode.iloc[-1] == "cellular"
    s = modes.mode_string(out.sort_values("valid_end"))
    assert s.startswith("frontal") and s.endswith("%") and "lake-effect" in s


def test_front_of_an_earlier_storm_does_not_label_a_later_one():
    cfg = load_config()["regimes"]
    first = [(220, -2)] * 4 + [(290, -9)] * 4                  # storm 1: front passes
    dry = [(250, -8)] * 30                                      # 30 h dry gap
    second = [(220, -3)] * 8                                    # storm 2: steady SW, no front
    steps = regime_steps(first + dry + second, start="2026-11-20 00:00")
    steps.loc[8:37, "qpf_mm"] = 0.0
    steps["t_500_k"], steps["z_500_m"], steps["z_700_m"], steps["rh_700"], steps["cape_jkg"] = steps["t_700_k"] - 14, 5600.0, 3000.0, 90.0, 0.0
    out = modes.add_precip_modes(regimes.add_step_regimes(steps, cfg), cfg).sort_values("valid_end")
    assert set(out.precip_mode.iloc[-8:]) == {"frontal"}       # storm 2 is pre-frontal/stratiform, not "post-frontal"



def test_report_times_are_mountain_24h_and_follow_dst():
    from cottonwoods import timefmt
    assert timefmt.clock("2026-10-27 04:49") == "10/26 2249 MDT"            # UTC-6 before DST ends
    assert timefmt.clock("2026-11-01 09:30") == "11/01 0230 MST"            # UTC-7 after 2 am 11/1
    assert timefmt.clock("2026-11-03 12:00", weekday=True) == "Tue 11/03 0500 MST"
    assert timefmt.run("2026-10-26 18:00") == "10/26 18z (1200 MDT)"
    assert timefmt.now_line("2026-10-27 04:49") == "made Mon 10/26 2249 MDT (0449 UTC)"



def test_config_check_names_every_missing_setting():
    from cottonwoods.config import ConfigError, check_config
    cfg = load_config()
    broken = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    del broken["precip_type"]["snow_min_slr"]
    del broken["storms"]
    try:
        check_config(broken)
        raise AssertionError("should have failed")
    except ConfigError as e:
        assert "precip_type.snow_min_slr" in str(e) and "storms:" in str(e)
