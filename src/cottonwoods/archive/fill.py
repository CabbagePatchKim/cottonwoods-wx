"""Hole-filling: put back values a run lost to a download timeout, from that same run's own files (owner, 2026-10-28).

A run is archived once 90% of it came back (forecasts.MIN_COMPLETE), so a timed-out file leaves a hole: one field
missing at one step (RDPS 18z 10/28: 700 mb temperature at 6 steps), or a whole step (its precip then merges into
the next step's). Nothing went back for them, so two machines archiving the same run could disagree.

A later pass re-pulls the WHOLE run (precip is a running total or a chain of buckets, so pulling only the missing
hours could give a wrong step amount) and:
  1. checks every value the archive already has against the new pull. They come from the same files, so they
     must be identical; one difference (a different grid, a re-posted run) and nothing is changed;
  2. fills only what is missing: empty values, and missing steps (a merged step is split back only if the new
     steps add up to the archived amount);
  3. re-derives the run (forecasts.derive_run_fields) and records filled_at / filled_values; fetched_at stays.
Values are never estimated or interpolated: a hole is filled with the model's own number or stays empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..models.registry import REGISTRY, get_puller
from .forecasts import DERIVED, _append_manifest, derive_run_fields, run_path, run_points
from .io import read_df, write_df

STEP = ["point", "member", "valid_start", "valid_end"]     # one row of a run
META = {"model", "init", "lead_hr", "valid_start", "valid_end", "point", "member", "source", "fetched_at",
        "filled_at", "filled_values", "cell_blend", "grid_dist_km"}
MOSTLY = 0.5        # a column present in at least this share of rows is expected everywhere (Canada's CAPE is
                    # empty over most of the map by design, so it's never counted as a hole)
NO_MEMBER = -1      # stands in for a deterministic run's empty member while joining


class FillMismatch(Exception):
    """The new pull disagrees with what the archive already holds, so it isn't the same data: nothing is filled."""


def raw_columns(df: pd.DataFrame) -> list[str]:
    """The pulled weather numbers of a run (not keys, bookkeeping or derived fields)."""
    return [c for c in df.select_dtypes("number").columns if c not in META and c not in DERIVED]


def count_holes(df: pd.DataFrame, model: str, init, cfg: dict) -> int:
    """Missing values (in columns present in most rows) plus missing steps (lead hours a full run has, per point
    and member)."""
    cols = [c for c in raw_columns(df) if df[c].notna().mean() >= MOSTLY]
    missing = int(df[cols].isna().sum().sum())
    fxx = set(REGISTRY[model].fxx_list(pd.Timestamp(init).to_pydatetime(), cfg))
    by = ["point"] + (["member"] if "member" in df and df["member"].notna().any() else [])
    steps = int(df.groupby(by)["lead_hr"].agg(lambda s: len(fxx - set(s))).sum())
    return missing + steps


