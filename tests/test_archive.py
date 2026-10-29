"""The forecast archive: one file per run, completeness checks, derived fields."""
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from cottonwoods.archive import forecasts as fa
from cottonwoods.verify import build_pairs

from helpers import make_cfg, fake_puller, fake_ensemble


def test_archive_run_is_idempotent_and_writes_kuchera():
    cfg = make_cfg()
    init = datetime(2026, 11, 2, 0)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        path, status = fa.archive_run("HRRR", init, cfg, root=root, puller=fake_puller())
        assert status == "archived" and path.exists()
        _, status2 = fa.archive_run("HRRR", init, cfg, root=root, puller=fake_puller())
        assert status2 == "exists"
        df = fa.load_forecasts(cfg, root=root)
        assert {"tmax_k", "slr_kuchera", "snow_in", "fetched_at"} <= set(df.columns)
        assert abs(df["slr_kuchera"].iloc[0] - (12 + (271.16 - 268.0))) < 1e-9
        manifest = pd.read_csv(root / "manifest.csv")
        assert len(manifest) == 1 and manifest.n_valid.iloc[0] == manifest.n_expected.iloc[0]


def test_incomplete_run_is_not_archived():
    cfg = make_cfg()
    init = datetime(2026, 11, 2, 0)
    base = fake_puller()

    def partial(init, fxx, points, save_dir=None):
        return base(init, list(fxx)[:20], points, save_dir)  # only 20 of 48 hours posted

    with tempfile.TemporaryDirectory() as d:
        try:
            fa.archive_run("HRRR", init, cfg, root=Path(d), puller=partial)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "retry" in str(e)
        assert fa.load_forecasts(cfg, root=Path(d)).empty


def test_load_forecasts_filters_by_model_and_init():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for h in (0, 6):
            fa.archive_run("HRRR", datetime(2026, 11, 2, h), cfg, root=root, puller=fake_puller())
        assert fa.load_forecasts(cfg, start="2026-11-02 06:00", root=root)["init"].nunique() == 1
        assert fa.load_forecasts(cfg, models=["GFS"], root=root).empty


def test_ensemble_completeness_counts_members_and_report_counts_wet_members():
    from cottonwoods.models import registry
    from cottonwoods.report import model_table
    cfg = make_cfg()
    init = datetime(2026, 11, 2, 0)
    orig = registry.REGISTRY["GEFS"]
    registry.REGISTRY["GEFS"] = registry.ModelSpec("GEFS", lambda i, c: list(range(6, 49, 6)), 7.0, members=3)
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # a run missing a member is incomplete (2 of 3 members = 67% < 90%)
            try:
                fa.archive_run("GEFS", init, cfg, root=root, puller=lambda i, f, p, s=None: fake_ensemble(3)(i, f, p, s).query("member < 2"))
                raise AssertionError("missing member should fail completeness")
            except RuntimeError:
                pass
            fa.archive_run("GEFS", init, cfg, root=root, puller=fake_ensemble(3))
            fa.archive_run("HRRR", init, cfg, root=root, puller=lambda i, f, p, s=None: fake_puller()(i, list(range(1, 49)), p, s))
            fc = fa.load_forecasts(cfg, root=root)
    finally:
        registry.REGISTRY["GEFS"] = orig
    assert "snow_in" not in fc[fc.model == "GEFS"].dropna(axis=1, how="all").columns   # precip only, no Kuchera
    lines = model_table(fc, cfg, "alta", days=2, start=pd.Timestamp("2026-11-02 00:00"))
    rows = {ln.split()[0]: ln for ln in lines[1:]}
    # the day ending 11/02 12z starts before the 00z run does, so it's incomplete -> "-". The 11/03 day is
    # complete: members get 0, 24 and 48 mm -> 2 of 3 reach 0.10" (2.54 mm), mean 24 mm = 0.94"
    assert lines[0].split() == ["Mon", "11/02", "Tue", "11/03"]
    assert rows["GEFS"].split()[3:] == ["-", "2/3", "0.94"]
    assert rows["HRRR"].split()[3:5] == ["-", f"{24 / 25.4:.2f}"]
    # every number ends in the same column as its sub-header (fixed widths)
    head = next(ln for ln in lines if ln.startswith("model") and "wet/N" in ln)
    assert rows["GEFS"].index("0.94") + 4 == head.rindex("mean") + 4
    assert not build_pairs(fc, pd.DataFrame(), cfg)["model"].eq("GEFS").any()       # ensembles skipped in verify


