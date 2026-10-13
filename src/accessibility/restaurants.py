"""Import active Uppsala workplaces with primary restaurant activity from SCB AFR."""

import argparse
from html import unescape
from http.client import IncompleteRead, RemoteDisconnected
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://apiafr.scb.se/v1"
DATABASE = Path("data/processed/restaurants.sqlite3")
SNI = "56110"
MUNICIPALITY = "0380"


def request_context(path):
    if path.startswith("/juridiskaenheter/"):
        return "company-name lookup"
    if path.startswith("/arbetsstallen/kommun/"):
        return "municipality workplace list"
    return "workplace details"


def problem_summary(error, key):
    """Read only bounded diagnostic fields, redacting credentials and identifiers."""
    try:
        raw = error.read(16384).decode("utf-8", errors="replace")
        try:
            problem = json.loads(raw)
        except ValueError:
            # Gateways may send HTML instead of SCB ProblemDetails. Only expose
            # the page title, not arbitrary HTML, echoed headers or request data.
            title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
            problem = {"title": unescape(title.group(1))} if title else {}
        if not isinstance(problem, dict):
            return ""
        fields = [problem.get("title"), problem.get("detail")]
        if isinstance(problem.get("errors"), dict):
            fields.append(json.dumps(problem["errors"], ensure_ascii=False))
        summary = " | ".join(field for field in fields if isinstance(field, str))
        summary = summary.replace(key, "[redacted]")
        summary = re.sub(r"\b\d{6,12}[-+]?\d{4}\b|\b\d{8,12}\b", "[identifier]", summary)
        return " ".join(summary.split())[:800]
    except (OSError, ValueError):
        return ""


def diagnose(client):
    """Read-only, small probes; do not print records, keys, URLs or identifiers."""
    probes = [
        ("Uppsala workplace count", "/arbetsstallen/kommun/0380/count"),
        ("Uppsala list, one record", "/arbetsstallen/kommun/0380?limit=1"),
        ("Uppsala list, default limit", "/arbetsstallen/kommun/0380"),
        ("Unfiltered list, one record", "/arbetsstallen?limit=1"),
    ]
    for label, path in probes:
        try:
            client.get(path)
        except (OSError, ValueError, RuntimeError) as error:
            # Client errors already contain only sanitised diagnostic fields.
            if isinstance(error, RuntimeError):
                print(f"{label}: {error}")
            else:
                print(f"{label}: transport error or invalid JSON response")
        else:
            print(f"{label}: OK")


