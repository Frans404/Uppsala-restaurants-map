"""Import SCB's published annual Uppsala population totals into SQLite."""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

TABLE = "TAB5557"
SELECTION = {
    "Region": "0380",
    "Civilstand": "SC",
    "Alder": "TotSA",
    "Kon": "TotSa",
    "ContentsCode": "000007ME",
}
SOURCE_URL = (
    f"https://statistikdatabasen.scb.se/api/v2/tables/{TABLE}/data?"
    + urlencode({"lang": "en", "outputFormat": "json-stat2",
                 **{f"valueCodes[{key}]": value for key, value in SELECTION.items()},
                 "valueCodes[Tid]": "*"})
)
DEFAULT_DB = Path("data/processed/population.sqlite3")


def parse_population(dataset):
    """Validate the selected totals; never turn missing observations into zero."""
    dimensions = dataset["dimension"]
    ids = dataset["id"]
    if len(ids) != 6 or set(ids) != {*SELECTION, "Tid"}:
        raise ValueError("Unexpected SCB dimensions")
    for key, expected in SELECTION.items():
        if dimensions[key]["category"]["index"] != {expected: 0}:
            raise ValueError(f"Unexpected selection for {key}")
    index = dimensions["Tid"]["category"]["index"]
    years = sorted(index, key=index.get)
    if not years or [index[y] for y in years] != list(range(len(years))):
        raise ValueError("Invalid SCB year index")
    if dataset["size"] != [len(years) if key == "Tid" else 1 for key in ids]:
        raise ValueError("Unexpected SCB dataset shape")
    values = dataset["value"]
    if isinstance(values, list) and len(values) != len(years):
        raise ValueError("Unexpected number of SCB values")
    rows = []
    for i, year in enumerate(years):
        value = values.get(str(i)) if isinstance(values, dict) else values[i]
        if not year.isdigit() or len(year) != 4:
            raise ValueError(f"Invalid annual period: {year}")
        if type(value) not in (int, float) or value < 0 or value != int(value):
            raise ValueError(f"Missing or invalid population for {year}: {value!r}")
        rows.append((int(year), int(value)))
    return rows


def import_population(database=DEFAULT_DB, dataset=None):
    """Fetch and atomically upsert all annual totals available in TAB5557."""
    if dataset is None:
        with urlopen(SOURCE_URL, timeout=45) as response:
            dataset = json.load(response)
    rows = parse_population(dataset)
    retrieved = datetime.now(timezone.utc).isoformat()
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS municipality_population (
                    municipality_code TEXT NOT NULL,
                    municipality_name TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    population INTEGER NOT NULL CHECK(population >= 0),
                    source_table TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_updated_at TEXT,
                    retrieved_at TEXT NOT NULL,
                    PRIMARY KEY (municipality_code, year)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS scb_sources (
                    source_table TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    response_json TEXT NOT NULL
                )
            """)
            connection.executemany("""
                INSERT INTO municipality_population VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(municipality_code, year) DO UPDATE SET
                    municipality_name=excluded.municipality_name,
                    population=excluded.population,
                    source_table=excluded.source_table,
                    source_url=excluded.source_url,
                    source_updated_at=excluded.source_updated_at,
                    retrieved_at=excluded.retrieved_at
            """, [("0380", "Uppsala", year, count, TABLE, SOURCE_URL,
                   dataset.get("updated"), retrieved) for year, count in rows])
            connection.execute("""
                INSERT INTO scb_sources VALUES (?, ?, ?, ?)
                ON CONFLICT(source_table) DO UPDATE SET
                    source_url=excluded.source_url,
                    retrieved_at=excluded.retrieved_at,
                    response_json=excluded.response_json
            """, (TABLE, SOURCE_URL, retrieved, json.dumps(dataset, ensure_ascii=False)))
    finally:
        connection.close()
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    try:
        rows = import_population(args.database)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(1, f"Population import failed: {error}\n")
    year, count = max(rows)
    print(f"Imported {len(rows)} annual total(s) into {args.database}; "
          f"latest: Uppsala (0380), {year}: {count:,} people")


if __name__ == "__main__":
    main()
