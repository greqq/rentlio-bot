#!/usr/bin/env python3
"""
Rentlio API capability scanner.

Probes the Rentlio API with your key and reports:
  1. Which endpoints exist (200 / 401 / 404 / 405 ...)
  2. Which FIELDS the objects actually expose today (schema drift check) -
     e.g. did an online check-in URL / eVisitor status / guest counter appear?
  3. Which enums are available (and their IDs, needed for guest registration)
  4. Whether webhooks can be managed over the API

Values are redacted by default - only field NAMES, types and shapes are kept,
so the report is safe to paste into a chat or an issue.
Use --raw if you explicitly want values (contains guest PII - handle with care).

Usage:
    python scripts/api_capability_scan.py
    python scripts/api_capability_scan.py --reservation-id 9609866
    python scripts/api_capability_scan.py --raw --out data/scan.json
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config

# Fields the bot already knows about. Anything the API returns that is NOT in
# here gets flagged as "NEW" - that is the interesting part of the report.
KNOWN_RESERVATION_FIELDS = {
    "id", "channelId", "guestName", "guestEmail", "guestContactNumber",
    "arrivalDate", "departureDate", "propertyName", "unitName", "status",
    "checkedIn", "totalNights", "pricePerNight", "otaChannelName",
    "salesChannelName", "origin", "holder",
}
KNOWN_GUEST_FIELDS = {
    "id", "name", "dateOfBirth", "genderId", "email", "contactNumber",
    "countryId", "citizenshipCountryId", "countryOfBirthId",
    "countryOfResidenceId", "cityOfResidence", "documentNumber",
    "travelDocumentTypesId", "arrivalArrangementsId", "providedServicesTypesId",
    "isBooker", "isPrimary", "isAdditional", "note", "address",
}

# Endpoints probed without any ID substitution.
STATIC_ENDPOINTS = [
    # core
    "/properties",
    "/units",
    "/reservations",
    "/guests",
    "/invoices",
    # enums (IDs needed for guest registration / eVisitor)
    "/enums/countries",
    "/enums/genders",
    "/enums/guests/document-types",
    "/enums/guests/arrival-arrangements",
    "/enums/guests/provided-services-types",
    "/enums/guests/payment-categories",
    "/enums/services/payment-types",
    "/enums/invoices/types",
    "/enums/reservations/statuses",
    "/enums/languages",
    # things that would be gold if they exist
    "/webhooks",
    "/webhook-subscriptions",
    "/settings/webhooks",
    "/online-checkin",
    "/self-checkin",
    "/guest-registrations",
    "/tourist-tax",
    "/evisitor",
    "/email-templates",
    "/messages",
    "/channels",
    "/account",
    "/me",
]

# Endpoints probed with {res} = a real reservation id.
RESERVATION_ENDPOINTS = [
    "/reservations/{res}",
    "/reservations/{res}/details",
    "/reservations/{res}/guests",
    "/reservations-guests/{res}",
    "/reservations/{res}/invoices",
    "/reservations/{res}/notes",
    "/reservations/{res}/messages",
    "/reservations/{res}/checkin-url",
    "/reservations/{res}/online-checkin",
    "/reservations/{res}/self-checkin",
    "/reservations/{res}/checkin-link",
    "/reservations/{res}/guest-registration",
    "/reservations/{res}/tourist-tax",
    "/reservations/{res}/evisitor",
    "/reservations/{res}/token",
]

# Endpoints probed with {prop} = a real property id.
PROPERTY_ENDPOINTS = [
    "/properties/{prop}",
    "/properties/{prop}/units",
    "/properties/{prop}/guests/checked-in",
    "/properties/{prop}/rate-plans",
    "/properties/{prop}/settings",
    "/properties/{prop}/webhooks",
]

INTERESTING_SUBSTRINGS = (
    "checkin", "check_in", "checkedin", "online", "url", "link", "token",
    "uuid", "evisitor", "visitor", "tourist", "registration", "guestcount",
    "guests", "adults", "children", "webhook", "fiscal", "status",
)


def shape(value, depth: int = 0):
    """Describe a JSON value by shape/type only - no values."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def collect_keys(value, prefix: str = "", out: set = None) -> set:
    """Flatten every key path in a JSON structure."""
    if out is None:
        out = set()
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            collect_keys(v, path, out)
    elif isinstance(value, list) and value:
        collect_keys(value[0], f"{prefix}[]", out)
    return out


