"""Build a standalone clickable population map from the local SQLite database."""

import argparse
import json
from pathlib import Path
import sqlite3

from accessibility.population import DEFAULT_DB
from accessibility.restaurants import DATABASE as RESTAURANTS_DB

ASSETS = Path(__file__).with_name("map_assets")
OUTPUT = Path("data/processed/uppsala_population_map.html")


def load_restaurants(database):
    """Use address coordinates only; retain unlocated workplaces in the selector."""
    from pyproj import Transformer
    project = Transformer.from_crs(3006, 4326, always_xy=True)
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        records = [dict(row) for row in db.execute("""SELECT cfar_number, name,
            street_address, postal_code, postal_locality, primary_sni, retrieved_at,
            address_northing, address_easting FROM restaurants
            WHERE municipality_code='0380' ORDER BY name, cfar_number""")]
    for record in records:
        north = record.pop("address_northing")
        east = record.pop("address_easting")
        record["coordinates"] = None
        if north and east:
            lon, lat = project.transform(east, north, errcheck=True)
            record["coordinates"] = [lat, lon]
    return records


def count_restaurants(features, restaurants):
    """Count each located workplace once, including distinct workplaces sharing an address."""
    from shapely.geometry import Point, shape
    from shapely import STRtree
    polygons = [shape(feature["geometry"]) for feature in features]
    tree = STRtree(polygons)
    for feature in features:
        feature["properties"]["restaurant_count"] = 0
    seen = set()
    for restaurant in restaurants:
        identifier = restaurant["cfar_number"]
        if identifier in seen:
            raise ValueError("Duplicate restaurant CFAR identifier")
        seen.add(identifier)
        coordinates = restaurant["coordinates"]
        if coordinates is None:
            continue
        latitude, longitude = coordinates
        matches = tree.query(Point(longitude, latitude), predicate="intersects")
        if len(matches):
            # Shared-edge points go to the lowest cell ID, avoiding double counts.
            index = min(matches, key=lambda i: features[i]["properties"]["cell_id"])
            features[index]["properties"]["restaurant_count"] += 1


def build_map(database=DEFAULT_DB, output=OUTPUT, restaurants_database=RESTAURANTS_DB):
    database, output = Path(database), Path(output)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT cell_id, population, overlap_fraction,
            geometry_geojson, properties_json FROM population_grid
            WHERE municipality_code = '0380' AND year = 2025 ORDER BY cell_id""").fetchall()
    if not rows:
        raise ValueError("No Uppsala grid cells found. Run accessibility.population_grid first.")
    features = []
    for cell, population, overlap, geometry, properties in rows:
        properties = json.loads(properties)
        properties.update(cell_id=cell, population=population, overlap_fraction=overlap)
        features.append({"type": "Feature", "id": cell,
                         "geometry": json.loads(geometry), "properties": properties})
    restaurant_records = load_restaurants(restaurants_database)
    count_restaurants(features, restaurant_records)
    data = json.dumps({"type": "FeatureCollection", "features": features})
    # Prevent source strings from closing the embedded JSON script element.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = (ASSETS / "map.html").read_text(encoding="utf-8")
    restaurants = json.dumps(restaurant_records)
    restaurants = restaurants.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    for token, content in [("/* LEAFLET_CSS */", (ASSETS / "leaflet.css").read_text()),
                           ("/* LEAFLET_JS */", (ASSETS / "leaflet.js").read_text()),
                           ("__GRID_DATA__", data), ("__RESTAURANT_DATA__", restaurants)]:
        html = html.replace(token, content)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return len(features)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--restaurants-database", type=Path, default=RESTAURANTS_DB)
    args = parser.parse_args()
    try:
        count = build_map(args.database, args.output, args.restaurants_database)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Map build failed: {error}\n")
    print(f"Built map with {count} cells. Open {args.output.resolve()} in your browser.")


if __name__ == "__main__":
    main()
