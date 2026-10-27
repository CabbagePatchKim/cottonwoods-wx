"""Add and update snow-journal entries (storms, model forecasts, observed totals).

Everything is stored in the same CSVs the scoring code already reads:
    data/journal/storms.csv
    data/journal/journal_forecasts.csv

Re-running is safe: entering a storm or forecast that already exists UPDATES it instead of duplicating it,
and every save first copies the old file to <name>.csv.bak. Close the CSVs in Excel before saving (Windows
locks open files).

Interactive (works in a terminal or a Jupyter cell):
    from cottonwoods.journal_entry import run_interactive
    run_interactive()

Non-interactive:
    from cottonwoods.journal_entry import add_storm, add_forecast, set_actual
    add_storm(start_date="2026-11-23", end_date="2026-11-25", entry_date="2026-11-22")
    add_forecast("2026-11-23", "GFS", "12z", "3-6")           # init defaults to the storm's entry date
    add_forecast("2026-11-23", "NAMNST", "11/22 18z", "60+")  # open-ended
    set_actual("2026-11-23", 10, site="alta")
"""
from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

import pandas as pd

from .config import JOURNAL_DIR

# Column order for each CSV. Kept in one place so reading and writing always agree.
STORM_COLS = ["storm_id", "entry_date", "start_date", "end_date", "actual_in", "actual_site",
              "flow_notes", "window_confirmed", "notes"]
FC_COLS = ["storm_id", "model", "init_utc", "low_in", "high_in", "open_ended", "notes"]

# Nicknames people type -> the one name stored, so "Euro" and "ECMWF" are scored as the same model.
MODEL_ALIASES = {"NAMNEST": "NAMNST", "NAM3K": "NAMNST", "NAM3KM": "NAMNST", "EURO": "ECMWF", "IFS": "ECMWF",
                 "CMC": "GDPS", "GEM": "GDPS"}


# --- parsing ---------------------------------------------------------------------------------

def normalize_model(name: str) -> str:
    # Uppercase and remove spaces, underscores and dashes: "nam nest", "NAM-Nest" -> "NAMNEST".
    key = re.sub(r"[\s_\-]", "", name.strip().upper())
    if not key:
        raise ValueError("Model name is empty.")
    return MODEL_ALIASES.get(key, key)  # .get(key, default): the alias if there is one, else the name as typed


def parse_range(text: str) -> tuple[float, float, bool]:
    """'8-12' -> (8, 12, False); '60+' -> (60, nan, True); '~24' or '24' -> (24, 24, False)."""
    t = str(text).strip().lower().replace('"', "").replace("in", "").replace("~", "").replace("inches", "").strip()
    if not t:
        raise ValueError("Empty forecast. Use e.g. 8-12, 60+, or 24.")
    if t.endswith("+"):
        return float(t[:-1]), math.nan, True  # t[:-1] = everything except the last character (the "+")
    # Matches "8-12", "8 to 12", "7.5-10". (\d+(?:\.\d+)?) = a number with an optional decimal part;
    # fullmatch means the WHOLE text must match, so "8-12ish" is rejected rather than half-read.
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", t)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (min(lo, hi), max(lo, hi), False)  # min/max so "12-8" is stored as 8-12
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        x = float(t)
        return x, x, False
    raise ValueError(f"Can't read {text!r}. Use e.g. 8-12, 60+, or 24.")


def parse_init(text, default_date) -> pd.Timestamp:
    """Model init time in UTC. Accepts '12z' (uses default_date), '11/30 00z', or '2024-11-30 00z'/ISO."""
    if isinstance(text, pd.Timestamp):
        return text.tz_localize(None) if text.tzinfo else text
    default = pd.Timestamp(default_date).normalize()
    t = str(text).strip().lower().replace("utc", "").replace("z", "").strip()
    # Form 1: just an hour, "12" or "12:00" (the "z" was already stripped above)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", t)
    if m:
        return default + pd.Timedelta(hours=int(m.group(1)), minutes=int(m.group(2) or 0))
    # Form 2: month/day and hour, "11/30 00"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2})(?::(\d{2}))?", t)
    if m:
        mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
        ts = pd.Timestamp(year=default.year, month=mo, day=d, hour=h, minute=mi)
        if ts - default > pd.Timedelta(days=200):   # entry in early Jan about a late-Dec init
            ts = ts.replace(year=default.year - 1)
        return ts
    # Form 3: anything else pandas can read, e.g. "2024-11-30 00" or ISO "2024-11-30T00:00"
    return pd.Timestamp(t.replace("t", " "))


