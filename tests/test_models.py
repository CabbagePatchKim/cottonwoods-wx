"""Model-side logic: precip buckets, cycles and schedules, precip-message choice, grid cells."""
import math
from datetime import datetime

import numpy as np


from cottonwoods.archive import forecasts as fa
from cottonwoods.models import accum
from cottonwoods.models.registry import cycles_between, recent_cycles
from cottonwoods.verify import build_pairs

from helpers import make_cfg, build_world


def test_parse_acc_window():
    assert accum.parse_acc_window("surface 0-6 hour acc fcst") == (0, 6)
    assert accum.parse_acc_window("6-12 hour acc fcst") == (6, 12)
    assert accum.parse_acc_window("anl") is None


def test_deaccumulate_gfs_like_buckets():
    recs = [(0, 3, 1.0), (0, 6, 3.0), (6, 9, 0.5), (6, 12, 1.5), (12, 15, 2.0), (12, 18, 2.5)]
    steps = accum.deaccumulate(recs)
    assert steps == [(0, 3, 1.0), (3, 6, 2.0), (6, 9, 0.5), (9, 12, 1.0), (12, 15, 2.0), (15, 18, 0.5)]
    assert abs(sum(s[2] for s in steps) - 7.0) < 1e-9  # = 0-6 (3.0) + 6-12 (1.5) + 12-18 (2.5)


def test_deaccumulate_hourly_resetting_buckets():
    # f001 0-1, f002 0-2, f003 0-3, f004 3-4, f005 3-5, f006 0-6
    recs = [(0, 1, 1.0), (0, 2, 2.0), (0, 3, 3.0), (3, 4, 1.0), (3, 5, 2.0), (0, 6, 4.0)]
    steps = accum.deaccumulate(recs)
    assert [round(s[2], 6) for s in steps] == [1.0, 1.0, 1.0, 1.0, 1.0, 0.0]


def test_deaccumulate_missing_bucket_start_is_nan_not_guess():
    steps = accum.deaccumulate([(0, 3, 1.0), (6, 9, 0.5)])  # f006 missing, so the 6-9 bucket has no resolved start
    assert math.isnan(steps[1][2])


def test_recent_cycles_respects_lag():
    cfg = make_cfg()
    now = datetime(2026, 11, 2, 15, 30)
    hrrr = recent_cycles("HRRR", cfg, now, 2)   # lag 2.5 h -> 13:00 -> latest synoptic is 12z
    assert hrrr == [datetime(2026, 11, 2, 12), datetime(2026, 11, 2, 6)]
    gfs = recent_cycles("GFS", cfg, now, 2)     # lag 5.5 h -> 10:00 -> 06z, then 00z
    assert gfs == [datetime(2026, 11, 2, 6), datetime(2026, 11, 2, 0)]


def test_cycles_between_oldest_first():
    cfg = make_cfg()
    c = cycles_between("GFS", cfg, datetime(2026, 11, 1, 3), datetime(2026, 11, 2, 0))
    assert c == [datetime(2026, 11, 1, 6), datetime(2026, 11, 1, 12), datetime(2026, 11, 1, 18), datetime(2026, 11, 2, 0)]


def test_off_cycle_runs_are_refused_for_every_model():
    cfg = make_cfg()
    for model in ("HRRR", "GFS"):
        try:
            fa.archive_run(model, datetime(2026, 11, 2, 3), cfg, puller=lambda *a, **k: None)
            raise AssertionError("an 03z run should be refused")
        except ValueError as e:
            assert "shared cycles" in str(e)
    assert cycles_between("HRRR", cfg, datetime(2026, 11, 1, 0), datetime(2026, 11, 1, 23)) == \
        [datetime(2026, 11, 1, h) for h in (0, 6, 12, 18)]


def test_choose_precip_prefers_since_start_total_then_latest_bucket():
    from cottonwoods.models.accum import choose_precip
    nam = [":APCP:surface:0-12 hour acc fcst:", ":APCP:surface:9-12 hour acc fcst:"]
    assert choose_precip(nam, 12)[1] == (0, 12)
    gefs = [":APCP:surface:6-9 hour acc fcst:ENS=+1:"]
    assert choose_precip(gefs, 9)[1] == (6, 9)
    ecmwf = [":tp:sfc:g:0001:od:fc:oper:"]            # no window text = total since start
    assert choose_precip(ecmwf, 24)[1] == (0, 24)
    assert choose_precip([":APCP:surface:0-6 hour acc fcst:"], 9) is None   # wrong hour -> nothing


