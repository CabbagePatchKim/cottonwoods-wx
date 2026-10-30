"""The storm report as a web page (owner, 2026-10-27 plan; built 2026-10-29): data/reports/storms_latest.html, rebuilt
every pass next to the text report. One self-contained file (inline CSS, no scripts, works offline, light and dark).
Internal only: never the feed.

Per storm still ahead or under way, summary first:
  header      window, length, the run that set it, flow / modes / parts
  per resort  the owner's bin (lowest bin containing the median), median and range of the deterministic runs covering
              the whole storm, change since each model's previous run, ensemble wet share, confidence (html.confidence)
  changes     one sentence: what moved since the last cycle at storms.block_point
  chances     probs.py, UNCALIBRATED: ensemble water thresholds and snow bins, the time-lagged high-res set
  timeline    trends.py at storms.block_point: onset / peak / end per model, change vs its previous run, its last runs
Then NOAA ALTA (coop) daily observations and storm totals, to compare with Alta Collins by eye, and the full text report
(every model at every point, 12 h blocks, verification) in a collapsed section.
"""
from __future__ import annotations

from html import escape

import numpy as np
import pandas as pd

from . import bins, probs, timefmt, trends
from .report import regime_adjusted, storm_models, storm_notes

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;--card:#ffffff;--up:#1f7a3a;--down:#b3261e;--accent:#2b5c8a}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a19b;--line:#33322f;--card:#1f1f1d;
--up:#6cc58a;--down:#ff8a80;--accent:#8ab4e0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0}
h2{font-size:19px;margin:28px 0 4px;border-top:2px solid var(--line);padding-top:14px}h3{font-size:15px;margin:18px 0 6px}
.muted{color:var(--muted)}.note{color:var(--muted);font-size:13px;margin:2px 0}.wrap{overflow-x:auto}
table{border-collapse:collapse;margin:4px 0 10px;font-variant-numeric:tabular-nums}th,td{padding:4px 9px;
border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{font-weight:600;color:var(--muted);
font-size:13px}td:first-child,th:first-child{text-align:left}.bin{display:inline-block;min-width:52px;padding:1px 7px;
border-radius:4px;color:#fff;font-weight:600;text-align:center}.up{color:var(--up)}.down{color:var(--down)}
.conf-high{color:var(--up);font-weight:600}.conf-low{color:var(--down);font-weight:600}.conf-medium{font-weight:600}
.changes{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:8px 0}
details{margin:10px 0}summary{cursor:pointer;color:var(--accent);font-weight:600}pre{font-size:12px;overflow-x:auto;
background:var(--card);border:1px solid var(--line);padding:10px;border-radius:6px}
"""


def _bin(snow: float, vcfg: dict) -> str:
    """The owner's bin for an amount as a coloured label (deeper colour = more snow)."""
    if pd.isna(snow):
        return '<span class="muted">-</span>'
    b = bins.snow_bins(vcfg, snow)
    i = bins.bin_index(snow, b)
    return (f'<span class="bin" style="background:hsl({205 + min(i, 14) * 6} 62% {52 - min(i, 14) * 1.6:.0f}%)">'
            f"{bins.label(b[i])}</span>")


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Cells are already HTML (escape text before passing it)."""
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _pct(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:.0%}"


def _delta(x, steady: float, unit: str = '"') -> str:
    """+2.1" up arrow / -1.0" down arrow / steady, from a change in inches."""
    if x is None or pd.isna(x):
        return '<span class="muted">-</span>'
    if abs(x) < steady:
        return f'<span class="muted">&asymp; {x:+.1f}{unit}</span>'
    return f'<span class="{"up" if x > 0 else "down"}">{"&#9650;" if x > 0 else "&#9660;"} {x:+.1f}{unit}</span>'


def _shift(h) -> str:
    """Hours earlier / later."""
    if h is None or pd.isna(h):
        return '<span class="muted">-</span>'
    return "same" if h == 0 else f"{abs(h):.0f} h {'later' if h > 0 else 'earlier'}"


def _when(t) -> str:
    return "-" if t is None or pd.isna(t) else escape(timefmt.clock(t, weekday=True))


def _name(p: str, cfg: dict) -> str:
    """A point's display name: its config label, else the key capitalized ("alta" -> "Alta")."""
    return cfg["points"][p].get("label", p.replace("_", " ").title())


def _snow(c: dict) -> float:
    """A run's snow at a point as the text report shows it: regime-adjusted when there is one."""
    return c["snow_in_regime"] if pd.notna(c.get("snow_in_regime", np.nan)) else c["snow_in"]


def confidence(spread: float, wet: float, ccfg: dict) -> str:
    """'high', 'medium' or 'low' from the deterministic spread ((max - min) / median) and the ensemble wet share."""
    hi, lo = ccfg["high"], ccfg["low"]
    if pd.isna(spread) or (pd.notna(wet) and wet < lo["wet"]) or spread > lo["spread"]:
        return "low"
    if spread <= hi["spread"] and (pd.isna(wet) or wet >= hi["wet"]):
        return "high"
    return "medium"


def resort_rows(models: list[dict], tr: dict[str, list[dict]], cfg: dict) -> list[list[str]]:
    """The per-resort summary rows (see the module notes); tr = trends.model_trends per point."""
    rows, steady = [], cfg["trends"]["steady_in"]
    for p in cfg["points"]:
        snow = [_snow(r["points"][p]) for r in models
                if r.get("kind") == "deterministic" and not r["ensemble"] and r["full"] and r["points"].get(p)]
        snow = [x for x in snow if pd.notna(x)]
        ens = [r["points"][p] for r in models if r["ensemble"] and r["points"].get(p)]
        wet = float(np.mean([c["members_wet"] / c["members"] for c in ens])) if ens else np.nan
        changes = [m["change"]["snow_in"] for m in tr.get(p, []) if m["change"]]
        if snow:
            med = float(np.median(snow))
            spread = (max(snow) - min(snow)) / max(med, 1.0)
            conf = confidence(spread, wet, cfg["html"]["confidence"])
            cells = [_bin(med, cfg["verify"]), f"{med:.1f}\"", f"{min(snow):.0f}-{max(snow):.0f}\"", str(len(snow)),
                     _delta(float(np.median(changes)) if changes else None, steady), _pct(wet),
                     f'<span class="conf-{conf}">{conf}</span>']
        else:
            cells = ['<span class="muted">-</span>', "-", "-", "0", "-", _pct(wet), "-"]
        rows.append([escape(_name(p, cfg))] + cells)
    return rows


def changes_sentence(tr: list[dict], ens: list[dict], label: str, steady: float) -> str:
    """What moved since each model's previous run at the block point, in one sentence."""
    ch = [m["change"] for m in tr if m["change"]]
    if not ch:
        return f"{escape(label)}: no model has two runs covering the whole storm yet."
    up = sum(c["snow_in"] >= steady for c in ch)
    down = sum(c["snow_in"] <= -steady for c in ch)
    on = [c["onset_h"] for c in ch if c["onset_h"] is not None]
    text = (f"{escape(label)}, since each model's previous run: snow up in {up}, down in {down}, about the same in "
            f"{len(ch) - up - down} of {len(ch)} models (median change {np.median([c['snow_in'] for c in ch]):+.1f}\")")
    if on:
        text += f"; snow starts earlier in {sum(h < 0 for h in on)}, later in {sum(h > 0 for h in on)}"
    for m in ens:
        if len(m["runs"]) >= 2:
            text += f"; {m['model']} wet {m['runs'][1]['wet']:.0%} &rarr; {m['runs'][0]['wet']:.0%}"
    return text + "."


def chances_html(st, fc: pd.DataFrame, cfg: dict, point: str, label: str) -> str:
    """The probabilities section (UNCALIBRATED shares of members)."""
    thr = cfg["probs"]["water_in"]
    top = lambda shares: " &middot; ".join(f"{b} {v:.0%}" for b, v in sorted(shares.items(), key=lambda kv: -kv[1])[:4])  # noqa: E731
    rows = []
    for e in probs.ensemble_probs(st, fc, cfg, point):
        fam = (" (each member's own snow)" if e["own_snow"] else
               f" (snow via {e['family'][0]} {timefmt.cycle(e['family'][1])})" if e["family"] else "")
        rows.append([f"{e['model']} {timefmt.cycle(e['init'])}" + ("" if e["full"] else " (partial)"), str(e["members"]),
                     *[_pct(e["water"][t]) for t in thr], (top(e["snow_bins"]) or "-") + escape(fam)])
    lg = probs.lagged_probs(st, fc, cfg, point)
    rows.append([f"high-res, last {cfg['probs']['lagged']['runs']} runs", str(lg["members"]),
                 *[_pct(lg["water"][t]) for t in thr],
                 top(lg["snow_bins"]) or f"no run covers the whole storm ({lg['short']} too short)"])
    out = [f"<h3>Chances at {escape(label)} <span class='muted'>(share of members; uncalibrated)</span></h3>",
           _table(["set", "members", *[f"water &ge; {t:g}\"" for t in thr], "likeliest snow bins"], rows),
           "<p class='note'>GEFS / ENS snow bins reflect the spread in WATER only: each member's water is turned into "
           "snow with its family's deterministic run (the run named), so every member shares that run's temperatures, "
           "snow ratio and rain/snow split. In a marginal storm the snow level is the bigger unknown; the RRFS "
           "ensemble members and the high-res runs use their own temperatures.</p>"]
    blk = probs.lagged_block_probs(st, fc, cfg, point)
    if any(b["members"] for b in blk):
        snow_in = cfg["probs"]["lagged"]["block_snow_in"]
        out += [f"<p class='note'>High-res runs block by block (each run covering the whole 12 h block): share with "
                f"&ge; {snow_in:g}\" snow, median snow, runs.</p>",
                _table(["", *[escape(b["label"]) for b in blk]],
                       [[f"&ge; {snow_in:g}\""] + [_pct(b["snow_share"]) for b in blk],
                        ["median"] + ["-" if pd.isna(b["median_snow"]) else f"{b['median_snow']:.1f}\"" for b in blk],
                        ["runs"] + [str(b["members"]) for b in blk]])]
    return "".join(out)


def guidance_html(st, fc: pd.DataFrame, models: list[dict], cfg: dict, point: str, label: str) -> str:
    """Post-processed guidance (U of U at Collins, machine-learning snow ratio) next to OUR newest run of the model it
    comes from, over the storm window; kept out of the summary, medians and bins (owner, 2026-10-30)."""
    from .models.registry import REGISTRY
    by = {r["model"]: r for r in models}
    rows = []
    for r in [r for r in models if r.get("kind") == "guidance"]:
        c, base = r["points"].get(point), REGISTRY[r["model"]].based_on
        b = by.get(base, {}).get("points", {}).get(point) if base in by else None
        g = fc[(fc["model"] == r["model"]) & (fc["init"] == r["init"]) & (fc["point"] == point)
               & (fc["valid_end"] > st.fc_start) & (fc["valid_start"] < st.fc_end) & (fc["qpf_mm"] > 0)]
        slr = (g["snow_issued_in"].sum() / (g["qpf_mm"].sum() / 25.4)) if len(g) and g["qpf_mm"].sum() > 0 else np.nan
        cover = "" if r["full"] else (" (from " + _when(r["valid_from"]) + ")" if r["late"] else " (to " + _when(r["valid_to"]) + ")")
        inch = lambda x, spec: "-" if x is None or pd.isna(x) else format(x, spec) + "&quot;"   # noqa: E731
        rows.append([escape(r["model"]), timefmt.cycle(r["init"]) + cover,
                     inch(c and c["water_in"], ".2f"), inch(c and c["snow_in"], ".1f"),
                     "-" if pd.isna(slr) else f"{slr:.0f}:1",
                     f"{base} {timefmt.cycle(by[base]['init'])}" if base in by else "-",
                     inch(b and b["water_in"], ".2f"), inch(b and b["snow_in"], ".1f")])
    if not rows:
        return ""
    return (f"<h3>Guidance at {escape(label)} <span class='muted'>(U of U, experimental; not in the summary)</span></h3>"
            + _table(["guidance", "run", "water", "snow", "their ratio", "our run", "our water", "our snow"], rows)
            + "<p class='note'>University of Utah (Steenburgh group) Little Cottonwood guidance at Collins: the model "
              "with their machine-learning snow ratio. Our run = our newest run of the same model with our snow method, "
              "plain (not regime-adjusted), so the two snow ratios compare directly.</p>")


def timeline_html(tr: list[dict], ens: list[dict], cfg: dict, label: str) -> str:
    """The block point's timeline and run-to-run trend per model."""
    steady, rows = cfg["trends"]["steady_in"], []
    for m in tr:
        r = m["runs"][0]
        t, c = r["timeline"], m["change"] or {}
        path = " &rarr; ".join(f"{x:.0f}" for x in m["snow_path"])
        arrow = {"up": " <span class='up'>&#9650;</span>", "down": " <span class='down'>&#9660;</span>",
                 "mixed": " <span class='muted'>mixed</span>", "steady": " <span class='muted'>steady</span>"}.get(m["direction"], "")
        peak = f"{t['peak_rate']:.2f}\"/h, {_when(t['peak_start'])}" if t["peak_start"] is not None else "-"
        rows.append([m["model"], timefmt.cycle(r["init"]) + ("" if r["full"] else " (partial)"), _when(t["onset"]), peak,
                     _when(t["end"]), f"{t['snow']:.1f}\"", f"{t['water']:.2f}\"", _delta(c.get("snow_in"), steady),
                     _shift(c.get("onset_h")), (path + arrow) if path else "-"])
    tc = cfg["trends"]
    out = [f"<h3>Timeline at {escape(label)}</h3>",
           f"<p class='note'>Snow starts / ends when the {tc['rate_hours']} h average reaches "
           f"{tc['onset_rate_in_hr']:g}\"/h; peak = the highest {tc['rate_hours']} h average. Change = newest run vs "
           f"the previous one; last runs = snow of up to {tc['runs']} runs covering the whole storm, oldest first.</p>",
           _table(["model", "run", "snow starts", "peak", "snow ends", "snow", "water", "change", "start moved",
                   "last runs"], rows)]
    if ens:
        out.append(_table(["ensemble", *[f"run {i + 1}" for i in range(tc["runs"])]],
                          [[m["model"]] + [f"{timefmt.cycle(r['init'])}: {r['wet']:.0%} wet, {r['mean_water_in']:.2f}\""
                                           for r in m["runs"]] for m in ens]))
    return "".join(out)


def storm_html(st, fc: pd.DataFrame, cfg: dict) -> str:
    """One storm's section."""
    models = storm_models(st, fc, cfg)
    s, e = st.fc_start, st.fc_end
    bp = cfg["storms"]["block_point"]
    label = _name(bp, cfg)
    tr = {p: trends.model_trends(st, fc, cfg, p) for p in cfg["points"]}
    ens = trends.ensemble_trends(st, fc, cfg, bp)
    notes = "".join(f"<p class='note'>{escape(k)}: {escape(v)}</p>" for k, v in storm_notes(st))
    head = (f"<h2>{escape(timefmt.clock(s, weekday=True))} &ndash; {escape(timefmt.clock(e, weekday=True))}</h2>"
            f"<p class='muted'>Storm {escape(str(st.storm_id))}, {(e - s) / pd.Timedelta(hours=1):.0f} h; window from "
            f"{escape(str(st.defining_model))} {escape(timefmt.run(st.defining_init))}</p>{notes}")
    if not models:
        return head + "<p>No model reaches this storm yet.</p>"
    summary = _table(["", "bin", "median", "range", "models", "since last run", "ensembles wet", "confidence"],
                     resort_rows(models, tr, cfg))
    return (head + summary
            + "<p class='note'>bin = the owner's bin holding the median of the deterministic runs covering the whole "
              "storm (snow regime-adjusted where factors exist, like the text report); since last run = median change "
              "of each model's snow vs its previous run.</p>"
            + f"<div class='changes'>{changes_sentence(tr[bp], ens, label, cfg['trends']['steady_in'])}</div>"
            + guidance_html(st, fc, models, cfg, bp, label) + chances_html(st, fc, cfg, bp, label)
            + timeline_html(tr[bp], ens, cfg, label))


def noaa_html(ghcn: pd.DataFrame | None, storms: pd.DataFrame, cfg: dict, now) -> str:
    """NOAA ALTA (coop) daily water, new snow and depth, and totals over recent storms (observer days overlapping
    the storm window), to compare with Alta Collins by eye. NOAA posts these 1-3 weeks late."""
    head = "<h2>NOAA ALTA (coop observer)</h2>"
    d = ghcn[ghcn["site"] == "alta_noaa"] if ghcn is not None and not ghcn.empty else pd.DataFrame()
    if d.empty:
        return head + "<p class='muted'>No NOAA ALTA values stored yet (they post 1-3 weeks late).</p>"
    d = d.assign(value=d["value"].where(~d["trace"].fillna(False).astype(bool), 0.0))
    wide = d.pivot_table(index="time_utc", columns="element", values="value", aggfunc="last")
    trace = d[d["trace"].fillna(False).astype(bool)].groupby("time_utc")["element"].agg(set)
    std = pd.Timedelta(hours=cfg["local_settings"]["standard_offset_hours"])
    fmt = lambda t, el, spec: ("T" if el in trace.get(t, set()) else "-" if pd.isna(wide.at[t, el])  # noqa: E731
                               else format(wide.at[t, el], spec)) if el in wide.columns else "-"
    days = wide.index.sort_values()[-cfg["html"]["noaa_days"]:][::-1]
    rows = [[escape(f"{t + std:%a %m/%d}"), fmt(t, "PRCP", ".2f"), fmt(t, "SNOW", ".1f"), fmt(t, "SNWD", ".0f")]
            for t in days]
    out = [head, f"<p class='note'>Each day = the observer's 24 h ending 6 pm MST on that date; newest stored day "
                 f"{escape(f'{wide.index.max() + std:%m/%d}')} (NOAA posts 1-3 weeks late). T = trace.</p>",
           _table(["day", "water", "new snow", "depth"], rows)]
    recent = storms[storms["status"].isin(["forecast", "verified"])
                    & (pd.to_datetime(storms["fc_end"]) >= pd.Timestamp(now) - pd.Timedelta(days=cfg["storms"]["report_verified_days"]))]
    srows = []
    for _, st in recent.sort_values("fc_start").iterrows():
        s = st.final_start if pd.notna(st.get("final_start")) else st.fc_start
        e = st.final_end if pd.notna(st.get("final_end")) else st.fc_end
        span = pd.date_range(s.normalize() + pd.Timedelta(hours=1), e + pd.Timedelta(days=1), freq="D")
        need = [t for t in span if t > s and t - pd.Timedelta(days=1) < e and t <= pd.Timestamp(now)]
        have = [t for t in need if t in wide.index]
        tot = lambda el: wide.loc[have, el].sum() if have and el in wide.columns else np.nan  # noqa: E731
        srows.append([escape(f"{timefmt.clock(s, weekday=True)} - {timefmt.clock(e, weekday=True)}"),
                      "-" if pd.isna(tot("PRCP")) else f"{tot('PRCP'):.2f}\"",
                      "-" if pd.isna(tot("SNOW")) else f"{tot('SNOW'):.1f}\"",
                      f"{len(have)} of {len(need)}" if need else "not started"])
    if srows:
        out += ["<h3>Storm totals</h3>", _table(["storm", "water", "new snow", "days posted"], srows)]
    return "".join(out)


def render_html(storms: pd.DataFrame, fc: pd.DataFrame, cfg: dict, now, text: str = "",
                ghcn: pd.DataFrame | None = None) -> str:
    """The whole page: every storm still ahead or under way, NOAA ALTA, and the full text report."""
    fc, adjusted = regime_adjusted(fc, cfg)
    open_ = storms[(storms["status"] == "forecast") & (pd.to_datetime(storms["fc_end"]) > pd.Timestamp(now))]
    body = [storm_html(st, fc, cfg) for _, st in open_.sort_values("fc_start").iterrows()] or \
           ["<p>No storms in the newest model runs.</p>"]
    full = f"<details><summary>Full text report</summary><pre>{escape(text)}</pre></details>" if text else ""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Cottonwoods storms</title><style>{CSS}</style></head>"
            f"<body><main><h1>Cottonwoods storms</h1><p class='muted'>{escape(timefmt.now_line(now))}. Times Mountain, "
            f"24-hour. Snow {'regime-adjusted' if adjusted else 'plain Kuchera'}. Internal; not the feed.</p>"
            + "".join(body) + noaa_html(ghcn, storms, cfg, now) + full + "</main></body></html>")
