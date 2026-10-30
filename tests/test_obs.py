"""Observations: SNOTEL, Synoptic, NOAA coop and balloon soundings (parsing and the obs archive)."""
import tempfile
from pathlib import Path

import pandas as pd

from cottonwoods.archive import obs as oa

from helpers import make_cfg, snotel_frame, gauge_frame, ghcn_csv, fake_sounding


def test_snotel_archive_converts_to_utc_and_replaces_revisions():
    cfg = make_cfg()
    trip = cfg["snotel"]["atwater"]["triplet"]
    # TemporaryDirectory: a scratch folder that is deleted automatically when the `with` block ends.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # lambda *a, **k: ... accepts any arguments and ignores them, standing in for the real fetch()
        oa.archive_snotel(cfg, "2026-11-02", "2026-11-04", root=root,
                          fetcher=lambda *a, **k: snotel_frame(trip, "2026-11-02 00:00", 10))
        df = oa.load_snotel(root=root)
        assert df["time_utc"].min() == pd.Timestamp("2026-11-02 08:00")  # 00:00 PST (SNOTEL's clock) -> 08:00 UTC
        assert set(df["site"]) == {"atwater"}
        # a later fetch with a revised value for the same hours must replace, not duplicate
        revised = snotel_frame(trip, "2026-11-02 00:00", 10, base=20.0)
        oa.archive_snotel(cfg, "2026-11-02", "2026-11-04", root=root, fetcher=lambda *a, **k: revised)
        df = oa.load_snotel(root=root)
        assert len(df) == 10 and df["value"].min() == 20.0


def test_synoptic_archive_keeps_utc_and_maps_site():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        oa.archive_synoptic(cfg, "2026-11-02", "2026-11-04", root=root,
                            fetcher=lambda *a, **k: gauge_frame("CLN", "2026-11-02 07:00", 10))
        df = oa.load_synoptic(root=root)
        assert df["time_utc"].min() == pd.Timestamp("2026-11-02 07:00")  # already UTC, no shift
        assert set(df["site"]) == {"alta_collins"}
        assert oa.load_snotel(root=root).empty and len(oa.load_obs(root=root)) == 10


def test_ghcn_fetch_parses_obs_time_trace_and_quality_flags():
    from cottonwoods.obs import ghcn

    class R:
        text = ghcn_csv([("2023-12-03", "2.02", ",,7,1800", "12.8", ",,7,1800"),
                         ("2023-12-04", "0.00", "T,,7,1800", "0.0", "T,,7,1800"),   # trace
                         ("2023-12-05", "9.99", ",X,7,1800", "", "")])              # failed QC -> dropped
        def raise_for_status(self): pass

    orig = ghcn.requests.get
    ghcn.requests.get = lambda *a, **k: R()
    try:
        df = ghcn.fetch(["USC00420072"], "2023-12-03", "2023-12-05")
    finally:
        ghcn.requests.get = orig
    p = df[df.element == "PRCP"].set_index("time_utc")
    # the 12/3 value covers 6 pm MST 12/2 -> 6 pm MST 12/3, i.e. it ends 01 UTC 12/4
    assert list(p.index) == [pd.Timestamp("2023-12-04 01:00"), pd.Timestamp("2023-12-05 01:00")]
    assert p.value.iloc[0] == 2.02 and p.trace.iloc[1] and 9.99 not in set(df.value)


def test_ghcn_fetch_handles_header_only_reply():
    from cottonwoods.obs import ghcn

    class R:
        text = ghcn_csv([])            # NOAA hasn't posted these days yet
        def raise_for_status(self): pass

    orig = ghcn.requests.get
    ghcn.requests.get = lambda *a, **k: R()
    try:
        assert ghcn.fetch(["USC00420072"], "2026-10-24", "2026-10-28").empty
    finally:
        ghcn.requests.get = orig


def test_unreliable_noaa_water_is_dropped_but_snowfall_kept():
    cfg = make_cfg()
    cutoff = pd.Timestamp(cfg["ghcn"]["alta_noaa"]["water_reliable_from"])
    obs = pd.DataFrame({"site": "alta_noaa", "station": "USC00420072",
                        "element": ["PRCP", "SNOW", "PRCP", "SNOW"],
                        "time_utc": [cutoff - pd.Timedelta(days=1)] * 2 + [cutoff + pd.Timedelta(days=1)] * 2,
                        "value": [0.5, 9.0, 0.6, 7.0]})
    kept = oa.drop_unreliable_water(obs, cfg)
    assert list(kept.value) == [9.0, 0.6, 7.0]   # only the early PRCP row is gone


