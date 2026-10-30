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
    rdps = [p for p in fa.run_points("RDPS", cfg) if p not in cfg["points"]]   # rotated grid: its own 3x3 grid points
    assert len(rdps) == 9 and "rdps_40.60N_111.62W" in rdps
    for model in ("HRRR", "NAMNEST", "NAM"):   # fine grids: resort points only
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

    failed = parallel_hours(download, lambda it, v: got.update({it: v}), [1, 2, 3], workers=2, sleep=lambda s: None)
    assert failed == [] and got == {1: 10, 2: 20, 3: 30} and tries[2] == 2


def test_fast_nearest_index_matches_a_full_search():
    from cottonwoods.models import points
    rng = np.random.default_rng(0)
    lats, lons = np.arange(-90, 90.01, 0.15), np.arange(0, 360, 0.15)          # GDPS-like global grid
    la2, lo2 = np.meshgrid(np.linspace(38, 43, 60), np.linspace(-115, -108, 70), indexing="ij")
    la2 = la2 + 0.02 * np.sin(lo2)                                             # a curved (projected) grid
    glat, glon = np.meshgrid(lats, lons, indexing="ij")
    for lat0, lon0 in zip(rng.uniform(39, 42, 8), rng.uniform(-113, -110, 8)):
        full = points._haversine_km(glat, glon, lat0, lon0 % 360)
        assert points.nearest_index(lats, lons, lat0, lon0)[0] == np.unravel_index(np.argmin(full), full.shape)
        full2 = points._haversine_km(la2, lo2, lat0, lon0)
        first = points.nearest_index(la2, lo2, lat0, lon0)
        assert first[0] == np.unravel_index(np.argmin(full2), full2.shape)
        assert points.nearest_index(la2, lo2, lat0, lon0) == first            # cached answer is the same


def test_each_puller_gets_its_servers_download_workers_from_config():
    from cottonwoods.config import load_config
    from cottonwoods.models.registry import get_puller
    cfg = load_config()
    w = cfg["archive"]["download_workers"]
    assert get_puller("HRRR", cfg).keywords == {"workers": w["noaa"]}
    assert get_puller("GFS", cfg).keywords == {"workers": w["noaa"], "gdex_workers": w["gdex"]}
    thr = cfg["archive"]["throttle"]                            # Herbie (S3) pullers also wait out throttling
    assert get_puller("IFS", cfg).keywords == {"workers": w["ecmwf"], "throttle": thr}
    assert get_puller("IFS", cfg).args == ("IFS",)
    assert get_puller("ENS", cfg).keywords == {"workers": w["ecmwf"], "throttle": thr}
    assert get_puller("GEFS", cfg).keywords == {"workers": w["noaa"], "throttle": thr}
    assert get_puller("GDPS", cfg).keywords == {"workers": w["canada"]} and get_puller("GDPS", cfg).args == ("GDPS",)


def test_canada_files_decode_from_memory_to_the_nearest_points_value():
    import eccodes
    import numpy as np
    from cottonwoods.models import canada
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set_key_vals(gid, {"Ni": 4, "Nj": 3, "latitudeOfFirstGridPointInDegrees": 41.0,
                                     "latitudeOfLastGridPointInDegrees": 40.0, "longitudeOfFirstGridPointInDegrees": 248.0,
                                     "longitudeOfLastGridPointInDegrees": 249.5, "iDirectionIncrementInDegrees": 0.5,
                                     "jDirectionIncrementInDegrees": 0.5})
    vals = np.arange(12, dtype=float) + 260.0           # row-major: lat 41.0, 40.5, 40.0 x lon 248.0 .. 249.5
    eccodes.codes_set_values(gid, vals)
    msg = eccodes.codes_get_message(gid)
    eccodes.codes_release(gid)
    out = canada._decode_points(msg, {"alta": {"lat": 40.59, "lon": -111.64}})
    value, dist = out["alta"]
    assert abs(value - 265.0) < 1e-6                     # nearest cell: lat 40.5 (row 1), lon 248.5 = -111.5 (col 1)
    assert dist < 25


def test_parallel_hours_waits_out_a_throttling_server_and_gives_up_after_its_tries():
    from cottonwoods.models.fields import parallel_hours
    slow = "RuntimeError('Processing failed: 503 Server Error: Slow Down for url: https://ecmwf-forecasts.s3...')"
    tries, got, waits = {}, {}, []

    def download(item):
        tries[item] = tries.get(item, 0) + 1
        if item == 2 and tries[item] <= 2:
            raise RuntimeError(slow)                            # throttled twice, then served
        if item == 3:
            raise RuntimeError(slow)                            # throttled every time
        return item * 10

    failed = parallel_hours(download, lambda it, v: got.update({it: v}), [1, 2, 3], workers=1,
                            throttle={"retries": 3, "wait_s": 30}, sleep=waits.append)
    assert got == {1: 10, 2: 20} and tries[2] == 3
    assert [f[0] for f in failed] == [3] and tries[3] == 1 + 3 + 2   # 3 throttle waits, then the 2 quick retries
    assert waits.count(30) == 2 and waits.count(60) == 2 and waits.count(90) == 1


