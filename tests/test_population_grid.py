import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from shapely.geometry import box, mapping

from accessibility.population_grid import fetch_features, save_grid, select_grid


def cell(identifier, geometry, population=100):
    return {"type": "Feature", "id": identifier, "geometry": mapping(geometry),
            "properties": {"rutid_scb": identifier, "rutstorl": 1000,
                           "referenstid": "20251231", "beftotalt": population,
                           "man": None, "kvinna": 50, "ald0_4": 3}}


class GridTests(unittest.TestCase):
    def setUp(self):
        self.boundary = box(650000, 6630000, 651500, 6631000)
        self.cells = [cell("inside", box(650000, 6630000, 651000, 6631000)),
                      cell("border", box(651000, 6630000, 652000, 6631000)),
                      cell("outside", box(652000, 6630000, 653000, 6631000))]

    def test_overlap_keeps_whole_population_and_transforms_coordinates(self):
        result = select_grid(self.cells, self.boundary)["features"]
        self.assertEqual([f["id"] for f in result], ["inside", "border"])
        self.assertEqual(result[1]["properties"]["overlap_fraction"], 0.5)
        self.assertEqual(result[1]["properties"]["population"], 100)
        self.assertIsNone(result[0]["properties"]["man"])
        lon, lat = result[0]["geometry"]["coordinates"][0][0]
        self.assertTrue(17 < lon < 19 and 59 < lat < 61)

    def test_touching_edge_is_excluded(self):
        boundary = box(650000, 6630000, 651000, 6631000)
        self.assertEqual(len(select_grid(self.cells, boundary)["features"]), 1)

    def test_wrong_year_and_duplicate_cells_rejected(self):
        with self.assertRaises(ValueError):
            select_grid(self.cells + [self.cells[0]], self.boundary)
        self.cells[0]["properties"]["referenstid"] = "20241231"
        with self.assertRaises(ValueError):
            select_grid(self.cells, self.boundary)

    def test_repeat_import_preserves_other_tables_and_removes_stale_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "population.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE municipality_population (population INTEGER)")
                db.execute("INSERT INTO municipality_population VALUES (249726)")
            collection = select_grid(self.cells, self.boundary)
            save_grid(database, collection, self.boundary, ["https://example.test"])
            collection["features"] = collection["features"][:1]
            save_grid(database, collection, self.boundary, ["https://example.test"])
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM population_grid").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT population FROM municipality_population").fetchone()[0], 249726)

    def test_pagination_detects_missing_page(self):
        page = {"crs": {"properties": {"name": "EPSG:3006"}},
                "numberMatched": 2, "features": [self.cells[0]]}
        empty = dict(page, features=[])
        import io
        with patch("accessibility.population_grid.urlopen", side_effect=[
            io.BytesIO(json.dumps(page).encode()), io.BytesIO(json.dumps(empty).encode())
        ]):
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                fetch_features("test")


if __name__ == "__main__":
    unittest.main()
