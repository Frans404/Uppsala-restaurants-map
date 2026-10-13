"""Import SCB's 2025 population grid intersecting Uppsala kommun."""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from pyproj import Transformer
from shapely import transform
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

from accessibility.population import DEFAULT_DB

WFS = "https://geodata.scb.se/geoserver/stat/wfs"
GRID = "stat:befolkning_1km_2025"
BOUNDARIES = "stat:DeSO_2025"
EXPORT = Path("data/processed/uppsala_population_grid_2025.geojson")


def fetch_features(layer, **filters):
    """Page WFS results deterministically; fail on incomplete responses."""
    features, urls, seen = [], [], set()
    while True:
        url = WFS + "?" + urlencode({
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": layer, "outputFormat": "application/json",
            "srsName": "EPSG:3006", "count": 1000,
            "startIndex": len(features), "sortBy": "objectid", **filters,
        })
        with urlopen(url, timeout=60) as response:
            page = json.load(response)
        if not page.get("crs", {}).get("properties", {}).get("name", "").endswith(":3006"):
            raise ValueError("Expected SWEREF99 TM coordinates")
        batch = page["features"]
        total = int(page["numberMatched"])
        for feature in batch:
            if feature["id"] in seen:
                raise ValueError("Duplicate WFS feature; pagination failed")
            seen.add(feature["id"])
        features.extend(batch)
        urls.append(url)
        if len(features) == total:
            if not features:
                raise ValueError(f"No features returned for {layer}")
            return features, urls
        if not batch or len(features) > total:
            raise ValueError("Incomplete WFS response")


def municipality_boundary(features):
    if not features or any(f["properties"]["kommunkod"] != "0380" for f in features):
        raise ValueError("Expected Uppsala DeSO boundaries only")
    boundary = unary_union([shape(f["geometry"]) for f in features])
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Invalid municipality boundary")
    return boundary


def select_grid(features, boundary):
    """Keep full cells with positive overlap; never prorate population counts."""
    project = Transformer.from_crs(3006, 4326, always_xy=True).transform
    selected, seen = [], set()
    for feature in features:
        geometry = shape(feature["geometry"])
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type != "Polygon":
            raise ValueError("Invalid grid polygon")
        overlap = geometry.intersection(boundary).area
        if overlap <= 0:
            continue
        properties = dict(feature["properties"])
        cell = properties["rutid_scb"]
        if not cell or cell in seen:
            raise ValueError("Missing or duplicate grid identifier")
        seen.add(cell)
        if properties["rutstorl"] != 1000 or properties["referenstid"] != "20251231":
            raise ValueError("Unexpected grid resolution or reference date")
        for key, value in properties.items():
            if key in ("beftotalt", "man", "kvinna") or key.startswith("ald"):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError(f"Invalid count in {key}")
        properties.update(municipality_code="0380", year=2025,
                          population=properties["beftotalt"],
                          overlap_fraction=min(1.0, overlap / geometry.area))
        selected.append({"type": "Feature", "id": cell,
                         "geometry": mapping(transform(geometry, project, interleaved=False)),
                         "properties": properties})
    if not selected:
        raise ValueError("No population grid cells overlap Uppsala")
    return {"type": "FeatureCollection", "features": selected}


def save_grid(database, collection, boundary, source_urls):
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS population_grid (
                municipality_code TEXT NOT NULL, year INTEGER NOT NULL,
                cell_id TEXT NOT NULL, population INTEGER CHECK(population >= 0),
                overlap_fraction REAL NOT NULL,
                geometry_geojson TEXT NOT NULL, properties_json TEXT NOT NULL,
                PRIMARY KEY (municipality_code, year, cell_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS population_grid_sources (
                municipality_code TEXT NOT NULL, year INTEGER NOT NULL,
                source_layer TEXT NOT NULL, boundary_layer TEXT NOT NULL,
                source_urls_json TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                boundary_epsg3006_geojson TEXT NOT NULL,
                PRIMARY KEY (municipality_code, year))""")
            connection.execute("DELETE FROM population_grid WHERE municipality_code=? AND year=?",
                               ("0380", 2025))
            connection.executemany("INSERT INTO population_grid VALUES (?,?,?,?,?,?,?)", [
                ("0380", 2025, f["id"], f["properties"]["population"],
                 f["properties"]["overlap_fraction"], json.dumps(f["geometry"]),
                 json.dumps(f["properties"])) for f in collection["features"]])
            connection.execute("""INSERT INTO population_grid_sources VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(municipality_code,year) DO UPDATE SET
                source_layer=excluded.source_layer, boundary_layer=excluded.boundary_layer,
                source_urls_json=excluded.source_urls_json, retrieved_at=excluded.retrieved_at,
                boundary_epsg3006_geojson=excluded.boundary_epsg3006_geojson""",
                ("0380", 2025, GRID, BOUNDARIES, json.dumps(source_urls), retrieved,
                 json.dumps(mapping(boundary))))
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=EXPORT)
    args = parser.parse_args()
    try:
        areas, urls = fetch_features(BOUNDARIES, CQL_FILTER="kommunkod='0380'")
        boundary = municipality_boundary(areas)
        west, south, east, north = boundary.bounds
        # WFS 2's formal EPSG:3006 axis order is northing, easting.
        cells, grid_urls = fetch_features(
            GRID, bbox=f"{south},{west},{north},{east},urn:ogc:def:crs:EPSG::3006")
        collection = select_grid(cells, boundary)
        save_grid(args.database, collection, boundary, urls + grid_urls)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".geojson.tmp")
        temporary.write_text(json.dumps(collection), encoding="utf-8")
        temporary.replace(args.output)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(1, f"Grid import failed: {error}\n")
    print(f"Imported {len(collection['features'])} grid cells into {args.database}; "
          f"map layer: {args.output}")


if __name__ == "__main__":
    main()
