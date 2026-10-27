"""How an archive pass runs: which runs to fetch, in what order, what to skip, and how the work is split.

scripts/archive_forecasts.py is only the command line around this module, so each piece can be tested:
    PassLock          one pass at a time (a pass can take an hour; the scheduler starts one every 3 hours)
    build_jobs        the to-do list of (model, init) for a pass: newest first, with long catch-up for Canada
    FailureLog        runs that keep failing are skipped by scheduled passes after MAX_TRIES (failures*.csv)
    run_jobs          archive each job, counting archived / already present / failed / given up
    split_streams     group models by server (registry.STREAMS) so each group can run in its own process
    run_streams       launch one child process per group and relay their output, prefixed
    clear_caches      delete the GRIB download caches once the point values are saved
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from ..models.registry import STREAMS, cycles, cycles_between, recent_cycles

MAX_TRIES = 4             # failed attempts before a scheduled pass gives up on a run older than a day
LOCK_STALE_HOURS = 6      # an older lock is left over from a crash and is ignored


class PassLock:
    """Stops two passes overlapping. `with PassLock(path) as got:` -> got is False if another pass holds it."""

    def __init__(self, path: Path, stale_hours: float = LOCK_STALE_HOURS):
        self.path, self.stale_s, self.held = Path(path), stale_hours * 3600, False

    def __enter__(self) -> bool:
        if self.path.exists() and time.time() - self.path.stat().st_mtime < self.stale_s:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        self.held = True
        return True

    def __exit__(self, *_):
        if self.held:
            self.path.unlink(missing_ok=True)   # always release, even after an error
        return False

    def started(self) -> str:
        return time.ctime(self.path.stat().st_mtime) if self.path.exists() else "?"


def clear_caches(raw_dir: Path, log=print) -> int:
    """Herbie leaves an index (.idx) file per forecast hour it touches, ~2,000 per GEFS run. The point values are
    already in the archive, so every download cache (data/raw/herbie*, one per stream) can go. Returns bytes freed."""
    freed = 0
    for cache in Path(raw_dir).glob("herbie*"):
        freed += sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
        shutil.rmtree(cache, ignore_errors=True)
    log(f"[cache]   cleared {freed / 1e6:.0f} MB from {raw_dir}")
    return freed


def build_jobs(models: list[str], cfg: dict, now: datetime, init=None, start=None, end=None,
               journal_storms: pd.DataFrame | None = None, recent: int | None = None) -> list[tuple[str, datetime]]:
    """The to-do list of (model, init time). Exactly one way of choosing, in this order of precedence:
      init            that one cycle for every model
      journal_storms  every cycle from 3 days before to 1 day after each journal storm
      start/end       every cycle in the range (end defaults to now)
      (default)       the newest cycles that should be posted by now: archive.recent_cycles per model (2 days),
                      or archive.catchup_days for models whose source deletes runs (RDPS/GDPS, ~30 days); sorted
                      NEWEST FIRST across models, so every model's latest run is saved before any long catch-up
    """
    if init is not None:
        t = pd.Timestamp(init).to_pydatetime()
        return [(m, t) for m in models]
    if journal_storms is not None:
        windows = [(s.start_date - timedelta(days=3), s.end_date + timedelta(days=1)) for _, s in journal_storms.iterrows()]
        jobs = []
        for m in models:
            seen = set()  # storm windows can overlap; a set stops the same run being queued twice
            for a, b in windows:
                for t in cycles_between(m, cfg, pd.Timestamp(a).to_pydatetime(), pd.Timestamp(b).to_pydatetime()):
                    if t not in seen:
                        seen.add(t)
                        jobs.append((m, t))
        return jobs
    if start is not None:
        a = pd.Timestamp(start).to_pydatetime()
        b = pd.Timestamp(end).to_pydatetime() if end else now
        return [(m, t) for m in models for t in cycles_between(m, cfg, a, b)]
    catchup = cfg["archive"].get("catchup_days") or {}
    jobs = []
    for m in models:
        n = recent or (int(catchup[m] * len(cycles(cfg, m))) if m in catchup else cfg["archive"]["recent_cycles"])
        jobs += [(m, t) for t in recent_cycles(m, cfg, now, n)]
    return sorted(jobs, key=lambda job: job[1], reverse=True)


class FailureLog:
    """Failed attempts per (model, init), kept in a CSV so they survive between passes (delete a row to retry).

    Scheduled passes skip a run once it has failed MAX_TRIES times and is over a day old (e.g. a forecast hour
    Environment Canada never posted), so it doesn't cost time every 3 hours. Manual runs always try.
    """

    COLS = ["model", "init", "tries", "last_error"]

    def __init__(self, path: Path):
        self.path = Path(path)
        self.df = pd.read_csv(self.path, parse_dates=["init"]) if self.path.exists() else pd.DataFrame(columns=self.COLS)

    def _mask(self, model, init):
        return (self.df["model"] == model) & (self.df["init"] == pd.Timestamp(init))

    def tries(self, model, init) -> int:
        hit = self.df[self._mask(model, init)]
        return int(hit["tries"].iloc[0]) if len(hit) else 0

    def give_up(self, model, init, now) -> bool:
        return self.tries(model, init) >= MAX_TRIES and now - init > timedelta(days=1)

    def failed(self, model, init, error) -> int:
        n = self.tries(model, init) + 1
        row = pd.DataFrame([{"model": model, "init": pd.Timestamp(init), "tries": n, "last_error": str(error)[:200]}])
        self.df = pd.concat([self.df[~self._mask(model, init)], row], ignore_index=True)
        return n

    def succeeded(self, model, init) -> None:
        self.df = self.df[~self._mask(model, init)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(self.path, index=False)


def run_jobs(jobs, cfg: dict, archive, failures: FailureLog, now: datetime, force: bool = False,
             scheduled: bool = True, save_dir: str | None = None, log=print) -> dict:
    """Archive every job with archive(model, init, cfg, force=, save_dir=) -> (path, status). One bad run never
    stops the rest. Returns counts: archived, present, failed, given_up."""
    n = {"archived": 0, "present": 0, "failed": 0, "given_up": 0}
    for model, init in jobs:
        label = f"{model} {init:%Y-%m-%d %Hz}"
        if scheduled and failures.give_up(model, init, now):
            n["given_up"] += 1
            continue
        t0 = time.time()
        try:
            path, status = archive(model, init, cfg, force=force, save_dir=save_dir)
            if status == "exists":
                n["present"] += 1
                if not scheduled:  # a scheduled pass checks ~300 already-saved runs; don't log each one
                    log(f"[skip]    {label} already archived")
            else:
                n["archived"] += 1
                failures.succeeded(model, init)
                log(f"[archive] {label} ({time.time() - t0:.0f}s) -> {path}")
        except Exception as e:  # noqa: BLE001  (one bad run must not stop the rest)
            n["failed"] += 1
            tries = failures.failed(model, init, e)
            log(f"[fail]    {label} ({time.time() - t0:.0f}s, try {tries}): {e}")
        failures.save()  # after every run, so a crash mid-pass keeps the record
    return n


def split_streams(models: list[str]) -> dict[str, list[str]]:
    """{stream: its models}, keeping only streams that have a model in `models` (registry.STREAMS)."""
    groups = {name: [m for m in models if m in mods] for name, mods in STREAMS.items()}
    return {k: v for k, v in groups.items() if v}


def run_streams(groups: dict[str, list[str]], script: str, passthrough: list[str], log=print) -> list[int]:
    """One child process per stream, all at once (processes, not threads: the GRIB decoder isn't thread-safe).
    Each child runs `script --stream NAME --models ...`; its output is relayed line by line, prefixed. Returns the
    children's exit codes."""
    env = {**os.environ, "PYTHONUTF8": "1"}

    def relay(name, proc):
        for line in proc.stdout:
            if line.strip():
                log(f"[{name:6s}] {line.rstrip()}")

    procs, relays = [], []
    for name, ms in groups.items():
        cmd = [sys.executable, os.path.abspath(script), "--stream", name, "--models", *ms, *passthrough]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env=env)
        procs.append(p)
        relays.append(threading.Thread(target=relay, args=(name, p), daemon=True))
        relays[-1].start()
    codes = [p.wait() for p in procs]
    for t in relays:
        t.join(timeout=10)
    return codes