def test_field_searches_that_find_nothing_are_left_out_not_fatal():
    import tempfile
    from pathlib import Path
    from cottonwoods.models.herbie_generic import SPECS, field_files

    with tempfile.TemporaryDirectory() as d:
        real = Path(d, "subset_surface.grib2")
        real.write_bytes(b"GRIB")

        class FakeHerbie:   # old Herbie raises for a search with no match; Herbie 2026.10 returns an unwritten path
            def download(self, search, verbose=False):
                return {"sfc": real, "pl": Path(d, "never_written.grib2")}.get(search) or _raise()

        def _raise():
            raise ValueError("no match")

        assert field_files(FakeHerbie(), ["sfc", "pl", "cape"]) == [real]
    # ECMWF's grid is named, so a Herbie update can't switch the archive to the 0.1 deg split files
    assert SPECS["IFS"]["herbie"]["resolution"] == SPECS["ENS"]["herbie"]["resolution"] == "0p25"


# --- model kinds, retirement, RRFS and the U of U guidance (owner, 2026-10-30) -----------------------------------

UU_HRRR = """BASED ON 00Z 30-OCT-2026 INITIALIZED HRRR
FHR  VALID MOUNTAIN TIME     AMB       ALT       IFF        AMB  ALT  CLN ATH20 ELBUT WBZ(ftMSL) CLN         CLN           CLN
  1  07 PM Thu Oct 29 2026   SSE11G27  SSE10G19  SSW13G21   32F  35F  39F  44F  41F    9000FT     6:1/17%    0.00/0.00     0.0/ 0.0
  2  08 PM Thu Oct 29 2026     S12G28    S10G19  SSW13G21   32F  35F  37F  41F  41F    8900FT     9:1/11%    0.10/0.10     0.9/ 0.9
  3  09 PM Thu Oct 29 2026   SSE13G28    S09G19  SSW12G21   31F  34F  36F  39F  39F    9100FT    12:1/ 8%    0.20/0.30     2.4/ 3.3
  4  10 PM Thu Oct 29 2026   SSE13G28    S09G19  SSW12G21   10F  11F  14F  17F  15F     <KSLC    14:1/ 7%    0.04/0.34     0.6/ 3.9
"""
UU_GFS = """FHR  VALID MOUNTAIN TIME     COLLINS T  MT BALDY T/RH  MT BALDY WIND       WETBULBZERO   SLR/H2O         1H QPF /  1H SNOW /  ACCUM QPF / ACCUM SNOW
0    06 PM Thu Oct 29 2026   38F        33F /  41%     SSE   7 G  18 MPH   8300 FT       11:1 /  9%      0.00"  /   0.0"       0.00"    /   0.0"
1    07 PM Thu Oct 29 2026   36F        32F /  51%     SSE  10 G  20 MPH   8300 FT       11:1 /  9%      0.05"  /   0.6"       0.05"    /   0.6"
FORECAST HOURS 123-180
FHR  VALID MOUNTAIN TIME     COLLINS T  MT BALDY T/RH  MT BALDY WIND       WETBULBZERO   SLR/H2O         3H QPF /  3H SNOW /  ACCUM QPF / ACCUM SNOW
4    10 PM Thu Oct 29 2026   25F        21F /  68%       W  17 G  27 MPH   \u2264 KSLC        15:1 /  7%      0.10"  /   1.5"       0.15"    /   2.1"
"""


def test_every_model_has_a_kind_and_the_reports_build_from_deterministic_rows_only():
    import pandas as pd
    from cottonwoods.models import registry as r
    assert r.kind_of("HRRR") == "deterministic" and r.kind_of("RRFS") == "deterministic" and r.kind_of("GFS raw") == "deterministic"
    assert r.kind_of("UUHRRR") == "guidance" and r.kind_of("RRFSENS") == "ensemble" and r.kind_of("GEFS") == "ensemble"
    assert set(r.models_of("guidance")) == {"UUHRRR", "UUGFS"} and r.REGISTRY["UUGFS"].based_on == "GFS"
    assert {m for ms in r.STREAMS.values() for m in ms} == set(r.REGISTRY)       # every model has a download stream
    fc = pd.DataFrame({"model": ["HRRR", "UUHRRR", "RRFSENS", "GEFS"], "member": [np.nan, np.nan, 1.0, 2.0]})
    assert r.deterministic(fc)["model"].tolist() == ["HRRR"]


def test_guidance_is_pulled_for_alta_only():
    cfg = make_cfg()
    assert list(fa.run_points("UUHRRR", cfg)) == ["alta"] and len(fa.run_points("RRFS", cfg)) == len(cfg["points"])


