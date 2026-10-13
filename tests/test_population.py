import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from accessibility.population import import_population, parse_population


class PopulationTests(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(
            (Path(__file__).parent / "fixtures/scb_uppsala_population.json").read_text()
        )

    def test_live_response(self):
        self.assertEqual(parse_population(self.dataset), [(2025, 249726)])

    def test_year_order_and_sparse_values(self):
        self.dataset["dimension"]["Tid"]["category"]["index"] = {"2025": 1, "2024": 0}
        self.dataset["size"][-1] = 2
        self.dataset["value"] = {"0": 100, "1": 200}
        self.assertEqual(parse_population(self.dataset), [(2024, 100), (2025, 200)])

    def test_missing_invalid_or_wrong_region_rejected(self):
        for value in (None, -1, 1.5, True, "249726"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.dataset["value"] = [value]
                parse_population(self.dataset)
        self.dataset["dimension"]["Region"]["category"]["index"] = {"03": 0}
        with self.assertRaises(ValueError):
            parse_population(self.dataset)

    def test_upsert_and_failed_import_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "population.sqlite3"
            import_population(database, self.dataset)
            revised = copy.deepcopy(self.dataset)
            revised["value"] = [249727]
            import_population(database, revised)
            revised["value"] = [None]
            with self.assertRaises(ValueError):
                import_population(database, revised)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT municipality_code, year, population FROM municipality_population"
                ).fetchall(), [("0380", 2025, 249727)])
                saved = json.loads(connection.execute(
                    "SELECT response_json FROM scb_sources"
                ).fetchone()[0])
                self.assertEqual(saved["note"], self.dataset["note"])
                self.assertEqual(saved["value"], [249727])

    def test_network_failure_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "population.sqlite3"
            with patch("accessibility.population.urlopen", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    import_population(database)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
