new_snow.csv: hand-entered ski-area / UAC observations used by verify.py.
  date          local date of the morning reading (the 24 h ending that morning), e.g. 2026-11-23
  site          must match a model point name in config/stations.yaml (alta, snowbird, brighton)
  new_snow_in   24 h new snow in inches
  swe_in        optional: water content of that snow, inches
  notes         free text

storm_totals.csv (Alta Collins etc.) lives in data/entries/ (see the README there): it must reach the cloud
passes without a code release, so it's synced through the bucket instead of git.
