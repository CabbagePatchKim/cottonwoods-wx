"""Score the owner's journal forecasts against recorded actuals (no network needed)."""
import pandas as pd

from cottonwoods.journal import score_journal

pd.set_option("display.width", 160)    # let pandas print wide tables on one line instead of wrapping
df = score_journal()                    # one row per journal forecast, with its error vs. the actual
have = df.dropna(subset=["actual_in"])  # skip storms whose actual hasn't been entered yet
print(have[["storm_id", "model", "low_in", "high_in", "actual_in", "error_in", "hit", "notes"]]
      .to_string(index=False))
print("\nMean error by model (midpoint minus actual, inches; excludes open-ended forecasts):")
# "~" means NOT for a True/False column: keep forecasts that are not open-ended. A "60+" forecast has
# no midpoint, so it can't be averaged.
clean = have[~have.open_ended]
print(clean.groupby("model")["error_in"].agg(["count", "mean"]).round(1).sort_values("mean"))