def test_a_retired_model_queues_no_runs_from_its_retirement_on():
    cfg = make_cfg()
    cfg["archive"]["retired"] = {"NAM": "2026-11-03T12:00"}
    before = recent_cycles("NAM", cfg, datetime(2026, 11, 3, 9), 2)
    assert before == [datetime(2026, 11, 3, 6), datetime(2026, 11, 3, 0)]                  # still running
    after = recent_cycles("NAM", cfg, datetime(2026, 11, 5, 12), 3)
    assert after == [datetime(2026, 11, 3, 6), datetime(2026, 11, 3, 0), datetime(2026, 11, 2, 18)]   # its last runs
    assert cycles_between("NAM", cfg, datetime(2026, 11, 3), datetime(2026, 11, 4))[-1] == datetime(2026, 11, 3, 6)
    assert recent_cycles("HRRR", cfg, datetime(2026, 11, 5, 12), 1)[0] > datetime(2026, 11, 4)    # others unaffected


def test_uu_guidance_text_becomes_model_steps_with_their_own_snow():
    from cottonwoods.models import uu
    cfg = make_cfg()
    h = uu.parse_text("UUHRRR", UU_HRRR, datetime(2026, 10, 30))
    assert h["lead_hr"].tolist() == [1, 2, 3, 4] and (h["point"] == "alta").all() and np.isnan(h["wbz_msl_m"].iloc[3])
    assert abs(h["qpf_mm"].sum() - 0.34 * 25.4) < 1e-9 and abs(h["snow_issued_in"].tolist()[2] - 2.4) < 1e-9
    assert h["slr_issued"].tolist() == [6.0, 9.0, 12.0, 14.0] and abs(h["wbz_msl_m"].iloc[0] - 9000 * 0.3048) < 1e-9
    g = uu.parse_text("UUGFS", UU_GFS, datetime(2026, 10, 30))
    assert g["lead_hr"].tolist() == [1, 4] and abs(g["snow_issued_in"].iloc[1] - 1.5) < 1e-9   # hour 0 dropped
    assert np.isnan(g["wbz_msl_m"].iloc[1])                                     # "<= KSLC": below the valley, no height
    d = fa.derive_run_fields(g, cfg)
    assert (d["snow_in"] == d["snow_issued_in"]).all()                          # their snow, their ratio


def test_utah_snow_ensemble_page_is_parsed_and_each_run_saved_once():
    import tempfile
    from pathlib import Path
    from cottonwoods.models import uu
    cfg = make_cfg()
    html = ("<h2>Utah Snow Ensemble (Experimental) Initialized 1200 UTC 29 Oct 2026 (82 members)<br>Total Snowfall (in) "
            "Guidance for 40.579 N</h2><table><thead><tr><th>&nbsp;</th><th>12 PM Thu 29 Oct 2026</th>"
            "<th>06 PM Thu 29 Oct 2026</th></tr></thead><tbody><tr><th>P50</th><td>0.0</td><td>2.5</td></tr>"
            "<tr><th>P90</th><td>0.1</td><td>\\-0.0</td></tr></tbody></table>")   # the page escapes "-" as "\-"
    init, df = uu.parse_use(html)
    assert init == datetime(2026, 10, 29, 12) and set(df["variable"]) == {"snow_total_in"}
    p50 = df[df["stat"] == "p50"].set_index("time_utc")["value"]
    assert p50[datetime(2026, 10, 30, 0)] == 2.5                                 # 6 PM MDT = 00z
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert uu.archive_use(cfg, root, fetch=lambda: html) == (init, "archived")
        assert uu.archive_use(cfg, root, fetch=lambda: html)[1] == "exists"
        assert uu.load_use(root=root)[0] == init


def test_ensemble_members_with_temperatures_keep_their_own_precip_modes():
    import pandas as pd
    from cottonwoods import modes
    cfg = make_cfg()["regimes"]
    t = pd.date_range("2026-11-02", periods=16, freq="3h")                    # 48 h
    wet = lambda hours: [2.0 if 3 * i in hours else 0.0 for i in range(16)]   # noqa: E731
    one = lambda m, q: pd.DataFrame({"point": "alta", "member": m, "valid_start": t, "valid_end": t + pd.Timedelta(hours=3),   # noqa: E731
                                     "qpf_mm": q, "t_700_k": 265.0, "t_500_k": 250.0, "flow_dir700": 250.0})
    df = pd.concat([one(1.0, wet({0, 3})), one(2.0, wet({30, 33}))], ignore_index=True)   # member 2 a day later
    out = modes.add_precip_modes(df, cfg)
    m1 = out[out["member"] == 1.0]
    assert m1[m1["qpf_mm"] > 0]["precip_mode"].notna().all()
    # member 1 is dry when member 2 snows: mixed into one run, those hours would sit inside member 2's event
    assert m1[m1["valid_start"] >= pd.Timestamp("2026-11-03 06:00")]["precip_mode"].isna().all()


def test_rrfs_totals_since_init_are_found_at_whole_days_too():
    import re
    from cottonwoods.models.rrfs import since_init
    assert re.search(since_init("APCP", 24), ":APCP:surface:0-1 day acc fcst:")
    assert re.search(since_init("APCP", 48), ":APCP:surface:0-48 hour acc fcst:")
    assert re.search(since_init("ASNOW", 13), ":ASNOW:surface:0-13 hour acc fcst:")
    assert not re.search(since_init("APCP", 24), ":APCP:surface:23-24 hour acc fcst:")   # never the last hour only
