from pathlib import Path
import sqlite3
import tempfile
import unittest

from accessibility.population_map import load_restaurants
from accessibility.restaurants import save_restaurants
from test_restaurants import restaurant


class RestaurantMapTests(unittest.TestCase):
    def test_address_projection_and_missing_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'restaurants.sqlite3'
            located = restaurant()
            missing = restaurant(22222222)
            missing['geografiskInformation'] = {
                'nordKoordinatSW': 6639000, 'ostKoordinatSW': 647000}
            save_restaurants([located, missing], database)
            rows = {r['cfar_number']: r for r in load_restaurants(database)}
            lat, lon = rows['12345678']['coordinates']
            self.assertTrue(59 < lat < 61 and 17 < lon < 19)
            self.assertIsNone(rows['22222222']['coordinates'])
            self.assertEqual(rows['12345678']['name'], 'Test restaurant')
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM restaurants').fetchone()[0], 2)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                load_restaurants(database)
            self.assertFalse(database.exists())
