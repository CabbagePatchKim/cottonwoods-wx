# Wasatch storm notes

Running log of storm observations, to compare against future forecasts and to feed the learning model's case
checks (docs/LEARNING_PLAN.md). Collins values are private: never written here (the repo is public).
Checklist that came out of these: docs/FORECAST_CHECKLIST.md.

## Forecast: Mon 11/23 (made Fri 11/20 evening; feed storm window Mon 11/23 05-23 MST)

**Owner's forecast, worked through the checklist with Claude:**

| Point | Deterministic total | Bins |
|---|---|---|
| Alta | 1.5" | <1": 40% / 1-3": 55% / 3+": 5% |
| Snowbird | 2" | <1": 25% / 1-3": 65% / 3+": 10% |
| Brighton | 2" | <1": 30% / 1-3": 62% / 3+": 8% |
| Thaynes Canyon SNOTEL (park_city) | 2" | <1": 35% / 1-3": 58% / 3+": 7% |

Bins for Snowbird, Brighton and Thaynes assigned by Claude: Snowbird gets the most upside (favored in the SW
phase; RRFS/RDPS highest there, 0.17-0.18"); Brighton a bit less (RRFS/RDPS 0.10-0.11"); Thaynes the widest
low tail (RRFS/RDPS only 0.04-0.07", IFS 0.21"; slack W flow means little spillover).

Reasoning: water ~0.11" (mean of ENS 0.12, GEPS 0.07, GEFS ~0.15, all trending down; high-res runs wetter at
0.15-0.17"). 700 mb moderate SW early (dry, favors the Back / Snowbird), then light W -> NW at 5-10 kt (slack)
with deep light moisture, surface to ~400 mb, no gaps, shallow instability. Growth zone ~660-590 mb inside the
moist layer. Ratio: owner 9 (max precip during the SW-W phase), Claude 10-12 (saturated growth zone, -8 C
crest; lower only if graupel); RAP Kuchera ~13. Close to the <1 / 1-3 bin edge, hence the probabilities.
Not yet given: onset, peak window, end time.

Verify: Alta on Collins (private, not here); Snowbird and Brighton on SNOTEL water plus board/resort reports
for snow; Thaynes on its SNOTEL (water, and depth gain as a rough snow check).

## Storm Thu 11/19 ~05 MST - Fri 11/20 evening (still snowing lightly at ~17 MST)

**Owner's read (9:20 am MST Fri):** Big and Little Cottonwood under forecast; Wasatch Back comparatively well.
Steady flow running into Lone Peak and along the Cottonwood ridgeline rather than across it; drier than
forecast, with precip in isolated patches. Storm then outlasted the morning runs through the afternoon.
Owner: the Alta total was roughly right, but for the wrong
reasons - the heaviest-period timing and the end time were missed.

**Crest-level flow (RAP hourly analyses, BUFKIT):**

| Period (MST)      | 700 mb at Alta | 850 mb at KSLC                 |
|-------------------|----------------|--------------------------------|
| Thu am - Fri 5 am | 180-210 deg    | 155-180 deg                    |
| Fri 5-9 am        | 205-215 deg    | 180-210 deg (16Z: 209 / 13 kt) |
| Fri 10 am - 4 pm  | 220-255 deg, dropping to 1-3 kt by 3-4 pm | 260-355 deg at 5-9 kt (valley wind, not the steering flow) |

**KSLC soundings:**

| | Thu 5 pm (00Z 11/20) | Fri 5 am (12Z 11/20) |
|---|---|---|
| Surface LCL           | ~5,800 ft          | ~5,300 ft         |
| Moist (T-Td <= 2 C)    | ~9,000-14,000 ft   | ~8,000-17,000 ft  |
| Dry layer above       | 15-16 kft (6 C depression) | none to 18 kft |
| Growth zone (~13-16 kft) | partly moist    | moist             |

RAP showed the deep moist layer only intermittently hour to hour - consistent with patchy precip.

**SNOTEL, Thu 05 - Fri 15 MST:**

| Site | Water (gauge) | After 9 am Fri | Net depth |
|---|---|---|---|
| Atwater (Alta base) | 1.20" | 0.10" | +7" |
| Snowbird            | 1.60" | 0.40" | +10" |
| Brighton            | 1.60" | 0.60" | +8" |
| Mill-D North        | 0.70" | 0.10" | +5" |
| Thaynes Canyon      | 1.90" | 0.20" | +14" |

**Rates (water per 3 h, Alta BUFKIT point vs gauges):** HRRR expected 0.3-0.44" per 3 h through Thu afternoon
and night; Atwater never exceeded 0.2" (mostly 0.1-0.2"). Biggest miss Thu 11-17 (HRRR ~0.55", Atwater 0.1").
Run totals vs Atwater over the same hours: HRRR 19/12z 2.21" vs 1.20; HRRR 20/00z 1.46 vs 0.90; RRFS 19/12z
1.43 vs 1.20; GFS 19/06z 0.85 vs 1.20 (GFS dry again - for the GFS dryness check). HRRR 20/12z had a 0.31"
burst Fri 8-11 am: it fell at Brighton (0.6"), not Atwater (0.1"); cause unknown (flow was SW-WSW).

**Lessons / corrections made in discussion:**
- Atwater is not Alta: Collins (~9,600 ft, board totals) out-snowed Atwater's net depth by a wide margin. Verify Alta on
  Collins only.
- Net depth change runs low (old-snow settling plus new-snow compaction).
- 850 mb at KSLC under ~10 kt is valley wind; judge direction at 700 mb. There was no NW tail.
- Afternoon persistence came with slack WSW crest winds under a lingering moist layer.

**To do when the storm ends:** Collins water and snow vs the forecasts (private); whether the forecast water or
the snow ratio was off at Alta; add to the time-height scoring; end-time scoring.
