"""NWS Salt Lake storm-total statements (PNSSLC) as Alta Collins storm totals (owner, 2026-11-10; docs/PLAN.md step 3).
Free, public NWS products, archived by the Iowa Environmental Mesonet back many years:
https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=PNSSLC

Two layouts, both read:
  TABLE ("...Preliminary Storm Information...", most winters): a fixed-width table under a header such as
      ***** Precip Reports *****                Time        Snow     Precip
      Alta - Collins - 9662 ft                 4 AM Thu     47.0      4.72
    The header's columns vary (some have no Snow column: 11/01/2026 gave Collins water only), so each number goes to
    the header label its right edge lines up with. Times are an hour and a weekday.
  SECTIONS (the 2023-24 winter): "...SNOWFALL REPORTS..." and "...PRECIPITATION REPORTS...", one line per value:
      Alta - Collins               28.0 in   0436 AM 12/09   40.58N/111.64W
    so a station's snow and water come from two lines. Times are a clock time and a month/day.
The station name varies ("Alta Collins", "Alta - Collins"; config pns.sites). The storm's start is stated in prose
that varies ("from 8am Monday through 7am Thursday", "Storm totals beginning Monday morning", "beginning December 7,
2023"): read when it can be (start_local), always kept as written (start_note). A storm usually gets several
statements, each a running total; the latest is the storm's. Statements are PRELIMINARY and only for notable storms.

Scoring order (owner, 2026-11-10): the Worker's Collins hours first, then these statements (storms.pns_totals).
"""
from __future__ import annotations

import re

import pandas as pd

from ..timefmt import LOCAL_TZ

SOURCE = "nws statement"   # the source name its rows carry among the storm totals (archive.obs.load_totals)
COLS = ["product_id", "issued_utc", "start_note", "start_local", "site", "reading_utc", "snow_in", "water_in"]

_HEADER = re.compile(r"^\*{5} .+ Reports \*{5}")
_SECTION = re.compile(r"^\.\.\.(SNOWFALL|PRECIPITATION|[A-Z ]+) REPORTS\.\.\.", re.IGNORECASE)
_ISSUED = re.compile(r"^(\d{1,2})(\d{2}) (AM|PM) (M[SD]T) \w{3} (\w{3}) (\d{1,2}) (\d{4})\s*$")
_ROW = re.compile(r"^(?P<name>.+?) - (?P<elev>\d+) ft\s+(?P<hour>\d{1,2}) (?P<ampm>AM|PM) (?P<day>\w{3})\b")
_LINE = re.compile(r"^(?P<name>\S.*?)\s{2,}(?P<amount>\d+(?:\.\d+)?) in\s+(?P<hh>\d{1,2})(?P<mm>\d{2}) (?P<ampm>AM|PM) "
                   r"(?P<month>\d{1,2})/(?P<day>\d{1,2})\b")
_START = re.compile(r"\b(?:from|beginning|starting|since|between)\b(?P<rest>[^.]*)", re.IGNORECASE)
_PROSE_END = re.compile(r"\*{5}|^\.\.\.[A-Z ]+ REPORTS\.\.\.", re.MULTILINE | re.IGNORECASE)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_PARTS = {"morning": 6, "afternoon": 12, "evening": 18, "night": 18}


def fetch(cfg: dict, begin: str, end: str) -> str:
    """Every PNSSLC statement issued begin..end (YYYY-MM-DD, UTC) as IEM's plain text (statements between \\x01 and
    \\x03)."""
    import requests
    c = cfg["pns"]
    r = requests.get(c["url"], params={"pil": c["pil"], "sdate": begin, "edate": end, "limit": c["limit"],
                                       "fmt": "text"}, headers={"User-Agent": c["user_agent"]}, timeout=c["timeout_s"])
    r.raise_for_status()
    return r.text


def statements(text: str) -> list[str]:
    """IEM's text split into single statements: those giving storm totals in either layout."""
    parts = [p.strip("\x03\r\n ") for p in text.replace("\r", "").split("\x01")]
    return [p for p in parts if "PNSSLC" in p and ("preliminary storm information" in p.lower()
                                                   or re.search(r"^\.\.\.(SNOWFALL|PRECIPITATION) REPORTS\.\.\.", p,
                                                                re.MULTILINE | re.IGNORECASE))]


