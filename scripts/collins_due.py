"""Which verified storms still need Alta Collins entries? Run by the Claude Code SessionStart hook
(.claude/settings.local.json), so each session opens with a reminder; also fine by hand:
    python scripts/collins_due.py

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
from cottonwoods.archive.obs import load_hand_obs, load_storm_totals  # noqa: E402
from cottonwoods.config import load_config  # noqa: E402


def newest_storms() -> tuple[pd.DataFrame, str]:
    """The storm table from the bucket if reachable (quietly, 15 s at most), else the local one."""
    env_file = Path.home() / ".cwx_r2.env"
    try:
        from cottonwoods import bucket
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        if bucket.check_env():
            raise RuntimeError("bucket settings incomplete")
        from botocore.config import Config
        s3 = bucket.client()
        s3.meta.config = s3.meta.config.merge(Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))
        with tempfile.TemporaryDirectory() as d:
            s3.download_file(os.environ["CWX_BUCKET"].strip(), "data/archive/storms.csv", str(Path(d) / "storms"))
            return storms.load_storms(Path(d)), "cloud"
    except Exception:  # noqa: BLE001 - offline, no credentials, no boto3: the local copy will do
        return storms.load_storms(), "laptop"


def due(table: pd.DataFrame, cfg: dict, now: pd.Timestamp, totals: pd.DataFrame, readings: pd.DataFrame) -> pd.DataFrame:
    """Verified storms within report_verified_days with no hand-entered Collins totals or readings."""
    days = cfg["storms"]["report_verified_days"]
    v = table[(table["status"] == "verified") & (pd.to_datetime(table["final_end"]) >= now - pd.Timedelta(days=days))]
    missing = [i for i, st in v.iterrows()
               if not storms.manual_totals(st, totals, cfg) and not storms.hand_obs_totals(st, readings, cfg)]
    return v.loc[missing].sort_values("final_start")


def main():
    cfg = load_config()
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    table, source = newest_storms()
    need = due(table, cfg, now, load_storm_totals(), load_hand_obs())
    if need.empty:
        return
    lines = [f"- storm {st.storm_id}: {timefmt.clock(st.final_start, weekday=True)} to "
             f"{timefmt.clock(st.final_end, weekday=True)}" for _, st in need.iterrows()]
    context = ("COLLINS ENTRIES DUE (from the cottonwoods-wx SessionStart hook; storm list from the "
               f"{source}). These storms are over (SNOTEL dry {cfg['storms']['gap_hours']} h) and have no Alta Collins "
               "observations entered yet:\n" + "\n".join(lines) + "\nAt the start of your first reply, before anything "
               "else, prompt the owner to enter Collins readings for them (12 h boards and/or hourly from "
               "https://www.weather.gov/wrh/timeseries?site=CLN, read by eye; format in data/entries/README.txt; "
               "offer to type them into data/entries/collins_obs.npy for the owner, then run scripts/send_entries.bat).")
    print(json.dumps({"systemMessage": f"Collins entries due for {len(need)} verified storm(s)",
                      "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - a broken check must never block opening a session
        print(json.dumps({"systemMessage": f"Collins reminder check failed: {e}"}))