def test_snow_steps_get_minimum_ratio_and_rain_steps_none():
    from cottonwoods.archive.forecasts import derive_run_fields
    cfg = make_cfg()
    base = {"model": "X", "point": "alta", "qpf_mm": 25.4, "psfc_hpa": 780.0, "z_500_m": 5600.0}
    rows = []
    for t_stn, rh in ((4.0, 25.0), (6.0, 95.0)):        # warm & very dry (snow by wet-bulb) / warm & humid (rain)
        r = dict(base, t2m_k=273.15 + t_stn + 3.0, td2m_k=273.15 + t_stn + 3.0 - (25 if rh < 50 else 1))
        for lev in (850, 800, 750, 700, 650, 600, 550, 500):
            r[f"t_{lev}_k"] = 273.15 + t_stn - 0.0065 * (lev and (700 - lev) * 8.5) - 1.5
            r[f"rh_{lev}"] = rh
        rows.append(r)
    out = derive_run_fields(pd.DataFrame(rows), cfg)
    snowy, rain = out.iloc[0], out.iloc[1]
    assert snowy.snow_frac > 0 and snowy.slr_kuchera >= 3.0 and snowy.snow_in > 0     # wet snow, not zero
    assert rain.snow_frac == 0 and rain.snow_in == 0


# --- coarse models: points blend the 2x2 cells around them (owner, 2026-10-28) ---------------------------------

CELLS = {"lats": [40.50, 40.75], "lons": [-111.75, -111.50]}


def _cell_run(points, values, member=None):
    """One step of a fake GFS run: each cell (by name) gets its own numbers, each point a copy of cell[0]'s."""
    from cottonwoods.archive.cells import cell_name
    names = [cell_name("GFS", la, lo) for la in CELLS["lats"] for lo in CELLS["lons"]]
    rows = []
    for name, v in [*zip(names, values), *[(p, values[0]) for p in points]]:
        row = {"model": "GFS", "init": pd.Timestamp("2026-11-01"), "lead_hr": 3, "point": name, "grid_dist_km": 0.0,
               "valid_start": pd.Timestamp("2026-11-01"), "valid_end": pd.Timestamp("2026-11-01 03:00"), **v}
        if member is not None:
            row["member"] = member
        rows.append(row)
    return pd.DataFrame(rows)


def test_cell_weights_are_bilinear_and_clip_at_the_edges():
    from cottonwoods.archive.cells import weights
    w = weights("GFS", CELLS, 40.625, -111.625)                      # middle of the block
    assert all(abs(v - 0.25) < 1e-12 for v in w.values())
    w = weights("GFS", CELLS, 40.80, -111.80)                        # outside, beyond the NW corner
    assert w["gfs_40.75N_111.75W"] == 1.0 and abs(sum(w.values()) - 1) < 1e-12
    w = weights("GFS", CELLS, 40.55, -111.70)                        # 1/5 of the way N, 1/5 E
    assert abs(w["gfs_40.50N_111.75W"] - 0.64) < 1e-9 and abs(w["gfs_40.75N_111.50W"] - 0.04) < 1e-9


