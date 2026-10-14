# Sweden Accessibility Map

## Open the clickable map

Serve the generated map locally so background tile requests have an HTTP referrer:

```bash
python3.12 -m http.server 8000 --bind 127.0.0.1 --directory data/processed
```

Open http://127.0.0.1:8000/uppsala_population_map.html in your browser.
It shows all imported 2025 population cells with a population colour scale.
Use “Heat map shows” to switch between residents, restaurant counts and
restaurants per resident. All modes use light-to-dark red, with metric-specific
legend ranges; grey indicates unavailable values. Changing the metric preserves
the selected cell and restaurant markers.
Click a cell for total population, sex counts, age groups and border overlap.
Cell details, hover tooltips and popups also show restaurant counts based on
registered address points inside that cell. Distinct CFAR workplaces sharing an
address count separately. Missing coordinates and points outside the displayed
grid are excluded. A point exactly on a shared edge is assigned once, to the
lowest cell ID. Counts use the current restaurant import, not the population's
2025 reference date, and are recalculated whenever the map is rebuilt.
Each cell also shows restaurants per resident: restaurant count divided by its
2025 population, displayed to four significant digits. Zero or missing population
produces “Not available”; zero restaurants with positive population produces 0.
You can also select a cell using the dropdown, pan/zoom, or choose “Show all cells”.

Orange markers show restaurant workplaces. Click one for its name, address and
CFAR ID; shared locations list all restaurants there. Use the restaurant dropdown
to select a specific workplace, or the checkbox to hide/show the markers.
Restaurants without address coordinates remain in the list but have no marker.
Property-centre coordinates are not substituted for address locations.

Rebuild after refreshing either database (requires the `geo` optional dependencies):

```bash
PYTHONPATH=src .venv/bin/python -m accessibility.population_map
```

The map embeds both datasets and Leaflet 1.9.4; no API key is embedded or needed to view it.
An internet connection is required only for the OpenStreetMap background tiles;
the clickable grid still works without them. The output is a snapshot: rerun
the command after importing newer data. `--database`, `--restaurants-database` and `--output` accept custom
paths. Leaflet's license is included in `src/accessibility/map_assets/LEAFLET-LICENSE`.

## SCB restaurant workplaces

SCB's [business-register API](https://www.scb.se/vara-tjanster/bestall-data-och-statistik/foretagsregistret/avgiftsfria-uppgifter-i-foretagsregistret/)
requires a personal API key from its [registration portal](https://registreraafr.scb.se/).
The importer reads `SCB_FDB_API_KEY` from the environment and sends it only in
the `X-API-Key` header. Set it in your terminal without putting it in shell history:

```bash
read -rsp 'SCB API key: ' SCB_FDB_API_KEY
echo
export SCB_FDB_API_KEY
PYTHONPATH=src .venv/bin/python -m accessibility.restaurants
unset SCB_FDB_API_KEY
```

This creates `data/processed/restaurants.sqlite3` after a complete successful
download. `--database PATH` selects another database. It does not load `.env`
files automatically. No real restaurant records are included until the importer
has run with a valid key.

Selection: active workplaces (`aeStat=1`) located in Uppsala kommun (`0380`)
with **primary** SNI 2025 code `56110` (restaurant activities). It includes all
matching records returned by SCB, not every possible dining venue: restaurants
with another primary industry, mobile food service (`56120`), catering and
bars with other codes are outside this selection. Registered workplace names
may differ from customer-facing restaurant names. If `ben` is missing or blank,
the importer looks up the linked company's `foretagsnamn` and uses it in the
`name` column. Company lookups are cached within the import, so shared companies
are fetched once. If neither name is available, the label remains
`Unnamed restaurant (CFAR <identifier>)`. The legal-name field `namn` is not
used as an additional fallback. Register status is not a
guarantee a venue is currently open to customers.

The importer traverses every municipality page using SCB's cursor (500 workplaces
per page; configurable with `--page-size` between 1 and 5000), filters the
records, then requests full details for each match. Calls are sequential and
limited to four per second. Interrupted connections, timeouts and HTTP
502/503/504 responses are retried up to three times, after 1, 2 and 4 seconds.
Other HTTP errors (including authentication and rate-limit errors) stop the run.
Failed requests, invalid results and empty imports
leave any existing restaurant records unchanged. A successful refresh replaces
the previous matching selection atomically, removing stale records.

