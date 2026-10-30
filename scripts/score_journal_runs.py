"""Score archived model and guidance runs against the snow journal's actuals (Alta stake). Laptop, on demand.

    python scripts/archive_forecasts.py --models UUHRRR UUGFS --journal --serial   # first: runs around the storms
    python scripts/score_journal_runs.py

Every run issued within verify.score_runs_hours (72 h) before a journal storm and covering its whole window
(journal.storm_window: start date 0000 to the morning after the end date, Mountain) is totalled and bin-scored.
U of U guidance files go back to 2026-01, so the 2026 journal storms can be scored; most models' archives start
10/2026. Writes data/local/journal_run_scores.csv.
"""
import pandas as pd

from cottonwoods import journal
from cottonwoods.archive.forecasts import load_forecasts
from cottonwoods.config import DATA_DIR, load_config

pd.set_option("display.width", 200)
cfg = load_config()
storms = journal.load_storms()
have = storms.dropna(subset=["actual_in"])
first = min(journal.storm_window(st)[0] for _, st in have.iterrows())
fc = load_forecasts(cfg, start=first - pd.Timedelta(hours=cfg["verify"]["score_runs_hours"]),
                    end=max(journal.storm_window(st)[1] for _, st in have.iterrows()))
sc = journal.score_runs(fc, storms, cfg)
if sc.empty:
    raise SystemExit("No archived run covers a journal storm with an actual (backfill first, see above).")
out = DATA_DIR / "local" / "journal_run_scores.csv"
out.parent.mkdir(parents=True, exist_ok=True)
sc.to_csv(out, index=False)
last = sc.sort_values("init").groupby(["storm_id", "model"]).tail(1)
print("Last run before each storm (snow, owner's bin, score):")
print(last[["storm_id", "model", "init", "snow_in", "actual_in", "fc_bin", "obs_bin", "points"]]
      .round({"snow_in": 1}).to_string(index=False))
print(f"\nEvery run within {cfg['verify']['score_runs_hours']} h before each storm: average bin score, average snow "
      "error (+ = too much), runs")
print(sc.groupby(["storm_id", "model"]).agg(score=("points", "mean"), err=("snow_err_in", "mean"), runs=("points", "size"))
      .round(1).to_string())
print(f"\nRows: {out}")
