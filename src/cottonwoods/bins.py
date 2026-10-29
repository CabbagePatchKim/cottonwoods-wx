"""The owner's snowfall bins and the bin score (config verify.snow_bins_in, verify.bin_score).

The owner forecasts in fixed overlapping inch bins (0-2, 1-3, 2-4, 3-6, ... 34-40, then 6" wide stepping by 4 above
that). An amount's bin is the LOWEST bin that contains it (the owner's pessimistic pick), so 4" is 2-4 and 4.5" is 3-6.

Bin score of a forecast against the observed snow (owner, 2026-10-27):
  right bin     the observed amount is inside the forecast's bin: + reward (a 3-6 forecast and 4" observed is right)
  one bin off   0: neighbouring bins overlap, so they mostly agree
  further off   a ding of per_bin for every bin beyond free_bins (2 bins off -1, 3 bins off -2, ...)
bins_off is signed (forecast bin minus observed bin): positive = the model was too high. Kept for the learning layer,
which uses it to decide which way to lean.

Event call (owner, 2026-10-29): the bins are wide at the bottom (0-2 holds a dry storm and 1.9"), so a model that
called for a storm (>= verify.event_swe_in water) when none came, or called it dry when one did, scores at most
bin_score.event_cap (0) even inside the right bin. NAM Nest's 1.4" for the dry 10/27 storm scored +2 before this.
"""
from __future__ import annotations


def snow_bins(vcfg: dict, upto: float = 0.0) -> list[tuple[float, float]]:
    """The bins from config, extended above the last listed bin (snow_bins_above) until one reaches `upto`."""
    bins = [tuple(b) for b in vcfg["snow_bins_in"]]
    above = vcfg["snow_bins_above"]
    lo = above["first_low"]
    while bins[-1][1] < upto:
        bins.append((lo, lo + above["width"]))
        lo += above["step"]
    return bins


def bin_index(x: float, bins: list[tuple[float, float]]) -> int:
    """Index of the lowest bin containing x (negative amounts count as 0)."""
    x = max(float(x), 0.0)
    return next(i for i, (lo, hi) in enumerate(bins) if lo <= x <= hi)


def label(b: tuple[float, float]) -> str:
    return f"{b[0]:g}-{b[1]:g}"


def event_call(fc_event: bool, obs_event: bool) -> str:
    """'hit', 'false alarm' (called for a storm that didn't come), 'miss' (called dry, it came) or 'dry'."""
    return {(True, True): "hit", (True, False): "false alarm", (False, True): "miss", (False, False): "dry"}[
        (bool(fc_event), bool(obs_event))]


def bin_score(fc_in: float, obs_in: float, vcfg: dict, fc_event: bool | None = None, obs_event: bool | None = None) -> dict:
    """fc_bin, obs_bin (labels), bins_off (signed, + = forecast too high), hit, bin_points (bins alone), points
    (bin_points capped at event_cap for a false alarm or miss, when the event flags are given), event."""
    bins = snow_bins(vcfg, max(fc_in, obs_in))
    i, j = bin_index(fc_in, bins), bin_index(obs_in, bins)
    lo, hi = bins[i]
    hit = lo <= max(float(obs_in), 0.0) <= hi
    sc = vcfg["bin_score"]
    points = sc["reward"] if hit else -max(0, abs(i - j) - sc["free_bins"]) * sc["per_bin"]
    call = event_call(fc_event, obs_event) if fc_event is not None and obs_event is not None else ""
    capped = min(points, sc["event_cap"]) if call in ("false alarm", "miss") else points
    return {"fc_bin": label(bins[i]), "obs_bin": label(bins[j]), "bins_off": i - j, "hit": hit, "bin_points": points,
            "points": capped, "event": call}
