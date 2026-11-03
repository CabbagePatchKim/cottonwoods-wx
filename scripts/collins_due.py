"""Session-start reminders, run by the Claude Code SessionStart hook (.claude/settings.local.json); also fine by hand:
    python scripts/collins_due.py

Always: the agreed plan (docs/PLAN.md, owner 2026-10-30), so each session opens by recalling it.
Then: which verified storms still need Alta Collins entries.

A storm "needs" entries when it verified (SNOTEL dry for storms.gap_hours) within storms.report_verified_days and
nothing in data/entries/ (storm_totals.csv, collins_obs.npy/.csv) matches it. The newest storm list is taken from
the cloud bucket when it can be reached (after the cutover the cloud passes verify storms, not the laptop), else
the local data/archive/storms.csv. Prints hook JSON when something is due, nothing otherwise; never fails the hook.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(Path.home() / ".cwx_pylib")]   # this copy's code; boto3 for the bucket

import pandas as pd  # noqa: E402

from cottonwoods import storms, timefmt  # noqa: E402
from cottonwoods.archive.obs import collins_dir, load_collins_readings, load_storm_totals  # noqa: E402
from cottonwoods.config import load_config  # noqa: E402


def newest_state() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """The storm table and every Collins reading (the Worker's hours + hand entries), with the storm table and the
    Worker's last two months taken from the bucket when it can be reached (quietly, 15 s at most), else local."""
    env_file = Path.home() / ".cwx_r2.env"
    try:
        from cottonwoods import bucket
        bucket.load_env_file(env_file)
        if bucket.check_env():
            raise RuntimeError("bucket settings incomplete")
        from botocore.config import Config
        s3 = bucket.client(Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))
        name = os.environ["CWX_BUCKET"].strip()
        with tempfile.TemporaryDirectory() as d:
            s3.download_file(name, "data/archive/storms.csv", str(storms.storms_path(Path(d))))
            store = collins_dir(Path(d))
            store.mkdir(parents=True)
            today = pd.Timestamp.now(tz="UTC")
            for ym in {f"{today:%Y-%m}", f"{today - pd.Timedelta(days=31):%Y-%m}"}:
                try:
                    s3.download_file(name, f"data/archive/obs/collins/ym={ym}.parquet", str(store / f"ym={ym}.parquet"))
                except Exception:  # noqa: BLE001 - that month has no Worker file yet
                    pass
            return storms.load_storms(Path(d)), load_collins_readings(root=Path(d)), "cloud"
    except Exception:  # noqa: BLE001 - offline, no credentials, no boto3: the local copy will do
        return storms.load_storms(), load_collins_readings(), "laptop"


def due(table: pd.DataFrame, cfg: dict, now: pd.Timestamp, totals: pd.DataFrame, readings: pd.DataFrame) -> pd.DataFrame:
    """Verified storms within report_verified_days with no hand-entered Collins totals or readings."""
    days = cfg["storms"]["report_verified_days"]
    v = table[(table["status"] == "verified") & (pd.to_datetime(table["final_end"]) >= now - pd.Timedelta(days=days))]
    missing = [i for i, st in v.iterrows()
               if not storms.manual_totals(st, totals, cfg) and not storms.hand_obs_totals(st, readings, cfg)]
    return v.loc[missing].sort_values("final_start")


def collins_progress(table: pd.DataFrame, cfg: dict, now: pd.Timestamp, readings: pd.DataFrame) -> str:
    """While a storm is under way or just ended (its window reaches into the last storms.collins_page_hours, the
    NWS Collins page's reach): where the owner's Collins readings stop, so nothing drops off the page unentered
    (owner 2026-11-01: the page only goes back ~2 days). "" when no storm is that recent."""
    hours = cfg["storms"]["collins_page_hours"]
    edge = now - pd.Timedelta(hours=hours)
    live = []                                            # (storm_id, start, end) of storms begun and still on the page
    for _, st in table[~table["status"].isin(["dropped", "merged"])].iterrows():
        s, e = storms.storm_span(st)
        if pd.notna(s) and s <= now and e >= edge:
            live.append((st.storm_id, s, e))
    if not live:
        return ""
    names = ", ".join(f"{sid} ({timefmt.clock(s, weekday=True)} to {timefmt.clock(e, weekday=True)})"
                      for sid, s, e in live)
    last = readings["end_utc"].max() if readings is not None and not readings.empty else pd.NaT
    where = (f"recorded through {timefmt.clock(last, weekday=True)}" if pd.notna(last) else "none recorded yet")
    lost = (f" Readings between {timefmt.clock(last, weekday=True)} and {timefmt.clock(edge, weekday=True)} may "
            "already be off the page." if pd.isna(last) or last < edge else "")
    return (f"COLLINS READINGS: storm(s) {names} reach into the last {hours} h, all the NWS Collins page shows. "
            f"Collins readings (Worker + hand entries) {where}; readings before {timefmt.clock(edge, weekday=True)} drop off the page.{lost} "
            "At the start of your first reply, tell the owner where they left off and ask for the readings since "
            "(hourly water, Snow Interval and Snowfall 24 hr boards with their times); offer to type them into "
            "data/entries/collins_obs.csv and send them (scripts/send_entries.bat, outside pass times).")


def plan_context() -> str:
    """The agreed plan (docs/PLAN.md), for the session's opening reply."""
    plan = ROOT / "docs" / "PLAN.md"
    if not plan.exists():
        return ""
    return ("AGREED PLAN (docs/PLAN.md, from the cottonwoods-wx SessionStart hook). At the start of your first reply, "
            "remind the owner in two or three lines which step is next and that the plan is to measure before "
            "building more; tick steps off in docs/PLAN.md as they finish.\n\n" + plan.read_text(encoding="utf-8"))


def main():
    cfg = load_config()
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    plan = plan_context()
    table, readings, source = newest_state()
    need = due(table, cfg, now, load_storm_totals(), readings)
    progress = collins_progress(table, cfg, now, readings)
    parts, notes = [], []
    if progress:
        parts.append(progress)
        notes.append("Collins readings: see where you left off")
    if not need.empty:
        lines = [f"- storm {st.storm_id}: {timefmt.clock(st.final_start, weekday=True)} to "
                 f"{timefmt.clock(st.final_end, weekday=True)}" for _, st in need.iterrows()]
        parts.append("COLLINS ENTRIES DUE (from the cottonwoods-wx SessionStart hook; storm list from the "
                     f"{source}). These storms are over (SNOTEL dry {cfg['storms']['gap_hours']} h) and have no Alta "
                     "Collins observations entered yet:\n" + "\n".join(lines) + "\nAt the start of your first reply, "
                     "before anything else, prompt the owner to enter Collins readings for them (12 h boards and/or "
                     "hourly from https://www.weather.gov/wrh/timeseries?site=CLN, read by eye; format in "
                     "data/entries/README.txt; offer to type them into data/entries/collins_obs.csv for the owner, "
                     "then run scripts/send_entries.bat).")
        notes.append(f"Collins entries due for {len(need)} verified storm(s)")
    if plan:
        parts.append(plan)
        notes.append("plan in docs/PLAN.md")
    if parts:
        print(json.dumps({"systemMessage": "; ".join(notes),
                          "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "\n\n".join(parts)}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - a broken check must never block opening a session
        print(json.dumps({"systemMessage": f"Collins reminder check failed: {e}"}))
