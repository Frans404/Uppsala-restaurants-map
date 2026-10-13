import copy
from http.client import RemoteDisconnected, IncompleteRead
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from accessibility.restaurants import Client, fetch_restaurants, save_restaurants, workplace_name, diagnose


def restaurant(cfar=12345678):
    return {"cfarNr": cfar, "ben": "Test restaurant", "aeStat": 1, "anstKl": 3,
            "belagenhetsadress": {"kommun": "0380", "bGatuAdress": "Testgatan 1",
                                   "bPostNr": "75300", "bPostOrt": "Uppsala"},
            "primarNaringsgren": {"naringsgren": "56110"},
            "naringsgrenar": [{"rangordning": 1, "naringsgren": "56110"}],
            "geografiskInformation": {"nordKoordinatAdressSW": 6639000,
                                       "ostKoordinatAdressSW": 647000}}


class RestaurantTests(unittest.TestCase):
    def test_diagnostic_continues_without_printing_records(self):
        client = Mock()
        client.get.side_effect = [RuntimeError('HTTP 400'), {}, {}, {'secret': 'private record'}]
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            diagnose(client)
        self.assertEqual(client.get.call_count, 4)
        self.assertIn('HTTP 400', output.getvalue())
        self.assertIn('Unfiltered list, one record: OK', output.getvalue())
        self.assertNotIn('private record', output.getvalue())

    def test_invalid_key_characters_rejected_locally(self):
        for key in ('abc def', 'abc\nxyz', 'abc\u200bxyz'):
            with self.assertRaisesRegex(ValueError, 're-enter'):
                Client(key)

    @patch('accessibility.restaurants.time.sleep')
    @patch('accessibility.restaurants.urlopen')
    def test_gateway_title_is_reported_without_body(self, open_url, sleep):
        open_url.side_effect = HTTPError('https://example.test', 400, 'Bad Request', {},
            io.BytesIO(b'<title>Bad Request - Header too long</title><body>private</body>'))
        with self.assertRaises(RuntimeError) as caught:
            Client('test-secret').get('/test')
        self.assertIn('Header too long', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen")
    def test_bad_request_shows_stage_and_redacted_reason(self, open_url, sleep):
        open_url.side_effect = HTTPError("https://example.test", 400, "Bad Request", {},
            io.BytesIO(b'{"title":"Invalid identifier","detail":"165555555555 test-secret"}'))
        with self.assertRaises(RuntimeError) as caught:
            Client("test-secret").get("/juridiskaenheter/165555555555/full")
        message = str(caught.exception)
        self.assertIn("company-name lookup", message)
        self.assertIn("Invalid identifier", message)
        self.assertNotIn("165555555555", message)
        self.assertNotIn("test-secret", message)
        self.assertEqual(open_url.call_count, 1)

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen")
    def test_bad_request_non_json_body_is_not_printed(self, open_url, sleep):
        open_url.side_effect = HTTPError("https://example.test", 400, "Bad Request", {},
            io.BytesIO(b'<html>private server details</html>'))
        with self.assertRaises(RuntimeError) as caught:
            Client("test-secret").get("/arbetsstallen/kommun/0380?limit=5000")
        self.assertIn("municipality workplace list", str(caught.exception))
        self.assertNotIn("private server", str(caught.exception))

    def test_company_fallback_lookup_is_cached_and_saved(self):
        records = [restaurant(), restaurant(22222222)]
        for record in records:
            record.update(ben=" ", peOrgNr="165555555555")
        client = Mock()
        client.get.side_effect = [
            {"arbetsstallen": records, "pagination": {"hasMore": False}},
            records[0], {"peOrgNr": "165555555555", "foretagsnamn": " Example AB "},
            records[1]]
        fetched = fetch_restaurants(client)
        self.assertEqual(client.get.call_count, 4)
        self.assertEqual(client.get.call_args_list[2].args[0],
                         "/juridiskaenheter/165555555555/full")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "restaurants.sqlite3"
            save_restaurants(fetched, db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT name FROM restaurants").fetchall(),
                                 [("Example AB",), ("Example AB",)])

    def test_name_precedence(self):
        record = restaurant()
        record["foretagsnamn"] = "Company name"
        self.assertEqual(workplace_name(record), "Test restaurant")
        record["ben"] = None
        self.assertEqual(workplace_name(record), "Company name")
        for name in (None, "", "  "):
            record["foretagsnamn"] = name
            self.assertEqual(workplace_name(record), "Unnamed restaurant (CFAR 12345678)")

    def test_wrong_company_response_rejected(self):
        record = restaurant()
        record.update(ben="", peOrgNr="165555555555")
        client = Mock()
        client.get.side_effect = [
            {"arbetsstallen": [record], "pagination": {"hasMore": False}}, record,
            {"peOrgNr": "166666666666", "foretagsnamn": "Wrong company"}]
        with self.assertRaisesRegex(ValueError, "wrong company"):
            fetch_restaurants(client)

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen")
    def test_interrupted_requests_retry_same_request(self, open_url, sleep):
        open_url.side_effect = [RemoteDisconnected(), IncompleteRead(b'{'),
                                io.BytesIO(b'{"ok": true}')]
        self.assertEqual(Client("test-key").get("/test"), {"ok": True})
        self.assertEqual(open_url.call_count, 3)
        self.assertEqual(open_url.call_args_list[0], open_url.call_args_list[2])
        sleep.assert_any_call(1)
        sleep.assert_any_call(2)

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen", side_effect=RemoteDisconnected())
    def test_retry_limit(self, open_url, sleep):
        with self.assertRaisesRegex(RuntimeError, "after 4 attempts.*database unchanged"):
            Client("test-key").get("/test")
        self.assertEqual(open_url.call_count, 4)

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen")
    def test_authentication_failure_not_retried(self, open_url, sleep):
        open_url.side_effect = HTTPError("https://example.test", 401, "Unauthorized", {}, None)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            Client("test-key").get("/test")
        self.assertEqual(open_url.call_count, 1)

    @patch("accessibility.restaurants.time.sleep")
    @patch("accessibility.restaurants.urlopen")
    def test_server_failure_retried(self, open_url, sleep):
        open_url.side_effect = [HTTPError("https://example.test", 503, "Unavailable", {}, None),
                                io.BytesIO(b'{}')]
        self.assertEqual(Client("test-key").get("/test"), {})
        self.assertEqual(open_url.call_count, 2)

    def test_missing_names_survive_fetch_and_save(self):
        for name in (None, "", "   ", "missing-field"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                record = restaurant()
                if name == "missing-field":
                    del record["ben"]
                else:
                    record["ben"] = name
                client = Mock()
                client.get.side_effect = [
                    {"arbetsstallen": [record], "pagination": {"hasMore": False}}, record]
                records = fetch_restaurants(client)
                db = Path(directory) / "restaurants.sqlite3"
                save_restaurants(records, db)
                with sqlite3.connect(db) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT name FROM restaurants"
                    ).fetchone()[0], "Unnamed restaurant (CFAR 12345678)")

    def test_invalid_name_type_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            record = restaurant()
            record["ben"] = ["unexpected"]
            with self.assertRaisesRegex(ValueError, "name type"):
                save_restaurants([record], Path(directory) / "restaurants.sqlite3")

    def test_missing_key(self):
        with self.assertRaisesRegex(ValueError, "SCB_FDB_API_KEY"):
            Client(None)

    def test_cursor_pagination_and_activity_filter(self):
        selected = restaurant()
        other = restaurant(22222222)
        other["primarNaringsgren"]["naringsgren"] = "47110"
        client = Mock()
        client.get.side_effect = [
            {"arbetsstallen": [other], "pagination": {"hasMore": True, "nextCursorId": 17}},
            {"arbetsstallen": [selected], "pagination": {"hasMore": False}}, selected]
        self.assertEqual(fetch_restaurants(client), [selected])
        self.assertIn("cursorId=17", client.get.call_args_list[1].args[0])
        for call in client.get.call_args_list[:2]:
            self.assertIn("limit=500", call.args[0])

    def test_list_failure_identifies_first_page(self):
        client = Mock()
        client.get.side_effect = RuntimeError("HTTP 400")
        with self.assertRaisesRegex(RuntimeError, "page 1, limit=500.*no cursor"):
            fetch_restaurants(client)

    def test_list_failure_identifies_later_page(self):
        client = Mock()
        client.get.side_effect = [
            {"arbetsstallen": [restaurant()], "pagination": {"hasMore": True, "nextCursorId": 17}},
            RuntimeError("HTTP 400")]
        with self.assertRaisesRegex(RuntimeError, "page 2, limit=500.*continuation"):
            fetch_restaurants(client)

    def test_invalid_page_size_does_not_request_data(self):
        client = Mock()
        for size in (0, 5001):
            with self.assertRaises(ValueError):
                fetch_restaurants(client, page_size=size)
        client.get.assert_not_called()

    def test_repeated_cursor_rejected(self):
        client = Mock()
        client.get.side_effect = [
            {"arbetsstallen": [restaurant()], "pagination": {"hasMore": True, "nextCursorId": 17}},
            {"arbetsstallen": [restaurant(22222222)], "pagination": {"hasMore": True, "nextCursorId": 17}}]
        with self.assertRaisesRegex(ValueError, "pagination"):
            fetch_restaurants(client)

    def test_refresh_and_validation_preserve_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "restaurants.sqlite3"
            record = restaurant()
            save_restaurants([record, restaurant(22222222)], db)
            record["ben"] = "Updated name"
            save_restaurants([record], db)
            invalid = copy.deepcopy(record)
            invalid["belagenhetsadress"]["kommun"] = "0180"
            with self.assertRaises(ValueError):
                save_restaurants([invalid], db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute(
                    "SELECT cfar_number,name,address_northing,property_northing FROM restaurants"
                ).fetchall(), [("12345678", "Updated name", 6639000, None)])

    def test_zero_coordinates_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "restaurants.sqlite3"
            record = restaurant()
            record["geografiskInformation"]["nordKoordinatAdressSW"] = 0
            save_restaurants([record], db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute(
                    "SELECT address_northing,address_easting FROM restaurants"
                ).fetchone(), (None, None))


if __name__ == "__main__":
    unittest.main()