`restaurants` stores CFAR workplace ID, name, physical address, municipality,
primary SNI, employee size-class code, separate address/property coordinates in
SWEREF99 TM (EPSG:3006), source URL and retrieval time. Missing/zero coordinate
pairs become SQL NULL; property coordinates are not silently used as entrance
locations. `restaurant_imports` logs successful imports and row counts.
API keys, personal/organisation numbers and contact details are not stored.
For unexplained API failures, run
`PYTHONPATH=src .venv/bin/python -m accessibility.restaurants --diagnose`.
This compares four read-only API requests and prints their outcomes without
printing restaurant records or the API key; it does not modify the database.
Rebuild the map after importing to update its restaurant markers.

## Project directories

- `data/raw/`: Original input data.
- `data/processed/`: Processed datasets.
- `src/accessibility/`: Python package source code.
- `tests/`: Automated tests.
- `notebooks/`: Exploratory notebooks.
- `docs/`: Project documentation.

## Uppsala population database

Requires Python 3.10+; no third-party runtime dependencies. From the project root:

```bash
PYTHONPATH=src python3.12 -m accessibility.population
```

This downloads Uppsala kommun's annual population totals from
[SCB PxWebApi v2](https://www.scb.se/vara-tjanster/oppna-data/pxwebapi/pxwebapi-v2/)
table `TAB5557` into `data/processed/population.sqlite3`. Use `--database PATH`
to choose another SQLite file. After `pip install -e .`, the equivalent command
is `import-uppsala-population`.

The table currently starts in 2025. Each observation is the population on
31 December, all ages, sexes and marital statuses combined. The importer uses
SCB's published total rather than summing demographic categories. SCB applies
disclosure protection from 2025; the complete response, including its notes,
is retained in `scb_sources`. Earlier historical years are not imported.

`municipality_population` stores the municipality code as text (`0380`), name,
year, population, source table/URL, source update time and retrieval time.
Rerunning the import updates existing municipality/year rows without duplicates.
Missing or invalid counts abort the import before database writes.

Read the latest observation:

```python
import sqlite3

with sqlite3.connect("data/processed/population.sqlite3") as db:
    latest = db.execute("""
        SELECT municipality_code, municipality_name, year, population
        FROM municipality_population
        WHERE municipality_code = ?
        ORDER BY year DESC LIMIT 1
    """, ("0380",)).fetchone()
    print(latest)
```

Join `municipality_code` to municipality boundary data for mapping. This is a
municipality total, not a population grid or neighbourhood dataset; geometries
are not included.

Run the offline tests (the fixture is a saved SCB response):

```bash
PYTHONPATH=src python3.12 -m unittest discover -s tests -v
```

## Uppsala population grid (2025)

Install the optional geographic dependencies and run the importer:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[geo]'
PYTHONPATH=src .venv/bin/python -m accessibility.population_grid
```

This adds `population_grid` and `population_grid_sources` to the same SQLite
database, preserving the municipality totals. It also exports
`data/processed/uppsala_population_grid_2025.geojson`, a FeatureCollection with
longitude/latitude polygons (EPSG:4326) for use in Leaflet, MapLibre or GIS tools.
Use `--database PATH` and `--output PATH` to override the output locations.

The source is SCB's [2025 population grid](https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/statistik-pa-rutor/),
fetched through WFS with pagination. The importer unions SCB's 2025 DeSO areas
with municipality code `0380` to obtain the selection boundary, then selects
1 km cells with positive area overlap. Spatial calculations use SWEREF99 TM
(EPSG:3006); exported geometry uses EPSG:4326.

Each database row has a cell ID, year, population, overlap fraction, GeoJSON
geometry, and all original SCB attributes as JSON. Attributes include `man`,
`kvinna`, `ald0_4` through `ald95_99`, `ald100w`, and `referenstid` (20251231).
Null counts remain null. Source request URLs, retrieval time and the selection
boundary are retained in `population_grid_sources`. Reruns replace the selected
municipality/year's cells in a transaction, so duplicate or obsolete rows do not
accumulate. Both download and validation finish before database writes.

**Border cells retain their full geometry and population**, including the portion
outside Uppsala. `overlap_fraction` describes the area inside the municipality;
it is not a population weight. Consequently the sum is not the official municipal
total. SCB disclosure protection also means age/sex subtotals may not add up.
Only cells returned by SCB are included; missing cells are not filled with zeros.

Run all tests with geographic dependencies installed:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```
