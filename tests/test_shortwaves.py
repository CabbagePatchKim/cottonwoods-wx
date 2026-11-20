"""500 mb shortwave tracking and vorticity advection (shortwaves.py)."""
import numpy as np
import pandas as pd

from cottonwoods import shortwaves as sw

from helpers import make_cfg

LATS = np.arange(35.0, 45.01, 0.25)
LONS = np.arange(-120.0, -104.99, 0.25)
ALTA = (40.59, -111.64)


def _blob(lat0, lon0, amp=4e-4, width_deg=1.0):
    la, lo = np.meshgrid(LATS, LONS, indexing="ij")
    return amp * np.exp(-(((la - lat0) / width_deg) ** 2 + ((lo - lon0) / width_deg) ** 2))


def _rows(t, vort, z):
    la, lo = np.meshgrid(LATS, LONS, indexing="ij")
    return pd.DataFrame({"time": t, "lat": la.ravel(), "lon": lo.ravel(), "vort500_s": vort.ravel(),
                         "z500_m": z.ravel()})


def test_a_shortwave_moving_east_is_found_tracked_and_timed_at_the_point():
    cfg = make_cfg()["shortwaves"]
    # a trough 1 deg south of Alta moving east 1.5 deg lon per 6 h (~21 km/h), crossing -111.64 at hour 13.44,
    # plus a weak blob that never reaches the threshold
    t0 = pd.Timestamp("2026-11-20 00:00")
    frames = []
    for k in range(6):
        lon = -117.0 + 1.5 * k
        vort = _blob(39.59, lon) + _blob(43.0, -108.0, amp=2e-5)
        frames.append(_rows(t0 + pd.Timedelta(hours=6 * k), vort, np.full(vort.shape, 5500.0)))
    series, tracks, cross = sw.analyse(pd.concat(frames), *ALTA, cfg)
    assert tracks["track"].nunique() == 1 and len(tracks) == 6                  # the weak blob is not a trough
    assert len(cross) == 1
    c = cross.iloc[0]
    expect = t0 + pd.Timedelta(hours=6 * (-111.64 + 117.0) / 1.5)
    assert abs((c["time"] - expect) / pd.Timedelta(minutes=1)) < 30
    assert abs(c["dlat"] + 1.0) < 0.26 and c["anom"] > cfg["min_anomaly_s"]
    assert 100 < c["speed_kmh"] * 6 < 160                                      # ~127 km per 6 h at 39.6 N
    assert len(series) == 6


def test_tracks_do_not_jump_backwards_or_too_far():
    cfg = make_cfg()["shortwaves"]
    t0 = pd.Timestamp("2026-11-20 00:00")
    found = pd.DataFrame({"time": [t0, t0 + pd.Timedelta(hours=6), t0 + pd.Timedelta(hours=12)],
                          "lat": [40.0, 40.0, 40.0], "lon": [-112.0, -116.0, -90.0], "anom": 1e-4})
    tr = sw.track(found, {**cfg, "min_maps": 1})
    # 4 deg west in 6 h: not the same trough; 22 deg east in 12 h (~1,870 km, 156 km/h) from the first: too fast
    assert tr["track"].nunique() == 3
    ok = sw.track(found.assign(lon=[-112.0, -116.0, -105.0]), {**cfg, "min_maps": 1})
    assert ok["track"].nunique() == 2          # 7 deg in 12 h (50 km/h, where expected) joins the first, skipping a map


def test_geostrophic_vorticity_advection_sign():
    # westerly geostrophic flow (heights fall northward) carrying a vorticity maximum: PVA downstream (east)
    # of the maximum, NVA upstream (west)
    la, lo = np.meshgrid(LATS, LONS, indexing="ij")
    z = 5600.0 - 30.0 * (la - 35.0)                                            # ~20 m/s westerly
    vort = _blob(40.0, -114.0)
    adv = sw.vort_advection(z, vort, LATS, LONS)
    east = sw.disk_mean(adv, LATS, LONS, 40.0, -112.5, 60)
    west = sw.disk_mean(adv, LATS, LONS, 40.0, -115.5, 60)
    assert east > 0 > west
    # no vorticity gradient and a straight flow: only planetary advection (small, beta x v = 0 for pure westerly)
    flat = sw.vort_advection(z, np.zeros_like(z), LATS, LONS)
    assert np.nanmax(np.abs(flat[2:-2, 2:-2])) < 1e-12


def test_analyses_are_stitched_from_each_runs_6h_map():
    rows = []
    for h in (0, 6):
        init = pd.Timestamp("2026-11-20") + pd.Timedelta(hours=h)
        for lead in (6, 12):
            rows.append(pd.DataFrame({"init": [init], "lead_hr": [lead], "lat": [40.0], "lon": [-111.5],
                                      "z500_m": [5500.0 + lead], "vort500_s": [0.0]}))
    runs = [pd.concat(rows[:2]), pd.concat(rows[2:])]
    st = sw.stitch_analyses(runs)
    assert list(st["time"]) == [pd.Timestamp("2026-11-20 06:00"), pd.Timestamp("2026-11-20 12:00")]
    assert (st["z500_m"] == 5506.0).all()
    # backfill maps hold the 0 h ANALYSIS: it is each run's earliest map, and beats another run's 6 h map at the
    # same valid time
    ana = pd.DataFrame({"init": [pd.Timestamp("2026-11-20 06:00")], "lead_hr": [0], "lat": [40.0], "lon": [-111.5],
                        "z500_m": [5400.0], "vort500_s": [0.0]})
    st2 = sw.stitch_analyses([runs[0], ana])
    assert st2.loc[st2["time"] == pd.Timestamp("2026-11-20 06:00"), "z500_m"].tolist() == [5400.0]
