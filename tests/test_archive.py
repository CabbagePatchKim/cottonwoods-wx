"""The forecast archive: one file per run, completeness checks, derived fields."""
import tempfile
from datetime import datetime
from pathlib import Path

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
    t = model_table(fc, cfg, "alta", days=2, start=pd.Timestamp("2026-11-02 00:00")).set_index("model")
    # the day ending 11/02 12z starts before the 00z run does, so it's incomplete -> "-". The 11/03 day is
    # complete: members get 0, 24 and 48 mm -> 2 of 3 reach 0.10" (2.54 mm), mean 24 mm = 0.94"
    assert t.loc["GEFS", "11/02"] == "-" and t.loc["GEFS", "11/03"] == "2/3 0.94"
    assert t.loc["HRRR", "11/03"].startswith(f"{24 / 25.4:.2f}")
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
