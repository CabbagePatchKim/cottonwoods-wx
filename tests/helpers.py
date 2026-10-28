"""Shared test fixtures: fake pullers and fake observation frames (synthetic data, no network)."""
"""Offline tests for the archive and verification pipeline, using synthetic data only (csv store, temp dirs)."""
import copy
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from cottonwoods.archive import forecasts as fa
from cottonwoods.archive import obs as oa
from cottonwoods.config import load_config


# These tests never touch the network. They swap in fake pullers/fetchers that return made-up but
# predictable data, write to a throwaway temporary folder, and check the archive and verification math.


def make_cfg():
    # deepcopy: a fully independent copy of the config, so changing it here can't leak into other tests
    cfg = copy.deepcopy(load_config())
    cfg["archive"]["format"] = "csv"
    for i, k in enumerate(cfg["snotel"]):
        cfg["snotel"][k]["triplet"] = f"{100 + i}:UT:SNTL"
    return cfg


# A function that returns a function: the inner `pull` has the same signature as a real model puller,
# so archive_run can't tell the difference.
def fake_puller(step_hours=1, qpf_mm=1.0, tK=268.0):
    """Mimics a model puller: constant precip per step, constant layer temperature."""
    def pull(init, fxx, points, save_dir=None):
        rows = []
        for name in points:
            prev = 0
            for f in fxx:
                row = {
                    "model": "HRRR", "init": pd.Timestamp(init), "lead_hr": f,
                    "valid_start": pd.Timestamp(init) + pd.Timedelta(hours=prev),
                    "valid_end": pd.Timestamp(init) + pd.Timedelta(hours=f),
                    "point": name, "grid_dist_km": 1.0,
                    "qpf_mm": qpf_mm * (f - prev),
                }
                row.update({f"t_{lev}_k": tK for lev in (850, 800, 750, 700, 650, 600, 550, 500)})
                rows.append(row)
                prev = f
        return pd.DataFrame(rows)
    return pull


def snotel_frame(trip, start_local, hours, base=10.0, rate=0.05, elements=("WTEQ",)):
    """A SNOTEL site's hourly readings rising `rate` inches per hour: the pillow (WTEQ), and the gauge (PREC) if asked."""
    t = pd.date_range(start_local, periods=hours, freq="h")
    return pd.concat([pd.DataFrame({"station": trip, "element": e, "time": t, "value": base + rate * np.arange(hours)})
                      for e in elements], ignore_index=True)


def gauge_frame(stid, start_utc, hours, base=5.0, rate=0.04, reset_at=None):
    """Synoptic-style gauge precip (UTC). reset_at: hour index where the gauge is emptied back to 0."""
    v = base + rate * np.arange(hours)
    if reset_at is not None:
        v[reset_at:] = rate * np.arange(hours - reset_at)
    t = pd.date_range(start_utc, periods=hours, freq="h")
    return pd.DataFrame({"station": stid, "element": "PREC", "time_utc": t, "value": v})


def build_world(step_hours=1, hour_override=None):
    cfg = make_cfg()
    cfg["archive"]["gfs_max_fxx"] = 48  # keep the 3-hourly test model to the same 48 h as the hourly one
    model = "HRRR" if step_hours == 1 else "GFS"
    if hour_override is not None:
        cfg["verify"]["window_end_utc_hour"] = hour_override
    trip = cfg["snotel"]["atwater"]["triplet"]
    init = datetime(2026, 11, 2, 0)
    fxx = list(range(step_hours, 49, step_hours))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        fa.archive_run(model, init, cfg, root=root,
                       puller=lambda i, f, p, s=None: fake_puller()(i, fxx, p, s))
        oa.archive_snotel(cfg, "2026-11-02", "2026-11-05", root=root,
                          fetcher=lambda *a, **k: snotel_frame(trip, "2026-11-02 00:00", 60, elements=("WTEQ", "PREC")))
        oa.archive_synoptic(cfg, "2026-11-02", "2026-11-05", root=root,
                            fetcher=lambda *a, **k: gauge_frame("CLN", "2026-11-02 07:00", 60))
        fc = fa.load_forecasts(cfg, root=root)
        obs = oa.load_obs(root=root)
    return cfg, fc, obs


def ghcn_csv(rows):
    """NCEI daily-summaries CSV text for ALTA, as the real service returns it (attributes include obs time)."""
    lines = ['"STATION","DATE","PRCP","PRCP_ATTRIBUTES","SNOW","SNOW_ATTRIBUTES"']
    for date, prcp, pattr, snow, sattr in rows:
        lines.append(f'"USC00420072","{date}","{prcp}","{pattr}","{snow}","{sattr}"')
    return "\n".join(lines) + "\n"


def fake_sounding(t, t700=-10.0):
    """A simple Salt Lake City-like profile: surface 870 mb, cooling 6.5 C/km, westerly winds."""
    p = np.array([870.0, 850, 800, 750, 700, 600, 500, 400])
    z = np.array([1289.0, 1480, 1950, 2450, 2990, 4200, 5600, 7200])
    temp = t700 - 6.5 * (z - 2990) / 1000.0
    return pd.DataFrame({"station": "KSLC", "time_utc": pd.Timestamp(t), "levelcode": 4, "pressure_hpa": p,
                         "height_m": z, "temp_c": temp, "dewpoint_c": temp - 2.0, "wind_dir": 270.0, "wind_kt": 30.0})


def fake_ensemble(members=3, qpf_mm=1.0):
    def pull(init, fxx, points, save_dir=None):
        rows, prev = [], 0
        for f in fxx:
            for name in points:
                for m in range(members):
                    rows.append({"model": "GEFS", "init": pd.Timestamp(init), "member": m, "lead_hr": f,
                                 "valid_start": pd.Timestamp(init) + pd.Timedelta(hours=prev),
                                 "valid_end": pd.Timestamp(init) + pd.Timedelta(hours=f), "point": name,
                                 "qpf_mm": qpf_mm * m * (f - prev)})     # member 0 dry, others wetter
            prev = f
        return pd.DataFrame(rows)
    return pull


def hourly(start, mm_per_hour):
    """Hourly precip steps from a list of mm values, starting at `start`."""
    t = pd.Timestamp(start)
    return pd.DataFrame({"valid_start": [t + pd.Timedelta(hours=i) for i in range(len(mm_per_hour))],
                         "valid_end": [t + pd.Timedelta(hours=i + 1) for i in range(len(mm_per_hour))],
                         "qpf_mm": mm_per_hour})


def snotel_prec(start, hourly_in, base=10.0, site="atwater"):
    t = pd.date_range(start, periods=len(hourly_in) + 1, freq="h")
    return pd.DataFrame({"site": site, "station": "x", "element": "PREC", "time_utc": t,
                         "value": base + np.concatenate([[0.0], np.cumsum(hourly_in)])})