def test_points_take_the_blend_of_the_four_cells_and_cells_stay_as_pulled():
    from cottonwoods.archive.cells import blend_points
    cfg = make_cfg()
    cfg["model_cells"] = {"GFS": CELLS}
    cfg["points"] = {"mid": {"lat": 40.625, "lon": -111.625}, "nw": {"lat": 40.75, "lon": -111.75}}
    vals = [{"qpf_mm": q, "t_700_k": t} for q, t in ((1.0, 270.0), (2.0, 271.0), (3.0, 272.0), (6.0, 275.0))]
    df = _cell_run(["mid", "nw"], vals)
    out = blend_points(df, cfg).set_index("point")
    assert out.loc["mid", "qpf_mm"] == 3.0 and out.loc["mid", "t_700_k"] == 272.0     # mean of the four
    assert blend_points(df.assign(member=float("nan")), cfg).set_index("point").loc["mid", "qpf_mm"] == 3.0
    assert out.loc["nw", "qpf_mm"] == 3.0                                             # exactly the NW cell
    assert out.loc["gfs_40.50N_111.75W", "qpf_mm"] == 1.0 and out.loc["mid", "cell_blend"]
    assert blend_points(blend_points(df, cfg), cfg).equals(blend_points(df, cfg))     # re-deriving is safe


def test_a_model_marked_blend_false_keeps_its_nearest_cell():
    from cottonwoods.archive.cells import blend_points
    from cottonwoods.archive.forecasts import run_points
    cfg = make_cfg()
    assert cfg["model_cells"]["GEFS"]["blend"] is False and "blend" not in cfg["model_cells"]["GFS"]
    assert sum(p.startswith("gefs_") for p in run_points("GEFS", cfg)) == 4               # cells still archived
    cfg["model_cells"] = {"GFS": {**CELLS, "blend": False}}
    cfg["points"] = {"mid": {"lat": 40.625, "lon": -111.625}}
    df = _cell_run(["mid"], [{"qpf_mm": q} for q in (1.0, 2.0, 3.0, 6.0)])
    assert blend_points(df, cfg).equals(df)


def test_a_missing_cell_value_keeps_the_nearest_value_and_ensembles_blend_per_member():
    from cottonwoods.archive.cells import blend_points
    cfg = make_cfg()
    cfg["model_cells"] = {"GFS": CELLS}
    cfg["points"] = {"mid": {"lat": 40.625, "lon": -111.625}}
    vals = [{"qpf_mm": 1.0, "t_700_k": 270.0}, {"qpf_mm": 2.0, "t_700_k": float("nan")},
            {"qpf_mm": 3.0, "t_700_k": 272.0}, {"qpf_mm": 6.0, "t_700_k": 275.0}]
    out = blend_points(_cell_run(["mid"], vals), cfg).set_index("point")
    assert out.loc["mid", "qpf_mm"] == 3.0 and out.loc["mid", "t_700_k"] == 270.0     # nearest (cell 0) kept
    ens = pd.concat([_cell_run(["mid"], [{"qpf_mm": q * m} for q in (1.0, 2.0, 3.0, 6.0)], member=m) for m in (1, 2)])
    out = blend_points(ens, cfg)
    assert out[out["point"] == "mid"].set_index("member")["qpf_mm"].to_dict() == {1: 3.0, 2: 6.0}


def test_two_nearby_sites_in_different_nearest_cells_now_follow_elevation():
    # 10/28 IFS/GFS: Mill-D North (8,940 ft) was given a cold, wet NW cell and Solitude (9,040 ft) a warm SE cell
    # 28 km away, so the lower site got more snow. Blended, two sites ~1 km apart share almost the same column.
    from cottonwoods.archive.forecasts import derive_run_fields
    cfg = make_cfg()
    cfg["model_cells"] = {"GFS": CELLS}
    cfg["points"] = {"high": {"lat": 40.620, "lon": -111.620, "elev_ft": 9040},         # nearest: SE cell
                     "low": {"lat": 40.630, "lon": -111.630, "elev_ft": 8940}}          # nearest: NW cell
    def column(warm, qpf):
        r = {"qpf_mm": qpf, "psfc_hpa": 780.0, "z_500_m": 5600.0, "t2m_k": 273.15 + warm + 4, "td2m_k": 273.15 + warm + 3}
        for lev in (850, 800, 750, 700, 650, 600, 550, 500):
            r[f"t_{lev}_k"] = 273.15 + warm - (700 - lev) * 0.055
            r[f"rh_{lev}"] = 95.0
        return r
    # cells: SW, SE, NW, NE -- the NW cell is 2 C colder and wetter
    vals = [column(0.5, 5.0), column(0.5, 5.0), column(-1.5, 7.0), column(0.5, 5.0)]
    df = _cell_run(["high", "low"], vals)
    df.loc[df["point"] == "low", list(vals[2])] = list(vals[2].values())                 # nearest-cell copies
    out = derive_run_fields(df, cfg).set_index("point")
    assert out.loc["low", "snow_in"] <= out.loc["high", "snow_in"]
    assert abs(out.loc["low", "qpf_mm"] - out.loc["high", "qpf_mm"]) < 0.2


