"""Pull hourly SNOTEL data for every storm window in data/journal/storms.csv.

Writes data/processed/snotel_hourly.parquet and prints a SWE increase per storm vs the journal actual.
Pads each window by 1 day on each side.
"""
import pandas as pd

# These imports come from this project's own package in src/cottonwoods/. They work because the project
# was installed with `pip install -e .` ("editable"), so Python finds src/cottonwoods like any library.
from cottonwoods.config import PROCESSED_DIR, ensure_dirs, load_config
from cottonwoods.journal import load_storms
from cottonwoods.obs import snotel

ensure_dirs()       # create data/ folders if they don't exist yet
cfg = load_config()  # config/stations.yaml read into a nested Python dict

# Dict comprehension: builds {"snowbird": "766:UT:SNTL", ...}, skipping any station with no triplet.
sites = {k: v["triplet"] for k, v in cfg["snotel"].items() if v["triplet"]}
if not sites:
    raise SystemExit("No SNOTEL triplets set. Run scripts/01_find_stations.py and edit config/stations.yaml.")

storms = load_storms()  # one row per storm from data/journal/storms.csv
frames = []             # collect one DataFrame per storm, then join them once at the end (faster than appending)
# iterrows() yields (index, row) pairs; "_" is the convention for a value we don't need.
for _, s in storms.iterrows():
    begin = (s.start_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    end = (s.end_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = snotel.fetch(list(sites.values()), begin, end)
    if df.empty:
        print(f"[warn] no data for {s.storm_id} {begin}..{end}")
        continue
    df["storm_id"] = s.storm_id  # tag rows so we can split them back out per storm below
    frames.append(df)

if frames:
    out = pd.concat(frames, ignore_index=True)  # stack all storms into one table
    out.to_parquet(PROCESSED_DIR / "snotel_hourly.parquet")
    print(f"wrote {len(out):,} rows")
    for _, s in storms.iterrows():
        sub = out[out.storm_id == s.storm_id]
        # Quick sanity check: SWE gained at each station vs. what the journal says fell. Roughly, 1" of SWE is
        # 10-15" of snow, so the two numbers should be in proportion, not equal.
        incs = {name: round(snotel.swe_increase(sub, trip), 2) for name, trip in sites.items()}
        print(f"{s.storm_id}: SWE increase (in) {incs}   journal actual snow: {s.actual_in}")