def test_model_schedules_and_gdps_cycles():
    from cottonwoods.models.registry import REGISTRY, cycles
    cfg = make_cfg()
    assert cycles(cfg, "GDPS") == [0, 12] and cycles(cfg, "RDPS") == [0, 6, 12, 18]
    ifs00, ifs06 = REGISTRY["IFS"].fxx_list(datetime(2026, 11, 2, 0), cfg), REGISTRY["IFS"].fxx_list(datetime(2026, 11, 2, 6), cfg)
    assert ifs00[-1] == 360 and 147 not in ifs00 and 150 in ifs00 and ifs06[-1] == 144
    ens = REGISTRY["ENS"].fxx_list(datetime(2026, 11, 2, 6), cfg)
    assert ens[:3] == [6, 18, 30] and all((6 + h) % 12 == 0 for h in ens)   # valid at 12 and 00 UTC only
    try:
        fa.archive_run("GDPS", datetime(2026, 11, 2, 6), cfg, puller=lambda *a, **k: None)
        raise AssertionError("GDPS 06z should be refused")
    except ValueError:
        pass


def test_gfs_runs_also_store_the_2x2_cells_and_verify_ignores_them():
    cfg = make_cfg()
    pts = fa.run_points("GFS", cfg)
    cells = [p for p in pts if p.startswith("gfs_")]
    assert sorted(cells) == ["gfs_40.50N_111.50W", "gfs_40.50N_111.75W", "gfs_40.75N_111.50W", "gfs_40.75N_111.75W"]
    assert set(fa.run_points("HRRR", cfg)) == set(cfg["points"])       # HRRR is 3 km; no extra cells
    cfg2, fc, obs = build_world(step_hours=3)                          # a GFS-like run, cells included
    assert set(fc.point) >= set(cells)
    assert not build_pairs(fc, obs, cfg2)["point"].str.startswith("gfs_").any()


def test_every_coarse_model_stores_its_own_2x2_cells():
    cfg = make_cfg()
    expect = {"GFS": "gfs_40.50N_111.75W", "IFS": "ifs_40.75N_111.50W", "ENS": "ens_40.50N_111.50W",
              "GDPS": "gdps_40.65N_111.60W", "GEFS": "gefs_41.00N_112.00W"}
    for model, name in expect.items():
        pts = fa.run_points(model, cfg)
        cells = [p for p in pts if p not in cfg["points"]]
        assert len(cells) == 4 and name in cells, model
    for model in ("HRRR", "NAMNEST", "NAM", "RDPS"):   # fine grids: resort points only
        assert set(fa.run_points(model, cfg)) == set(cfg["points"]), model



def test_one_missing_hour_only_merges_neighbors_with_since_start_totals():
    # since-start totals at 3-hourly steps with f084 missing: only 81->87 merges; everything after survives
    recs = [(0, f, 0.1 * f) for f in range(3, 241, 3) if f != 84]
    steps = accum.deaccumulate(recs)
    assert not any(math.isnan(v) for _, _, v in steps)
    assert (81, 87, 0.1 * 87 - 0.1 * 81) in [(a, b, round(v, 9)) for a, b, v in steps] or         any(a == 81 and b == 87 for a, b, _ in steps)
    # the same gap with 6 h buckets (starting at 84) leaves every later step unknown: why totals are preferred
    buckets = [(6 * ((f - 1) // 6), f, 0.1) for f in range(3, 241, 3) if f != 84]
    assert math.isnan(accum.deaccumulate(buckets)[-1][2])


def test_parallel_hours_retries_a_dropped_connection():
    from cottonwoods.models.fields import parallel_hours
    tries, got = {}, {}

    def download(item):
        tries[item] = tries.get(item, 0) + 1
        if item == 2 and tries[item] == 1:
            raise ConnectionResetError("forcibly closed by the remote host")
        return item * 10

    failed = parallel_hours(download, lambda it, v: got.update({it: v}), [1, 2, 3], workers=2)
    assert failed == [] and got == {1: 10, 2: 20, 3: 30} and tries[2] == 2


def test_fast_nearest_index_matches_a_full_search():
    from cottonwoods.models import points
    rng = np.random.default_rng(0)
    lats, lons = np.arange(-90, 90.01, 0.15), np.arange(0, 360, 0.15)          # GDPS-like global grid
    la2, lo2 = np.meshgrid(np.linspace(38, 43, 60), np.linspace(-115, -108, 70), indexing="ij")
    la2 = la2 + 0.02 * np.sin(lo2)                                             # a curved (projected) grid
    for lat0, lon0 in zip(rng.uniform(39, 42, 25), rng.uniform(-113, -110, 25)):
        full = points._haversine_km(*np.meshgrid(lats, lons, indexing="ij"), lat0, lon0 % 360)
        assert points.nearest_index(lats, lons, lat0, lon0)[0] == np.unravel_index(np.argmin(full), full.shape)
        full2 = points._haversine_km(la2, lo2, lat0, lon0)
        first = points.nearest_index(la2, lo2, lat0, lon0)
        assert first[0] == np.unravel_index(np.argmin(full2), full2.shape)
        assert points.nearest_index(la2, lo2, lat0, lon0) == first            # cached answer is the same
