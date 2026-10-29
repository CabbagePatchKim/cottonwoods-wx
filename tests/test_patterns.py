"""Offline tests for the pattern archive (cottonwoods.patterns): synthetic grids, temp dirs, no network."""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from cottonwoods import bucket, patterns

from helpers import make_cfg


def _grid(res, values):
    """A global lat-lon dataset at `res` deg (0..360 longitudes, like GFS), each variable filled by values(lat, lon)."""
    lat = np.arange(90, -90 - res / 2, -res)
    lon = np.arange(0, 360, res)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    return xr.Dataset({k: (("latitude", "longitude"), f(la, lo)) for k, f in values.items()},
                      coords={"latitude": lat, "longitude": lon})


def test_box_is_cut_on_the_1_degree_grid_with_minus_180_longitudes_and_relative_vorticity():
    cfg = make_cfg()
    ds = _grid(1.0, {"gh": lambda la, lo: 5500 + la, "absv": lambda la, lo: 2 * patterns.OMEGA * np.sin(np.radians(la)),
                     "prmsl": lambda la, lo: np.full_like(la, 101300.0)})
    got = patterns.cut_box(ds, cfg["patterns"])
    z = got["z500_m"]
    assert (z["lat"].min(), z["lat"].max(), z["lon"].min(), z["lon"].max()) == (25, 60, -150, -90)
    assert len(z) == 36 * 61 and np.allclose(z["z500_m"], 5500 + z["lat"])
    assert np.allclose(got["vort500_s"]["vort500_s"], 0, atol=1e-12)       # absolute vorticity of a resting atmosphere
    assert np.allclose(got["mslp_hpa"]["mslp_hpa"], 1013.0)                  # Pa -> hPa


def test_finer_grids_are_averaged_over_each_1_degree_cell():
    cfg = make_cfg()
    checker = _grid(0.25, {"vo": lambda la, lo: np.where((np.round(la * 4) + np.round(lo * 4)) % 2 == 0, 1.0, -1.0) * 1e-4,
                           "t": lambda la, lo: np.full_like(la, 280.0)})
    got = patterns.cut_box(checker, cfg["patterns"])
    assert np.abs(got["vort500_s"]["vort500_s"]).max() < 2e-5             # grid-scale noise averages out
    assert np.allclose(got["t850_k"]["t850_k"], 280.0)                      # a smooth field is left alone


def test_the_utah_box_keeps_each_models_own_grid_points_unaveraged():
    cfg = make_cfg()
    checker = _grid(0.25, {"vo": lambda la, lo: np.where((np.round(la * 4) + np.round(lo * 4)) % 2 == 0, 1.0, -1.0) * 1e-4})
    got = patterns.native_box(checker, cfg["patterns"]["inner"])["vort500_s"]
    assert len(got) == 41 * 61                                              # 35-45 N x 120-105 W every 0.25 deg
    assert np.allclose(np.abs(got["vort500_s"]), 1e-4)                      # the shortwave-scale signal is kept
    assert (got["lat"].min(), got["lat"].max(), got["lon"].min(), got["lon"].max()) == (35, 45, -120, -105)


def test_a_run_is_archived_once_and_only_when_nearly_complete():
    cfg = make_cfg()
    full = pd.DataFrame([{"grid": "1deg", "lead_hr": h, "lat": 40.0, "lon": -112.0, "z500_m": 5700.0}
                         for h in patterns.steps(cfg)])
    init = datetime(2026, 10, 29)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        try:
            patterns.archive_pattern("GFS", init, cfg, root=root, puller=lambda *a, **k: full.iloc[:20])
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "still posting" in str(e)
        path, status = patterns.archive_pattern("GFS", init, cfg, root=root, puller=lambda *a, **k: full)
        assert status == "archived" and patterns.load_pattern("GFS", init, cfg, root)["lead_hr"].nunique() == 40
        assert patterns.archive_pattern("GFS", init, cfg, root=root, puller=None)[1] == "exists"


def test_bucket_pulls_only_recent_pattern_maps():
    now = datetime(2026, 10, 29)
    old, old_pattern = now - timedelta(days=25), now - timedelta(days=3)
    assert bucket.too_old("archive/patterns/model=GFS/init=2026102500.parquet", old, old_pattern)
    assert not bucket.too_old("archive/forecasts/model=GFS/init=2026102500.parquet", old, old_pattern)
    assert not bucket.too_old("archive/patterns/model=GFS/init=2026102812.parquet", old, old_pattern)