def issued(lines: list[str]) -> pd.Timestamp | None:
    """'751 AM MST Thu Feb 19 2026' -> naive UTC (MST = UTC-7, MDT = UTC-6), or None."""
    for line in lines[:15]:
        m = _ISSUED.match(line.strip())
        if m:
            h, mi, ampm, zone, mon, day, year = m.groups()
            local = pd.to_datetime(f"{year} {mon} {day} {int(h) % 12 + (12 if ampm == 'PM' else 0)}:{mi}",
                                   format="%Y %b %d %H:%M")
            return local + pd.Timedelta(hours=7 if zone == "MST" else 6)
    return None


def _on_or_before(day: str, hour: int, ref_local: pd.Timestamp) -> pd.Timestamp | None:
    """The latest local time on `day` (Mon..Sun) at `hour` that is not after ref_local (within the last 7 days)."""
    if day.lower()[:3] not in _DAYS:
        return None
    back = (ref_local.weekday() - _DAYS.index(day.lower()[:3])) % 7
    t = (ref_local - pd.Timedelta(days=back)).normalize() + pd.Timedelta(hours=hour)
    return t - pd.Timedelta(days=7) if t > ref_local else t


def _dated(month: int, day: int, ref_local: pd.Timestamp) -> pd.Timestamp:
    """month/day in the statement's year, or the year before when that would be after it (December in January)."""
    t = pd.Timestamp(year=ref_local.year, month=month, day=day)
    return t.replace(year=t.year - 1) if t > ref_local + pd.Timedelta(days=1) else t


def _hour(text: str) -> int | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    m = re.search(r"\b(noon|midnight)\b", text, re.IGNORECASE)
    if m:
        return 12 if m.group(1).lower() == "noon" else 0
    m = re.search(r"\b(" + "|".join(_PARTS) + r")\b", text, re.IGNORECASE)
    return _PARTS[m.group(1).lower()] if m else None


def start_of(prose: str, issued_local: pd.Timestamp) -> tuple[str, pd.Timestamp | None]:
    """The stated storm start (the prose as written, and a naive local time when one can be read). Only the words
    before through / until / to / ending / and count. A date ("October 31st", "Feb 16") wins over a weekday; the
    hour is a clock time, noon / midnight or a part of the day (morning 6, afternoon 12, evening / night 18), else
    midnight. Neither a date nor a weekday: no start."""
    flat = " ".join(prose.split())
    m = _START.search(flat)
    if not m:
        return "", None
    rest = re.split(r"\b(?:through|thru|until|to|ending|and|continuing)\b", m.group("rest"), maxsplit=1,
                    flags=re.IGNORECASE)[0]
    note = m.group(0).strip()
    hour = _hour(rest)
    date = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                     rest, re.IGNORECASE)
    if date:
        t = _dated(pd.to_datetime(date.group(1)[:3], format="%b").month, int(date.group(2)), issued_local)
        return note, t + pd.Timedelta(hours=hour or 0)
    day = re.search(r"\b(" + "|".join(_DAYS) + r")[a-z]*\b", rest, re.IGNORECASE)
    if day:
        t = _on_or_before(day.group(1), 0, issued_local.normalize() + pd.Timedelta(hours=23, minutes=59))
        return note, t + pd.Timedelta(hours=hour or 0) if t is not None else None
    return note, None


def _columns(header: str) -> dict[str, int]:
    """Header label -> the column its values end at (values are right-aligned under the label)."""
    return {m.group(0).lower(): m.end() for m in re.finditer(r"Snow|Precip|Time", header)}


def _site(name: str, sites: dict[str, str]) -> str | None:
    return next((s for s, rx in sites.items() if re.search(rx, name.strip(), re.IGNORECASE)), None)