def test_sounding_summary_values():
    from cottonwoods.obs import sounding
    s = sounding.summarize(fake_sounding("2023-12-08 12:00")).iloc[0]
    assert abs(s.t700_c - -10.0) < 1e-9 and s.wdir700 == 270 and s.wspd700_kt == 30
    assert abs(s.tmax_layer_c - (-10.0 + 6.5 * 0.54)) < 1e-6           # warmest at 750 mb (2450 m)
    assert abs(s.freezing_level_m - (2990 - 10 / 6.5 * 1000)) < 1       # 0 C about 1.5 km below 700 mb
    assert abs(s.dgz_depth_m - 6 / 6.5 * 1000) < 1                      # -12 to -18 C spans 6 C of cooling
    assert 80 < s.rh700 < 90                                            # 2 C dewpoint depression


def test_sounding_archive_replaces_refetched_levels():
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        oa.archive_soundings(cfg, "2023-12-08", "2023-12-09", root=root,
                             fetcher=lambda *a, **k: fake_sounding("2023-12-08 12:00"))
        oa.archive_soundings(cfg, "2023-12-08", "2023-12-09", root=root,
                             fetcher=lambda *a, **k: fake_sounding("2023-12-08 12:00", t700=-12.0))
        df = oa.load_soundings(root=root)
    assert len(df) == 8 and df[df.pressure_hpa == 700].temp_c.iloc[0] == -12.0


def test_scheduled_obs_fetch_reaches_back_to_the_newest_stored_hour_of_every_site():
    from datetime import date
    cfg = make_cfg()
    today = date(2026, 11, 20)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert oa.catchup_begin(cfg, today, 3, root=root) == date(2026, 10, 21)    # nothing stored: the 30-day cap
        for i, (site, v) in enumerate(cfg["snotel"].items()):
            start = "2026-11-12 00:00" if site == "atwater" else "2026-11-19 00:00"   # atwater missed a week
            f = snotel_frame(v["triplet"], start, 5).assign(element="PREC")
            oa.archive_snotel(cfg, "2026-11-01", "2026-11-20", root=root, fetcher=lambda *a, f=f, **k: f)
        assert oa.catchup_begin(cfg, today, 3, root=root) == date(2026, 11, 12)
        for site, v in cfg["snotel"].items():                  # every site current: just the trailing days
            f = snotel_frame(v["triplet"], "2026-11-19 12:00", 3).assign(element="PREC")
            oa.archive_snotel(cfg, "2026-11-19", "2026-11-20", root=root, fetcher=lambda *a, f=f, **k: f)
        assert oa.catchup_begin(cfg, today, 3, root=root) == date(2026, 11, 17)


def test_scheduled_noaa_coop_fetch_reaches_past_its_posting_delay():
    from datetime import date
    cfg = make_cfg()
    today = date(2026, 10, 29)
    cfg["obs_fetch"]["ghcn_days"] = 35
    assert oa.ghcn_begin(cfg, today, date(2026, 10, 26)) == date(2026, 9, 24)   # ALTA's newest day was 10/22
    assert oa.ghcn_begin(cfg, today, date(2026, 9, 1)) == date(2026, 9, 1)      # a longer SNOTEL catch-up wins


def test_obs_fetch_is_retried_and_the_last_failure_is_raised():
    cfg = make_cfg()
    cfg["obs_fetch"].update(retries=3, retry_wait_s=60)
    calls, waits, said = [], [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("getaddrinfo failed")
        return 42
    assert oa.with_retries(flaky, cfg, "obs", log=said.append, sleep=waits.append) == 42
    assert len(calls) == 3 and waits == [60, 60] and "try 1 of 3 failed (ConnectionError)" in said[0]
    try:
        oa.with_retries(lambda: 1 / 0, cfg, "obs", log=said.append, sleep=waits.append)
        raise AssertionError("should have raised")
    except ZeroDivisionError:
        pass


def test_the_observations_step_shows_each_sites_latest_values():
    # owner 2026-10-30: the step shows tables (newest per source, latest per site), not logs
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        site, v = next(iter(cfg["snotel"].items()))
        prec = snotel_frame(v["triplet"], "2026-11-01 00:00", 30, base=5.0, rate=0.02).assign(element="PREC")
        depth = snotel_frame(v["triplet"], "2026-11-01 00:00", 30, base=10.0, rate=0.0).assign(element="SNWD")
        oa.archive_snotel(cfg, "2026-11-01", "2026-11-03", root=root, fetcher=lambda *a, **k: pd.concat([prec, depth]))
        newest, sites = oa.latest_obs(cfg, pd.Timestamp("2026-11-02 12:00"), root=root)
    assert set(newest["source"]) == {"SNOTEL", "NOAA ALTA", "RAP (observed flow)"}
    r = sites.set_index("site").loc[v["name"]]
    assert abs(r["water_24h_in"] - 24 * 0.02) < 1e-6 and abs(r["water_6h_in"] - 6 * 0.02) < 1e-6
    assert r["depth_in"] == 10.0 and pd.isna(r["pillow_in"])                   # no pillow values: blank, not 0
