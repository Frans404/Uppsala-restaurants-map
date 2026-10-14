from pathlib import Path
import sqlite3
import tempfile
import unittest

from accessibility.population_map import load_restaurants, count_restaurants
from accessibility.restaurants import save_restaurants
from test_restaurants import restaurant


class RestaurantMapTests(unittest.TestCase):
    def test_restaurants_per_resident(self):
        from shapely.geometry import box, mapping
        features = [{"geometry": mapping(box(x, 0, x+1, 1)),
                     "properties": {"cell_id": str(x), "population": population}}
                    for x, population in enumerate([100, 0, None, 200])]
        restaurants = [{"cfar_number": str(i), "coordinates": [.5, x]}
                       for i, x in enumerate([.5, .5, 1.5, 2.5])]
        count_restaurants(features, restaurants)
        self.assertEqual([f['properties']['restaurants_per_resident'] for f in features],
                         [.02, None, None, 0])

    def test_spatial_counts_shared_addresses_edges_and_missing_points(self):
        from shapely.geometry import box, mapping
        features = [{"geometry": mapping(box(x, 0, x+1, 1)),
                     "properties": {"cell_id": str(x)}} for x in range(3)]
        restaurants = [{"cfar_number": str(i), "coordinates": point}
                       for i, point in enumerate([
                           [.5, .5], [.5, .5], [.5, 1], [.5, 1.5],
                           None, [5, 5]])]
        count_restaurants(features, restaurants)
        self.assertEqual([f['properties']['restaurant_count'] for f in features], [3, 1, 0])
        count_restaurants(features, restaurants)
        self.assertEqual(sum(f['properties']['restaurant_count'] for f in features), 4)

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
