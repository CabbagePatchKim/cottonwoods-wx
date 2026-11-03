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


# --- Alta Collins from the owner's Worker (owner 2026-11-01: record, never publish) ------------------------------

def _collins_row(label, water="0.00", interval="0.0", depth="5.0"):
    return {"Date/Time (L)": label, "Temp. (°F)": "34", "Relative Humidity (%)": "90", "1 Hour Precip (in)": water,
            "Snow Depth (in)": depth, "Snowfall 3 hour (in)": "", "Snowfall 6 Hour (in)": "",
            "Snowfall 24 Hour (in)": "", "Snow Interval (in)": interval}


def test_collins_rows_parse_to_utc_across_the_dst_change_and_a_new_year():
    from cottonwoods.obs import collins
    # newest first, like the page; 1:00 am happens twice on 11/1 (MDT, then MST)
    rows = [_collins_row(x) for x in ("Nov 1, 2:00 am", "Nov 1, 1:00 am", "Nov 1, 1:00 am", "Nov 1, 12:00 am",
                                      "Oct 31, 11:00 pm")]
    df = collins.parse({"scraped": "2026-11-01T09:30:00.000Z", "rows": rows})
    assert df["time_utc"].tolist() == [pd.Timestamp(f"2026-11-01 {h:02d}:00") for h in (5, 6, 7, 8, 9)]
    # a December row read on Jan 1 belongs to the year before
    df = collins.parse({"scraped": "2027-01-01T12:00:00Z", "rows": [_collins_row("Jan 1, 4:00 am"),
                                                                     _collins_row("Dec 31, 11:00 pm")]})
    assert df["time_utc"].tolist() == [pd.Timestamp("2027-01-01 06:00"), pd.Timestamp("2027-01-01 11:00")]


def test_collins_fails_loudly_when_a_page_column_disappears():
    from cottonwoods.obs import collins
    row = _collins_row("Oct 31, 4:00 pm")
    del row["Snow Interval (in)"]
    try:
        collins.parse({"scraped": "2026-11-01T00:00:00Z", "rows": [row]})
    except ValueError as e:
        assert "Snow Interval" in str(e)
    else:
        raise AssertionError("a missing column must fail")


def test_collins_new_snow_is_the_interval_boards_rise_and_a_clearing_starts_over():
    from cottonwoods.obs import collins
    t = pd.date_range("2026-10-31 00:00", periods=6, freq="h")
    stored = pd.DataFrame({"time_utc": t, "water_in": [0.1, 0.1, 0.05, 0.0, 0.02, 0.0],
                           "snow_interval_in": [0.0, 1.0, 2.0, 0.0, 0.5, None], "depth_in": 5.0, "temp_f": 30.0,
                           "rh_pct": 90.0, "fetched_at": t[-1]})
    r = collins.readings(stored)
    snow = r["new_snow_in"].tolist()
    assert snow[0] != snow[0]                                    # first hour: no earlier board value -> NaN
    assert snow[1:5] == [1.0, 1.0, 0.0, 0.5]                     # rises; cleared (2 -> 0) adds 0; then 0.5 since
    assert snow[5] != snow[5]                                    # blank board -> NaN, not 0
    assert (r["period_h"] == 1).all() and (r["start_utc"] == r["end_utc"] - pd.Timedelta(hours=1)).all()


def test_collins_worker_hours_are_stored_and_hand_entries_win_value_by_value():
    from cottonwoods.obs import collins
    cfg = make_cfg()
    rows = [_collins_row("Oct 31, 4:00 pm", water="0.05", interval="1.0"),
            _collins_row("Oct 31, 3:00 pm", water="0.00", interval="0.0")]
    with tempfile.TemporaryDirectory() as d:
        root, entries = Path(d) / "archive", Path(d) / "entries"
        entries.mkdir()
        n = oa.archive_collins(cfg, root=root, fetcher=lambda c: {"scraped": "2026-11-01T00:00:00Z", "rows": rows})
        assert n == 2 and len(oa.load_collins(root=root)) == 2
        # the hand entry for 4 pm has water only: its water wins, the Worker's snow stays
        (entries / "collins_obs.csv").write_text("year,month,day,hour,period_h,new_snow_in,water_in,depth_in,temp_f,"
                                                 "rh_pct\n2026,10,31,16,1,,0.07,,,\n")
        r = oa.load_collins_readings(root=root, entries=entries)
        four = r[r["end_utc"] == pd.Timestamp("2026-10-31 22:00")].iloc[0]
        assert abs(four["water_in"] - 0.07) < 1e-9 and abs(four["new_snow_in"] - 1.0) < 1e-9
        assert len(r) == 2
    assert collins.readings(pd.DataFrame()).empty


def test_collins_errors_never_show_the_secret_address():
    import os
    from cottonwoods.obs import collins
    cfg = make_cfg()
    secret = "https://secret-worker.invalid/"
    old = os.environ.get(cfg["collins_source"]["url_env"])
    os.environ[cfg["collins_source"]["url_env"]] = secret
    try:
        collins.fetch(cfg)
    except RuntimeError as e:
        assert "secret-worker" not in str(e) and "unreachable" in str(e)
    else:
        raise AssertionError("an unreachable address must fail")
    finally:
        if old is None:
            os.environ.pop(cfg["collins_source"]["url_env"])
        else:
            os.environ[cfg["collins_source"]["url_env"]] = old
