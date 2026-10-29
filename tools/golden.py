"""Golden snapshot for behavior-preserving refactors (part of the once-per-session maintenance review).

    python tools/golden.py save      # before refactoring: snapshot current outputs
    python tools/golden.py check     # after each step: IDENTICAL, or the differences

Snapshots go to data/processed/golden/ (ignored by git with the rest of data/).

Covers: every archived run re-derived from its raw pulled columns (all derived columns: profile, phase, Kuchera,
regimes, modes), the storm report text and the model report text, at a fixed 'now'. The storm table is copied
into the snapshot at save time, so a scheduled pass updating data/archive/storms.csv can't cause a false DIFF.
"""
import shutil
import sys

import pandas as pd

from cottonwoods.archive.forecasts import forecast_dir
from cottonwoods.archive.io import read_df
from cottonwoods.storms import storms_path
from cottonwoods.config import PROCESSED_DIR, load_config

OUT = PROCESSED_DIR / "golden"
NOW = pd.Timestamp("2026-10-27 05:00")
from cottonwoods.archive.forecasts import DERIVED  # noqa: E402


def derive(cfg):
    import cottonwoods.archive.forecasts as fa
    fn = getattr(fa, "derive_run_fields", None) or fa.add_kuchera
    out = {}
    for p in sorted(forecast_dir().glob("model=*/init=*.parquet")):
        df = read_df(p)
        raw = df.drop(columns=[c for c in DERIVED if c in df])
        out[f"{p.parent.name}_{p.stem}"] = fn(raw, cfg)
    return out


def reports(cfg):
    from cottonwoods import storms
    from cottonwoods.archive.forecasts import load_forecasts
    from cottonwoods.report import render
    fc = load_forecasts(cfg, start=NOW - pd.Timedelta(days=20))
    st = storms.load_storms(OUT)        # the snapshot's own copy (see save below)
    st = storms.update_forecast_storms(st, fc, cfg, NOW)
    render_storms = getattr(storms, "render_open", None)
    if render_storms is None:
        from cottonwoods.report import render_storms
    return {"storms": render_storms(st, fc, cfg, NOW),
            "models": render(fc[fc.init >= NOW - pd.Timedelta(days=2)], cfg, list(cfg["points"]), 10, start=NOW)}


cfg = load_config()
if sys.argv[1] == "save":
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(storms_path(), OUT / "storms.csv")
runs, texts = derive(cfg), reports(cfg)
if sys.argv[1] == "save":
    for k, v in runs.items():
        v.to_pickle(OUT / f"{k}.pkl")
    for k, v in texts.items():
        (OUT / f"{k}.txt").write_text(v, encoding="utf-8")
    print(f"saved {len(runs)} runs + {len(texts)} reports")
else:
    bad = 0
    for k, v in runs.items():
        if not (OUT / f"{k}.pkl").exists():
            continue  # a run archived after the snapshot
        g = pd.read_pickle(OUT / f"{k}.pkl")
        try:
            pd.testing.assert_frame_equal(v[sorted(v.columns)].reset_index(drop=True),
                                          g[sorted(g.columns)].reset_index(drop=True), check_dtype=False)
        except AssertionError as e:
            bad += 1
            print(f"DIFF run {k}: {str(e)[:300]}")
    for k, v in texts.items():
        if v != (OUT / f"{k}.txt").read_text(encoding="utf-8"):
            bad += 1
            print(f"DIFF report {k}")
    print("IDENTICAL" if not bad else f"{bad} differences")