class Scanner:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, raw: bool):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.raw = raw
        self.results: dict = {}

    async def probe(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        entry = {"endpoint": endpoint, "params": params or {}}
        try:
            async with self.session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                entry["status"] = resp.status
                try:
                    body = await resp.json()
                except Exception:
                    body = None
                if resp.status == 200 and body is not None:
                    payload = body.get("data", body) if isinstance(body, dict) else body
                    sample = payload[0] if isinstance(payload, list) and payload else payload
                    entry["count"] = len(payload) if isinstance(payload, list) else None
                    entry["shape"] = shape(sample)
                    entry["keys"] = sorted(collect_keys(sample))
                    if self.raw:
                        entry["sample"] = sample
                elif body is not None:
                    entry["error"] = str(body)[:200]
        except asyncio.TimeoutError:
            entry["status"] = "TIMEOUT"
        except Exception as e:  # noqa: BLE001 - diagnostic script
            entry["status"] = "ERROR"
            entry["error"] = str(e)[:200]
        self.results[endpoint] = entry
        return entry


def print_line(entry: dict):
    status = entry["status"]
    icon = {200: "OK  ", 401: "AUTH", 403: "FORB", 404: "--- ", 405: "M405"}.get(status, "?   ")
    extra = ""
    if status == 200:
        n = entry.get("count")
        extra = f"({n} items)" if n is not None else "(object)"
    elif entry.get("error"):
        extra = entry["error"][:80]
    print(f"  [{icon}] {status:<7} {entry['endpoint']:<48} {extra}")


async def main():
    parser = argparse.ArgumentParser(description="Scan the Rentlio API for capabilities")
    parser.add_argument("--reservation-id", help="Reservation id to probe (auto-detected if omitted)")
    parser.add_argument("--property-id", help="Property id to probe (auto-detected if omitted)")
    parser.add_argument("--raw", action="store_true", help="Include real values (contains guest PII)")
    parser.add_argument("--out", default=None, help="Where to write the JSON report")
    args = parser.parse_args()

    if not config.RENTLIO_API_KEY:
        print("RENTLIO_API_KEY not set - put it in .env first.")
        return 1

    headers = {
        "apikey": config.RENTLIO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        sc = Scanner(session, config.RENTLIO_API_URL, args.raw)

        print(f"\nRentlio API scan - {config.RENTLIO_API_URL}")
        print(f"{datetime.now():%Y-%m-%d %H:%M}\n")

        # --- discover ids -------------------------------------------------
        property_id = args.property_id
        reservation_id = args.reservation_id

        if not property_id:
            async with session.get(f"{sc.base_url}/properties") as r:
                if r.status == 200:
                    data = (await r.json()).get("data", [])
                    if data:
                        property_id = str(data[0].get("id"))

        if not reservation_id:
            today = datetime.now().strftime("%Y-%m-%d")
            future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            async with session.get(
                f"{sc.base_url}/reservations",
                params={"dateFrom": today, "dateTo": future, "perPage": 5},
            ) as r:
                if r.status == 200:
                    data = (await r.json()).get("data", [])
                    if data:
                        reservation_id = str(data[0].get("id"))

        print(f"Using propertyId={property_id}  reservationId={reservation_id}\n")

        # --- probe --------------------------------------------------------
        print("STATIC ENDPOINTS")
        for ep in STATIC_ENDPOINTS:
            params = {"propertiesIds": property_id} if ep == "/invoices" and property_id else None
            print_line(await sc.probe(ep, params))
            await asyncio.sleep(0.15)

        if property_id:
            print("\nPROPERTY ENDPOINTS")
            for ep in PROPERTY_ENDPOINTS:
                print_line(await sc.probe(ep.format(prop=property_id)))
                await asyncio.sleep(0.15)

        if reservation_id:
            print("\nRESERVATION ENDPOINTS")
            for ep in RESERVATION_ENDPOINTS:
                print_line(await sc.probe(ep.format(res=reservation_id)))
                await asyncio.sleep(0.15)

        # --- schema drift -------------------------------------------------
        print("\n" + "=" * 70)
        print("FIELD REPORT - what the API exposes that the bot does not use yet")
        print("=" * 70)

        drift = {}

        res_entry = sc.results.get("/reservations", {})
        if res_entry.get("status") == 200:
            keys = {k.split(".")[0].replace("[]", "") for k in res_entry.get("keys", [])}
            new = sorted(keys - KNOWN_RESERVATION_FIELDS)
            drift["reservation_new_fields"] = new
            print(f"\nReservation object - {len(new)} field(s) the bot ignores:")
            for k in new:
                mark = " <-- interesting" if any(s in k.lower() for s in INTERESTING_SUBSTRINGS) else ""
                print(f"   - {k}{mark}")

        for ep_key in (f"/reservations-guests/{reservation_id}", f"/reservations/{reservation_id}/guests"):
            g = sc.results.get(ep_key, {})
            if g.get("status") == 200:
                keys = {k.split(".")[-1].replace("[]", "") for k in g.get("keys", [])}
                new = sorted(keys - KNOWN_GUEST_FIELDS)
                drift[f"guest_new_fields::{ep_key}"] = new
                print(f"\nGuest object ({ep_key}) - {len(new)} field(s) not used:")
                for k in new:
                    mark = " <-- interesting" if any(s in k.lower() for s in INTERESTING_SUBSTRINGS) else ""
                    print(f"   - {k}{mark}")

        # anything anywhere that smells like an online check-in link
        print("\nCheck-in / eVisitor / webhook shaped fields found anywhere:")
        hits = []
        for ep, entry in sc.results.items():
            for k in entry.get("keys", []):
                low = k.lower()
                if any(s in low for s in ("checkin", "check_in", "evisitor", "webhook", "token", "uuid", "link")):
                    hits.append(f"{ep} -> {k}")
        for h in sorted(set(hits)):
            print(f"   {h}")
        if not hits:
            print("   (none)")
        drift["checkin_shaped_fields"] = sorted(set(hits))

        # --- summary ------------------------------------------------------
        alive = [e for e in sc.results.values() if e.get("status") == 200]
        missing = [e["endpoint"] for e in sc.results.values() if e.get("status") == 404]
        print("\n" + "=" * 70)
        print(f"{len(alive)}/{len(sc.results)} endpoints answered 200")
        print(f"404 (do not exist): {', '.join(missing) if missing else 'none'}")
        print("=" * 70)

        out = Path(args.out) if args.out else (
            Path(__file__).parent.parent / "data" / f"api_scan_{datetime.now():%Y%m%d_%H%M}.json"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            {"scanned_at": datetime.now().isoformat(), "base_url": sc.base_url,
             "property_id": property_id, "reservation_id": reservation_id,
             "redacted": not args.raw, "drift": drift, "endpoints": sc.results},
            indent=2, ensure_ascii=False,
        ))
        print(f"\nReport written to {out}")
        if not args.raw:
            print("Report is redacted (field names only) - safe to share.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