def _readings(lines: list[str], sites: dict[str, str], issued_local: pd.Timestamp) -> list[dict]:
    """Every value for a station in `sites`, from either layout: {site, local, snow} and/or {water}."""
    out, cols, section = [], {}, ""
    for line in lines:
        if _HEADER.match(line):                          # TABLE layout
            cols = _columns(line) if "Precip" in line or "Snow" in line else {}
            continue
        s = _SECTION.match(line)
        if s:                                            # SECTIONS layout
            section = s.group(1).lower()
            continue
        m = _ROW.match(line)
        if m and cols:
            site = _site(m.group("name"), sites)
            if site is None:
                continue
            got = {}
            for v in re.finditer(r"\d+(?:\.\d+)?", line[m.end():]):
                right = m.end() + v.end()
                label = min(cols, key=lambda k: abs(cols[k] - right))
                if label in ("snow", "precip") and abs(cols[label] - right) <= 3:
                    got["snow" if label == "snow" else "water"] = float(v.group(0))
            local = _on_or_before(m.group("day"), int(m.group("hour")) % 12 + 12 * (m.group("ampm") == "PM"),
                                  issued_local)
            if got and local is not None:
                out.append({"site": site, "local": local, **got})
            continue
        m = _LINE.match(line)
        if m and section in ("snowfall", "precipitation"):
            site = _site(m.group("name"), sites)
            if site is None:
                continue
            local = (_dated(int(m.group("month")), int(m.group("day")), issued_local)
                     + pd.Timedelta(hours=int(m.group("hh")) % 12 + 12 * (m.group("ampm") == "PM"),
                                    minutes=int(m.group("mm"))))
            out.append({"site": site, "local": local,
                        ("snow" if section == "snowfall" else "water"): float(m.group("amount"))})
    return out


def parse_statement(text: str, sites: dict[str, str]) -> list[dict]:
    """One statement -> one row per station in `sites` ({our site: regex on the station name}). A station listed
    more than once: a line with both values wins (the latest); otherwise its latest snow and latest water are joined,
    read at the later of their times. Values whose time can't be read are left out."""
    lines = text.split("\n")
    when = issued(lines)
    if when is None:
        return []
    issued_local = when.tz_localize("UTC").tz_convert(LOCAL_TZ).tz_localize(None)
    values = _readings(lines, sites, issued_local)
    if not values:
        return []
    # the prose: after the issue line (and the title, when there is one), before the first table or section
    first = next(i for i, ln in enumerate(lines) if _ISSUED.match(ln.strip()))
    body = _PROSE_END.split("\n".join(lines[first + 1:first + 40]), maxsplit=1)[0]
    note, start = start_of(body.split("Preliminary Storm Information...")[-1], issued_local)
    product = next((ln.strip() for ln in lines[:6] if ln.startswith("NOUS")), "")
    rows = []
    for site, g in pd.DataFrame(values).reindex(columns=["site", "local", "snow", "water"]).groupby("site"):
        g = g.sort_values("local")
        both = g.dropna(subset=["snow", "water"])
        if not both.empty:
            snow, water, local = both.snow.iloc[-1], both.water.iloc[-1], both.local.iloc[-1]
        else:
            sn, wa = g.dropna(subset=["snow"]), g.dropna(subset=["water"])
            snow = sn.snow.iloc[-1] if not sn.empty else None
            water = wa.water.iloc[-1] if not wa.empty else None
            local = max(x.local.iloc[-1] for x in (sn, wa) if not x.empty)
        reading = local.tz_localize(LOCAL_TZ, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")
        rows.append({"product_id": product, "issued_utc": when, "start_note": note, "start_local": start,
                     "site": site, "reading_utc": reading.tz_localize(None), "snow_in": snow, "water_in": water})
    return rows


def parse(text: str, sites: dict[str, str]) -> pd.DataFrame:
    """Every statement in IEM's text -> one row per station per statement (COLS)."""
    rows = [r for s in statements(text) for r in parse_statement(s, sites)]
    df = pd.DataFrame(rows, columns=COLS)
    if df.empty:
        return df
    return df.drop_duplicates(["issued_utc", "site"], keep="last").sort_values(["issued_utc", "site"]).reset_index(drop=True)
