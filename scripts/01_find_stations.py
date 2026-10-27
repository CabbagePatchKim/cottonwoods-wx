"""Look up SNOTEL station triplets so they can be pasted into config/stations.yaml."""
# Run once from the project folder:  python scripts/01_find_stations.py
# It asks the NRCS server for every active Utah SNOTEL station whose name contains each search word,
# and prints the "triplet" (e.g. 766:UT:SNTL), the ID the data API needs.
# Already done for this project: the results are in config/stations.yaml.
from cottonwoods.obs.snotel import find_stations

# Each string is a partial, case-insensitive name match ("Mill D" finds "Mill-D North").
for q in ["Alta", "Snowbird", "Brighton", "Mill D", "Atwater"]:
    print(f"\n== {q}")
    print(find_stations(q).to_string(index=False))