def test_rotated_grid_points_blend_inside_their_grid_square():
    # RDPS (2026-10-29): a rotated grid's block is its own grid points (config grid_points), not lat/lon lines
    from cottonwoods.archive.cells import cell_name, weights
    o, a, b = np.array([40.50, -111.70]), np.array([0.086, -0.035]), np.array([0.025, 0.110])   # rows, columns
    grid = [[list(o + r * a + c * b) for c in range(3)] for r in range(3)]
    cells = {"grid_points": grid}
    name = lambda r, c: cell_name("RDPS", *grid[r][c])                                       # noqa: E731
    w = weights("RDPS", cells, *(o + 0.25 * a + 0.6 * b))                  # inside the south-west square
    assert abs(w[name(0, 0)] - 0.75 * 0.4) < 1e-9 and abs(w[name(0, 1)] - 0.75 * 0.6) < 1e-9
    assert abs(w[name(1, 0)] - 0.25 * 0.4) < 1e-9 and abs(w[name(1, 1)] - 0.25 * 0.6) < 1e-9
    w = weights("RDPS", cells, *(o + 1.5 * a + 1.3 * b))                   # the north-east square
    assert abs(w[name(1, 1)] - 0.5 * 0.7) < 1e-9 and abs(w[name(2, 2)] - 0.5 * 0.3) < 1e-9
    w = weights("RDPS", cells, *grid[1][1])                                # on a grid point: that point only
    assert abs(max(w.values()) - 1) < 1e-9 and max(w, key=w.get) == name(1, 1)
    w = weights("RDPS", cells, *(o - 1.0 * a))                             # outside the block: clipped to its edge
    assert abs(sum(w.values()) - 1) < 1e-9 and min(w.values()) >= 0 and abs(w[name(0, 0)] - 1) < 1e-9
    # the real config: every point is blended inside the block, and Alta leans on RDPS's 9,204 ft peak point
    cfg = make_cfg()
    for p, pt in cfg["points"].items():
        w = weights("RDPS", cfg["model_cells"]["RDPS"], pt["lat"], pt["lon"])
        assert abs(sum(w.values()) - 1) < 1e-9 and min(w.values()) >= 0, p
    w = weights("RDPS", cfg["model_cells"]["RDPS"], cfg["points"]["alta"]["lat"], cfg["points"]["alta"]["lon"])
    assert max(w, key=w.get) == "rdps_40.60N_111.62W" and 0.5 < w["rdps_40.60N_111.62W"] < 0.65