def _keyed(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["member"] = out["member"].fillna(NO_MEMBER) if "member" in out else NO_MEMBER
    return out.set_index(STEP)


def merge_holes(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """The archived run with its holes filled from a fresh pull of the same run (raw columns only; the caller
    re-derives). Raises FillMismatch if any value both have differs."""
    blended = old["cell_blend"].fillna(False).astype(bool) if "cell_blend" in old else pd.Series(False, old.index)
    keep = [c for c in old.columns if c not in DERIVED and c not in ("cell_blend", "filled_at", "filled_values")]
    o = _keyed(old[keep])
    n = _keyed(new[new["point"].isin(old["point"].unique())])
    n = n[[c for c in o.columns if c in n.columns]]
    n = n[~n.index.duplicated()]

    # 1. Same files -> same numbers. Blended resort rows are skipped: the archive holds their blend of the four
    # cells, a new pull their nearest cell (the cells themselves are compared).
    check = o[~blended.to_numpy()]
    both = check.index.intersection(n.index)
    for c in [c for c in raw_columns(o) + ["grid_dist_km"] if c in n.columns]:
        a, b = check.loc[both, c].astype(float), n.loc[both, c].astype(float)
        bad = a.notna() & b.notna() & ~np.isclose(a, b, rtol=1e-6, atol=1e-6)
        if bad.any():
            raise FillMismatch(f"{c} differs at {int(bad.sum())} rows (e.g. {a[bad].iloc[0]:g} vs {b[bad].iloc[0]:g})")

    # 2a. Empty values in rows both have
    filled = o.fillna(n.reindex(o.index))

    # 2b. Missing steps. An archived step the new pull splits into finer steps (its file once timed out) is replaced
    # by them if their precip adds up to it; a new step overlapping an archived one is never added.
    old_only = o.index.difference(n.index)
    drop = []
    for key in old_only:
        p, m, s, e = key
        try:
            fine = n.xs((p, m), level=("point", "member"), drop_level=False)
        except KeyError:
            continue                                     # the new pull lacks this point / member altogether
        fine = fine[(fine.index.get_level_values("valid_start") >= s) & (fine.index.get_level_values("valid_end") <= e)]
        starts = sorted(fine.index.get_level_values("valid_start"))
        ends = sorted(fine.index.get_level_values("valid_end"))
        if len(fine) < 2 or starts[0] != s or ends[-1] != e or starts[1:] != ends[:-1]:
            continue                                     # the new pull doesn't tile this step: keep it as archived
        was = o.loc[key, "qpf_mm"]
        if pd.notna(was) and not np.isclose(fine["qpf_mm"].sum(), was, rtol=1e-5, atol=1e-4):
            raise FillMismatch(f"{p} {s:%m-%d %H}-{e:%H}z: new steps add to {fine['qpf_mm'].sum():g} mm, archive {was:g}")
        drop.append(key)
    filled = filled.drop(drop)
    add = n.loc[n.index.difference(o.index)]
    span = filled.reset_index()[["point", "member", "valid_start", "valid_end"]]
    overlaps = [((span["point"] == p) & (span["member"] == m) & (span["valid_start"] < e) & (span["valid_end"] > s)).any()
                for p, m, s, e in add.index]
    add = add[~np.array(overlaps, dtype=bool)] if len(add) else add
    out = pd.concat([filled, add]).reset_index()
    if "member" not in old:
        out = out.drop(columns="member")
    else:
        out["member"] = out["member"].where(out["member"] != NO_MEMBER)
    return out.sort_values([c for c in ("point", "member", "valid_start") if c in out]).reset_index(drop=True)


def fill_run(model: str, init: datetime, cfg: dict, root: Path | None = None, save_dir: str | None = None,
             puller=None, now: datetime | None = None) -> tuple[str, int, int]:
    """Re-pull an archived run and fill its holes. Returns (status, values filled, holes left), status one of
    'complete' (no holes), 'filled', 'unchanged' (the new pull had nothing more). Raises FillMismatch or the
    puller's error."""
    path = run_path(model, init, cfg, root)
    old = read_df(path)
    before = count_holes(old, model, init, cfg)
    if not before:
        return "complete", 0, 0
    pull = puller or get_puller(model, cfg)
    new = pull(init, REGISTRY[model].fxx_list(init, cfg), run_points(model, cfg), save_dir)
    if new is None or new.empty:
        raise RuntimeError(f"{model} {init:%Y-%m-%d %H}z: nothing retrieved for the fill")
    merged = merge_holes(old, new)
    after = count_holes(merged, model, init, cfg)
    if after >= before:
        return "unchanged", 0, before
    out = derive_run_fields(merged, cfg)
    out["fetched_at"] = old["fetched_at"].iloc[0] if "fetched_at" in old else pd.NaT
    out["filled_at"] = pd.Timestamp(now or datetime.now(timezone.utc).replace(tzinfo=None))
    out["filled_values"] = int(old["filled_values"].iloc[0] if "filled_values" in old else 0) + before - after
    write_df(out, path)
    _append_manifest({"model": model, "init": f"{pd.Timestamp(init):%Y-%m-%d %H:%M}", "n_expected": None,
                      "n_valid": int(out["qpf_mm"].notna().sum()),
                      "fetched_at": f"{out['filled_at'].iloc[0]:%Y-%m-%d %H:%M}",
                      "path": str(path), "note": f"filled {before - after} holes, {after} left"}, root)
    return "filled", before - after, after