class Client:
    def __init__(self, key):
        if not key or not key.strip():
            raise ValueError("Set SCB_FDB_API_KEY to your personal SCB API key first.")
        self.key = key.strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in self.key):
            raise ValueError("SCB_FDB_API_KEY contains whitespace or non-ASCII characters; re-enter the key.")
        self.last_request = 0.0

    def get(self, path):
        request = Request(BASE + path, headers={"X-API-Key": self.key,
                          "Accept": "application/json"})
        for attempt in range(4):
            # Apply the rate limit to retries as well as initial requests.
            time.sleep(max(0, .25 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with urlopen(request, timeout=60) as response:
                    return json.load(response)
            except HTTPError as error:
                code = error.code
                detail = problem_summary(error, self.key) if code == 400 else ""
                if code == 400 and not detail:
                    detail = "No readable error explanation was returned."
                error.close()
                if code not in (502, 503, 504):
                    raise RuntimeError(
                        f"SCB request failed (HTTP {code}) during {request_context(path)}; "
                        f"database unchanged." + (f" SCB explanation: {detail}" if detail else "")) from None
                reason = f"HTTP {code}"
            except (RemoteDisconnected, IncompleteRead, URLError,
                    ConnectionError, TimeoutError):
                reason = "connection interrupted or timed out"
            if attempt == 3:
                raise RuntimeError(
                    f"SCB request failed after 4 attempts ({reason}); database unchanged. "
                    "Try the import again later.") from None
            delay = 2 ** attempt
            # Never print credentials, headers or remote exception bodies.
            print(f"SCB {reason}; retrying request in {delay}s "
                  f"(attempt {attempt + 2}/4)...", file=sys.stderr)
            time.sleep(delay)


def cfar_id(value):
    if type(value) not in (str, int) or not str(value).isdigit() or not 0 < int(value) < 100000000:
        raise ValueError("Invalid CFAR workplace identifier")
    return str(value).zfill(8)


def fetch_restaurants(client, page_size=500):
    """Traverse every municipality page, then fetch full details for matches."""
    cursor, cursors, seen, candidates = None, set(), set(), []
    if type(page_size) is not int or not 1 <= page_size <= 5000:
        raise ValueError("Page size must be between 1 and 5000")
    page_number = 1
    while True:
        params = {"limit": page_size}
        if cursor is not None:
            params["cursorId"] = cursor
        try:
            page = client.get(f"/arbetsstallen/kommun/{MUNICIPALITY}?" + urlencode(params))
        except RuntimeError as error:
            stage = "initial request, no cursor" if cursor is None else "continuation request"
            raise RuntimeError(f"{error} List page {page_number}, limit={page_size} ({stage}).") from None
        workplaces = page["arbetsstallen"]
        pagination = page["pagination"]
        if not isinstance(workplaces, list) or type(pagination["hasMore"]) is not bool:
            raise ValueError("Unexpected SCB page format")
        for workplace in workplaces:
            cfar = cfar_id(workplace["cfarNr"])
            if cfar in seen:
                raise ValueError("Duplicate workplace across SCB pages")
            seen.add(cfar)
            if workplace["belagenhetsadress"]["kommun"] != MUNICIPALITY:
                raise ValueError("SCB returned a workplace outside Uppsala")
            if (str(workplace["aeStat"]) == "1"
                    and workplace["primarNaringsgren"]["naringsgren"] == SNI):
                candidates.append(cfar)
        if not pagination["hasMore"]:
            break
        cursor = pagination["nextCursorId"]
        if not workplaces or type(cursor) is not int or cursor <= 0 or cursor in cursors:
            raise ValueError("SCB pagination did not advance")
        cursors.add(cursor)
        page_number += 1
    records = []
    company_names = {}
    for cfar in candidates:
        record = client.get(f"/arbetsstallen/{int(cfar)}/full")
        if cfar_id(record["cfarNr"]) != cfar:
            raise ValueError("SCB returned the wrong workplace")
        validate_record(record)
        if not (record.get("ben") or "").strip():
            company_id = record.get("peOrgNr")
            if company_id:
                if not isinstance(company_id, str) or len(company_id) != 12 or not company_id.isdecimal():
                    raise ValueError("Invalid company identifier for name lookup")
                if company_id not in company_names:
                    company = client.get(f"/juridiskaenheter/{company_id}/full")
                    if company.get("peOrgNr") != company_id:
                        raise ValueError("SCB returned the wrong company for name lookup")
                    name = company.get("foretagsnamn")
                    if name is not None and not isinstance(name, str):
                        raise ValueError("Invalid company name type")
                    company_names[company_id] = name
                record = dict(record, foretagsnamn=company_names[company_id])
        records.append(record)
    if not records:
        raise ValueError("No restaurants returned; refusing to replace an existing import.")
    return records


def validate_record(record):
    cfar_id(record["cfarNr"])
    if (record["belagenhetsadress"]["kommun"] != MUNICIPALITY
            or str(record["aeStat"]) != "1"
            or not any(g["rangordning"] == 1 and g["naringsgren"] == SNI
                       for g in record["naringsgrenar"])):
        raise ValueError("Workplace does not match the active Uppsala restaurant selection")
    if record.get("ben") is not None and not isinstance(record["ben"], str):
        raise ValueError("Invalid workplace name type")
    if record.get("foretagsnamn") is not None and not isinstance(record["foretagsnamn"], str):
        raise ValueError("Invalid company name type")


def workplace_name(record):
    """Prefer workplace name, then registered company name, then a clear label."""
    for key in ("ben", "foretagsnamn"):
        name = record.get(key)
        if name and name.strip():
            return name.strip()
    return f"Unnamed restaurant (CFAR {cfar_id(record['cfarNr'])})"


def coordinate_pair(geo, north, east):
    n, e = geo.get(north), geo.get(east)
    if n in (None, 0) or e in (None, 0):
        return None, None
    if type(n) is not int or type(e) is not int or not (6000000 <= n <= 8000000 and 100000 <= e <= 1000000):
        raise ValueError("Invalid SWEREF99 coordinate pair")
    return n, e


def save_restaurants(records, database=DATABASE):
    if not records:
        raise ValueError("No restaurants to import")
    retrieved = datetime.now(timezone.utc).isoformat()
    rows, seen = [], set()
    for record in records:
        validate_record(record)
        cfar = cfar_id(record["cfarNr"])
        if cfar in seen:
            raise ValueError("Duplicate restaurant")
        seen.add(cfar)
        address = record["belagenhetsadress"]
        geo = record.get("geografiskInformation") or {}
        n, e = coordinate_pair(geo, "nordKoordinatAdressSW", "ostKoordinatAdressSW")
        pn, pe = coordinate_pair(geo, "nordKoordinatSW", "ostKoordinatSW")
        rows.append((cfar, MUNICIPALITY, workplace_name(record), SNI,
                     address["bGatuAdress"], address["bPostNr"], address["bPostOrt"],
                     str(record["anstKl"]), n, e, pn, pe,
                     BASE + f"/arbetsstallen/{int(cfar)}/full", retrieved))
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS restaurants (
                cfar_number TEXT PRIMARY KEY, municipality_code TEXT NOT NULL,
                name TEXT NOT NULL, primary_sni TEXT NOT NULL,
                street_address TEXT, postal_code TEXT, postal_locality TEXT,
                employee_size_class TEXT,
                address_northing INTEGER, address_easting INTEGER,
                property_northing INTEGER, property_easting INTEGER,
                source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS restaurant_imports (
                id INTEGER PRIMARY KEY, retrieved_at TEXT NOT NULL,
                municipality_code TEXT NOT NULL, primary_sni TEXT NOT NULL,
                restaurant_count INTEGER NOT NULL, source_url TEXT NOT NULL)""")
            connection.execute("DELETE FROM restaurants WHERE municipality_code=? AND primary_sni=?",
                               (MUNICIPALITY, SNI))
            connection.executemany("INSERT INTO restaurants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            connection.execute("""INSERT INTO restaurant_imports
                (retrieved_at,municipality_code,primary_sni,restaurant_count,source_url)
                VALUES (?,?,?,?,?)""", (retrieved, MUNICIPALITY, SNI, len(rows), BASE))
    finally:
        connection.close()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--page-size", type=int, default=500,
                        help="Workplaces per list request (1–5000; default: 500)")
    parser.add_argument("--diagnose", action="store_true",
                        help="Compare small API requests without modifying the database")
    args = parser.parse_args()
    try:
        client = Client(os.environ.get("SCB_FDB_API_KEY"))
        if args.diagnose:
            diagnose(client)
            return
        records = fetch_restaurants(client, page_size=args.page_size)
        count = save_restaurants(records, args.database)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"Restaurant import failed: {error}\n")
    print(f"Imported {count} active restaurant workplaces into {args.database}")


if __name__ == "__main__":
    main()