def test_a_blended_model_also_archives_its_unmodified_run_apart():
    # owner 2026-10-29: the blend and the unmodified model data are stored separately; both are scored
    from cottonwoods.archive.cells import cell_name
    from cottonwoods.archive.io import read_df
    cfg = make_cfg()
    cfg["model_cells"] = {"GFS": CELLS}
    cfg["points"] = {"mid": {"lat": 40.625, "lon": -111.625, "elev_ft": 9000}}
    cell_qpf = {cell_name("GFS", la, lo): q for (la, lo), q in zip(
        [(la, lo) for la in CELLS["lats"] for lo in CELLS["lons"]], [1.0, 2.0, 3.0, 6.0])}
    def pull(init, fxx, points, save_dir=None):
        rows = []
        for name in points:
            q = cell_qpf.get(name, 1.0)                                    # "mid": its nearest cell's 1.0
            prev = 0
            for f in fxx:
                rows.append({"model": "GFS", "init": pd.Timestamp(init), "lead_hr": f, "point": name,
                             "valid_start": pd.Timestamp(init) + pd.Timedelta(hours=prev),
                             "valid_end": pd.Timestamp(init) + pd.Timedelta(hours=f), "grid_dist_km": 1.0,
                             "qpf_mm": q, **{f"t_{lev}_k": 265.0 for lev in (850, 700, 500)}})
                prev = f
        return pd.DataFrame(rows)
    init = datetime(2026, 11, 2, 12)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        fa.archive_run("GFS", init, cfg, root=root, puller=pull)
        main = read_df(fa.run_path("GFS", init, cfg, root)).set_index("point")
        raw = read_df(fa.run_path("GFS", init, cfg, root, raw=True))
        assert (main.loc["mid", "qpf_mm"] == 3.0).all()                    # the blend: (1 + 2 + 3 + 6) / 4
        assert set(raw["point"]) == {"mid"} and (raw["qpf_mm"] == 1.0).all() and raw["snow_in"].notna().all()
        assert "cell_blend" not in raw or not raw["cell_blend"].fillna(False).any()
        assert set(fa.load_forecasts(cfg, root=root, raw=True)["model"]) == {"GFS raw"}
        assert set(fa.load_forecasts(cfg, root=root)["model"]) == {"GFS"}  # reports + feed never see it
        hrrr = fa.archive_run("HRRR", init, cfg, root=root, puller=fake_puller())[0]
        assert hrrr.exists() and not fa.run_path("HRRR", init, cfg, root, raw=True).exists()   # not blended


def test_grid_relative_winds_are_turned_to_true_north_once():
    from cottonwoods import winds
    cfg = make_cfg()
    assert round(winds.grid_angle("HRRR", 40.5764, -111.6383), 2) == 8.80        # measured on real files: 8.80
    assert round(winds.grid_angle("NAM", 40.5764, -111.6383), 1) == 7.0           # 7.01
    assert round(winds.grid_angle("RDPS", 40.5764, -111.6383), 1) == 16.9         # 16.84 over one grid step
    assert winds.grid_angle("GFS", 40.5764, -111.6383) == 0.0
    west = pd.DataFrame({"model": "HRRR", "point": ["alta"], "u_700_ms": [10.0], "v_700_ms": [0.0]})
    once = winds.earth_winds(west, cfg)
    a = np.radians(winds.grid_angle("HRRR", cfg["points"]["alta"]["lat"], cfg["points"]["alta"]["lon"]))
    assert abs(once["u_700_ms"].iloc[0] - 10 * np.cos(a)) < 1e-9 and abs(once["v_700_ms"].iloc[0] - 10 * np.sin(a)) < 1e-9
    pd.testing.assert_frame_equal(winds.earth_winds(once, cfg), once)              # derived again: not turned twice
    cell = winds.earth_winds(west.assign(model="RDPS", point="rdps_40.60N_111.62W"), cfg)   # RDPS's own grid points
    a = np.radians(winds.grid_angle("RDPS", 40.6050, -111.6220))
    assert abs(cell["u_700_ms"].iloc[0] - 10 * np.cos(a)) < 1e-9
    gfs = winds.earth_winds(west.assign(model="GFS"), cfg)
    assert gfs["u_700_ms"].iloc[0] == 10.0 and gfs["uv_earth"].all()


def test_derivation_marks_its_version_so_older_runs_can_be_rederived():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        path, _ = fa.archive_run("HRRR", datetime(2026, 11, 2, 12), cfg, root=root, puller=fake_puller())
        df = fa.read_df(path)
        assert (df["derive_version"] == fa.DERIVE_VERSION).all()
        again = fa.derive_run_fields(df.drop(columns=[c for c in fa.DERIVED if c in df]), cfg)
        pd.testing.assert_series_equal(again["snow_in"], df["snow_in"], check_names=False)