# Turn a value into the text stored in the CSV: missing -> "", 10.0 -> "10", 7.5 -> "7.5".
def _fmt(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


# --- file handling ---------------------------------------------------------------------------

def _read(path: Path, cols: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)  # strings in, strings out: nothing gets reformatted
    # Add any missing columns (e.g. an older CSV from before a column existed) so code below can rely on them.
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols]


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists():
            shutil.copy(path, path.with_name(path.name + ".bak"))  # keep the previous version as a backup
        tmp = path.with_name(path.name + ".tmp")
        df.to_csv(tmp, index=False)
        tmp.replace(path)
    except PermissionError as e:
        # "from e" chains the original error, so the full detail is still there if you need to debug.
        raise PermissionError(f"Can't write {path.name}. If it's open in Excel, close it and try again.") from e


def _iso(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%d")


# --- operations ------------------------------------------------------------------------------

def list_storms(journal_dir: Path = JOURNAL_DIR) -> pd.DataFrame:
    return _read(Path(journal_dir) / "storms.csv", STORM_COLS)


def add_storm(storm_id: str | None = None, start_date=None, end_date=None, entry_date=None,
              actual_in=None, actual_site: str | None = None, flow_notes: str | None = None,
              notes: str | None = None, journal_dir: Path = JOURNAL_DIR) -> tuple[str, str]:
    """Create a storm, or update the fields you pass if storm_id already exists. Returns (storm_id, 'added'|'updated')."""
    path = Path(journal_dir) / "storms.csv"
    df = _read(path, STORM_COLS)
    if storm_id is None:
        if start_date is None:
            raise ValueError("Give a storm_id or a start_date.")
        storm_id = _iso(start_date)
    # Compare every row's storm_id to ours (a True/False column); .any() = "is at least one True?"
    exists = (df["storm_id"] == storm_id).any()

    if not exists:
        if start_date is None:
            raise ValueError("A new storm needs a start_date.")
        start = pd.Timestamp(start_date)
        row = {
            "storm_id": storm_id,
            "entry_date": _iso(entry_date or pd.Timestamp.today()),
            "start_date": _iso(start),
            "end_date": _iso(end_date or start + pd.Timedelta(days=2)),
            "actual_in": _fmt(actual_in), "actual_site": actual_site or "", "flow_notes": flow_notes or "",
            "window_confirmed": "true", "notes": notes or "",
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        action = "added"
    else:
        i = df.index[df["storm_id"] == storm_id][0]  # row label of the existing storm
        # `x and f(x)` gives None when x is None, and f(x) otherwise: only convert dates that were passed in.
        updates = {"entry_date": entry_date and _iso(entry_date), "start_date": start_date and _iso(start_date),
                   "end_date": end_date and _iso(end_date), "actual_in": actual_in,
                   "actual_site": actual_site, "flow_notes": flow_notes, "notes": notes}
        for k, v in updates.items():
            if v is not None and v != "":  # only overwrite fields you actually gave
                df.at[i, k] = _fmt(v)       # .at[row, column] sets a single cell
        if start_date is not None or end_date is not None:
            df.at[i, "window_confirmed"] = "true"
        action = "updated"
    _write(df, path)
    return storm_id, action


def set_actual(storm_id: str, actual_in, site: str | None = None, journal_dir: Path = JOURNAL_DIR) -> None:
    """Record or overwrite the observed total for a storm."""
    if storm_id not in set(list_storms(journal_dir)["storm_id"]):
        raise ValueError(f"No storm {storm_id!r}. Add it first with add_storm().")
    add_storm(storm_id=storm_id, actual_in=float(actual_in), actual_site=site, journal_dir=journal_dir)


def add_forecast(storm_id: str, model: str, init, forecast: str, notes: str = "",
                 journal_dir: Path = JOURNAL_DIR) -> str:
    """Add one model forecast to a storm. Same storm + model + init time replaces the earlier entry."""
    storms = list_storms(journal_dir)
    match = storms[storms["storm_id"] == storm_id]
    if match.empty:
        raise ValueError(f"No storm {storm_id!r}. Add it first with add_storm().")
    # A bare "12z" needs a date: use the day the forecasts were made, or the storm start if that's blank.
    default_date = match.iloc[0]["entry_date"] or match.iloc[0]["start_date"]

    model = normalize_model(model)
    init_ts = parse_init(init, default_date)
    lo, hi, open_ended = parse_range(forecast)
    init_str = f"{init_ts:%Y-%m-%dT%H:%MZ}"

    path = Path(journal_dir) / "journal_forecasts.csv"
    df = _read(path, FC_COLS)
    same = (df["storm_id"] == storm_id) & (df["model"] == model) & (df["init_utc"] == init_str)
    action = "replaced" if same.any() else "added"
    df = df[~same]  # drop the old version (if any); the new row is added below
    row = {"storm_id": storm_id, "model": model, "init_utc": init_str, "low_in": _fmt(lo),
           "high_in": _fmt(hi), "open_ended": "true" if open_ended else "false", "notes": notes}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _write(df, path)
    return action


# --- report ----------------------------------------------------------------------------------

def print_report() -> None:
    """Score the journal against recorded actuals (same output as scripts/04_score_journal.py)."""
    from .journal import load_storms, score_journal

    pd.set_option("display.width", 160)
    df = score_journal()
    have = df.dropna(subset=["actual_in"])
    waiting = load_storms()
    waiting = waiting[waiting["actual_in"].isna()]
    if not waiting.empty:
        print("Storms still waiting for an actual:", ", ".join(waiting["storm_id"]), "\n")
    if have.empty:
        print("No storms with actuals yet.")
        return
    print(have[["storm_id", "model", "low_in", "high_in", "actual_in", "error_in", "hit", "notes"]].to_string(index=False))
    print("\nMean error by model (midpoint minus actual, inches; excludes open-ended forecasts):")
    clean = have[~have.open_ended]
    print(clean.groupby("model")["error_in"].agg(["count", "mean"]).round(1).sort_values("mean"))


# --- interactive -----------------------------------------------------------------------------

# input() pauses and waits for the user to type a line. Pressing Enter on a blank line takes the default.
def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or (default or "")


def _pick_storm(journal_dir: Path) -> str | None:
    storms = list_storms(journal_dir)
    if storms.empty:
        print("No storms yet. Add one first.")
        return None
    show = storms[["storm_id", "start_date", "actual_in"]].tail(12).copy()
    show["actual_in"] = show["actual_in"].replace("", "(none yet)")
    print(show.to_string(index=False))
    sid = _ask("Storm id", storms.iloc[-1]["storm_id"])
    if sid not in set(storms["storm_id"]):
        print(f"No storm {sid!r}.")
        return None
    return sid


def _do_new_storm(journal_dir: Path) -> None:
    start = _ask("Storm start date (YYYY-MM-DD)")
    end = _ask("Storm end date (YYYY-MM-DD)", _iso(pd.Timestamp(start) + pd.Timedelta(days=2)))
    entry = _ask("Date you made these forecasts (YYYY-MM-DD)", _iso(pd.Timestamp.today()))
    sid = _ask("Storm id", _iso(start))
    flow = _ask("Flow notes, e.g. 'warm, SW flow' (optional)")
    notes = _ask("Notes (optional)")
    actual = _ask("Actual snowfall in inches, if known (blank = fill in later)")
    site = _ask("Where measured (alta/snowbird/brighton/...)", "alta") if actual else ""
    sid, action = add_storm(sid, start, end, entry, float(actual) if actual else None, site, flow, notes,
                            journal_dir=journal_dir)
    print(f"Storm {sid} {action}.")


def _do_forecasts(journal_dir: Path) -> None:
    sid = _pick_storm(journal_dir)
    if not sid:
        return
    last_init = ""
    print("Enter one model per line. Blank model name = done. Forecast examples: 8-12   60+   24")
    while True:
        model = _ask("Model (GFS, GDPS, RDPS, NAM, NAMNST, HRRR, RAP, ECMWF, ICON...)")
        if not model:
            break
        try:
            init = _ask("Init time UTC ('12z', '11/30 00z', or '2026-11-30 00z')", last_init or None)
            fc = _ask("Forecast")
            notes = _ask("Note (optional)")
            action = add_forecast(sid, model, init, fc, notes, journal_dir=journal_dir)
            last_init = init
            print(f"  {normalize_model(model)} {action}.")
        except ValueError as e:
            print(f"  Not saved: {e}")


def _do_actual(journal_dir: Path) -> None:
    sid = _pick_storm(journal_dir)
    if not sid:
        return
    actual = _ask("Actual snowfall (inches)")
    site = _ask("Where measured (alta/snowbird/brighton/...)", "alta")
    try:
        set_actual(sid, float(actual), site, journal_dir=journal_dir)
        print(f"Saved {actual}\" for {sid}.")
    except ValueError as e:
        print(f"Not saved: {e}")


def run_interactive(journal_dir: Path = JOURNAL_DIR) -> None:
    """Menu loop. Safe to run as often as you like; it only adds or updates entries."""
    # Menu choice -> the function to run. Functions are values in Python, so they can sit in a dict.
    actions = {"1": _do_new_storm, "2": _do_forecasts, "3": _do_actual}
    while True:
        print("\n1) New storm (or update one)   2) Add model forecasts   3) Record/overwrite an actual\n"
              "4) Show scoring report         q) Quit")
        choice = _ask("Choose").lower()
        if choice in ("q", "quit", "exit", ""):
            print("Saved. Re-run any time to add more.")
            return
        if choice == "4":
            print_report()
        elif choice in actions:
            try:
                actions[choice](Path(journal_dir))  # look up the function, then call it
            except (ValueError, PermissionError) as e:
                print(f"Not saved: {e}")
        else:
            print("Pick 1, 2, 3, 4 or q.")
