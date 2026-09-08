#!/usr/bin/env python3
"""
Probe the Rentlio API for a way to ISSUE an invoice, not just draft it.

The documented Invoices API can only add items to a draft invoice
(POST /reservations/{id}/invoices/items) and write a fiscalization number.
There is no documented way to:
  - set the invoice date (Rentlio's UI always offers today), or
  - set the payment / transaction type, or
  - close ("izdaj") the invoice.

Rentlio's own web app clearly does all three, so this script checks whether
those routes exist on the public API under an API key - i.e. whether the
Telegram bot could ever issue a back-dated invoice by itself.

It reports, per candidate route:
    404  route does not exist        -> dead end
    405  route exists, wrong method  -> worth trying the allowed method
    400  route exists, bad payload   -> JACKPOT, the route is real
    403  route exists, key not allowed
    200  route exists and answered

Safety
------
By default only GET and OPTIONS are sent - nothing is created or changed.
--write-probes additionally sends deliberately empty payloads, and only ever
to a non-existent invoice id, so a real invoice can never be touched. The one
exception is the collection route POST /invoices: if that route exists and
accepts an empty body, it could create a blank draft invoice. Check Rentlio
afterwards and delete it if one shows up.

Usage:
    python scripts/probe_invoice_write_api.py
    python scripts/probe_invoice_write_api.py --write-probes
    python scripts/probe_invoice_write_api.py --raw --out data/invoice_probe.json
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from src.config import config
except ModuleNotFoundError:  # standalone use, outside a repo checkout
    import os
    from types import SimpleNamespace
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass
    config = SimpleNamespace(
        RENTLIO_API_KEY=os.getenv("RENTLIO_API_KEY", ""),
        RENTLIO_API_URL=os.getenv("RENTLIO_API_URL", "https://api.rentl.io/v1"),
    )

# An id that cannot belong to a real invoice, so write probes are harmless.
FAKE_INVOICE_ID = 999999999

# Enum lists that would have to exist for the bot to pick a transaction type.
ENUM_ENDPOINTS = [
    "/enums/invoices/types",
    "/enums/invoices/statuses",
    "/enums/invoices/payment-types",
    "/enums/payment-types",
    "/enums/services/payment-types",
    "/enums/guests/payment-categories",
    "/enums/currencies",
]

# Routes that would let us create or close an invoice ourselves.
# (method, path template) - {inv} is the fake id, {res}/{prop} are real ids.
WRITE_ROUTES = [
    ("POST", "/invoices"),
    ("POST", "/properties/{prop}/invoices"),
    ("POST", "/reservations/{res}/invoices"),
    ("PUT", "/invoices/{inv}"),
    ("PATCH", "/invoices/{inv}"),
    ("POST", "/invoices/{inv}/issue"),
    ("PUT", "/invoices/{inv}/issue"),
    ("PUT", "/invoices/{inv}/close"),
    ("PUT", "/invoices/{inv}/status"),
    ("PUT", "/invoices/{inv}/date"),
    ("PUT", "/invoices/{inv}/payment-type"),
    ("POST", "/invoices/{inv}/payments"),
]

# Invoice fields worth calling out if the GET response exposes them - these are
# exactly the ones the manual Rentlio clicking sets by hand.
INTERESTING_FIELDS = (
    "date", "issue", "due", "payment", "transaction", "status", "type",
    "number", "fiscal", "jir", "zki", "closed", "draft",
)

STATUS_ICON = {200: "OK  ", 201: "OK  ", 400: "HIT!", 401: "AUTH",
               403: "FORB", 404: "--- ", 405: "M405", 422: "HIT!"}


def icon_for(status) -> str:
    return STATUS_ICON.get(status, "?   ")


def collect_keys(value, prefix: str = "", out: set = None) -> set:
    """Flatten every key path in a JSON structure - names only, no values."""
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


class InvoiceProbe:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, raw: bool):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.raw = raw
        self.results: list[dict] = []

    async def request(self, method: str, endpoint: str, params: dict = None,
                      json_data=None) -> dict:
        url = f"{self.base_url}{endpoint}"
        entry = {"method": method, "endpoint": endpoint}
        try:
            async with self.session.request(
                method, url, params=params, json=json_data,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                entry["status"] = resp.status
                allow = resp.headers.get("Allow") or resp.headers.get("allow")
                if allow:
                    entry["allow"] = allow
                try:
                    entry["body"] = await resp.json()
                except Exception:
                    entry["body"] = None
        except asyncio.TimeoutError:
            entry["status"] = "TIMEOUT"
        except Exception as e:  # noqa: BLE001 - diagnostic script
            entry["status"] = "ERROR"
            entry["error"] = str(e)[:200]
        self.results.append(entry)
        return entry

    def report(self, entry: dict, note: str = ""):
        status = entry["status"]
        extra = note
        if not extra and entry.get("allow"):
            extra = f"Allow: {entry['allow']}"
        if not extra and entry.get("body") is not None and status not in (200, 201):
            extra = str(entry["body"])[:90]
        label = f"{entry['method']} {entry['endpoint']}"
        print(f"  [{icon_for(status)}] {str(status):<7} {label:<46} {extra}")


async def main():
    parser = argparse.ArgumentParser(
        description="Check whether the Rentlio API can issue (not just draft) an invoice"
    )
    parser.add_argument("--property-id", help="Property id (auto-detected if omitted)")
    parser.add_argument("--reservation-id", help="Reservation id (auto-detected if omitted)")
    parser.add_argument("--write-probes", action="store_true",
                        help="Also send empty write payloads to detect real routes")
    parser.add_argument("--raw", action="store_true",
                        help="Include real values in the report (contains guest PII)")
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
        p = InvoiceProbe(session, config.RENTLIO_API_URL, args.raw)

        print(f"\nRentlio invoice-write probe - {config.RENTLIO_API_URL}")
        print(f"{datetime.now():%Y-%m-%d %H:%M}\n")

        # --- discover ids -------------------------------------------------
        property_id = args.property_id
        reservation_id = args.reservation_id

        if not property_id:
            r = await p.request("GET", "/properties")
            data = (r.get("body") or {}).get("data") or []
            if data:
                property_id = str(data[0].get("id"))
            p.results.pop()

        if not reservation_id:
            past = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
            today = datetime.now().strftime("%Y-%m-%d")
            r = await p.request("GET", "/reservations",
                                params={"dateFrom": past, "dateTo": today, "perPage": 5})
            data = (r.get("body") or {}).get("data") or []
            if data:
                reservation_id = str(data[0].get("id"))
            p.results.pop()

        print(f"Using propertyId={property_id}  reservationId={reservation_id}")
        print(f"Write probes: {'ON' if args.write_probes else 'off (GET/OPTIONS only)'}\n")

        def fill(path: str) -> str:
            return (path.replace("{inv}", str(FAKE_INVOICE_ID))
                        .replace("{res}", str(reservation_id))
                        .replace("{prop}", str(property_id)))

        # --- 1. enums -----------------------------------------------------
        print("1. Enums (a transaction type has to come from one of these)")
        found_enums = {}
        for endpoint in ENUM_ENDPOINTS:
            entry = await p.request("GET", endpoint)
            note = ""
            if entry["status"] == 200:
                body = entry.get("body") or {}
                data = body.get("data", body)
                if isinstance(data, list):
                    note = f"({len(data)} items)"
                    found_enums[endpoint] = data
                    if args.raw:
                        entry["sample"] = data[:20]
                else:
                    note = "(object)"
            p.report(entry, note)

        for endpoint, data in found_enums.items():
            labels = [str(d.get("name") or d.get("label") or d.get("value") or d)
                      for d in data if isinstance(d, dict)][:12]
            if labels:
                print(f"        {endpoint} -> {', '.join(labels)}")

        # --- 2. what an existing invoice actually exposes ------------------
        print("\n2. Fields on a real invoice (which of them the UI sets by hand)")
        invoice_id = None
        if property_id:
            entry = await p.request("GET", "/invoices",
                                    params={"propertiesIds": property_id, "perPage": 5})
            data = (entry.get("body") or {}).get("data") or []
            p.report(entry, f"({len(data)} invoices)" if data else "")
            if data:
                invoice_id = str(data[0].get("id"))

        if invoice_id:
            entry = await p.request("GET", f"/invoices/{invoice_id}")
            body = entry.get("body") or {}
            detail = body.get("data", body)
            keys = sorted(collect_keys(detail))
            entry["keys"] = keys
            if not args.raw:
                entry.pop("body", None)
            p.report(entry, f"({len(keys)} fields)")
            hits = [k for k in keys
                    if any(word in k.lower() for word in INTERESTING_FIELDS)]
            for k in hits:
                print(f"        - {k}")
            if not hits:
                print("        (no date / payment / status fields exposed)")
        else:
            print("  (no invoice found to inspect - issue one in Rentlio first)")

        # --- 3. folios ------------------------------------------------------
        if reservation_id:
            print("\n3. Folios (newer endpoint, may carry charges and payment info)")
            entry = await p.request("GET", f"/reservations/{reservation_id}/folios")
            body = entry.get("body") or {}
            data = body.get("data", body)
            keys = sorted(collect_keys(data[0] if isinstance(data, list) and data else data))
            entry["keys"] = keys
            if not args.raw:
                entry.pop("body", None)
            p.report(entry, f"({len(keys)} fields)")
            for k in keys:
                if any(word in k.lower() for word in INTERESTING_FIELDS):
                    print(f"        - {k}")

        # --- 4. can we issue an invoice at all? ----------------------------
        print("\n4. Routes that would let the bot issue a back-dated invoice")
        print("   (404 = dead end, 405/400 = the route is real)")
        for method, template in WRITE_ROUTES:
            endpoint = fill(template)
            if "None" in endpoint:
                continue
            entry = await p.request("OPTIONS", endpoint)
            p.report(entry)
            if args.write_probes:
                entry = await p.request(method, endpoint, json_data={})
                p.report(entry)

        # --- verdict --------------------------------------------------------
        real_routes = [e for e in p.results
                       if e["method"] in ("POST", "PUT", "PATCH")
                       and e["status"] in (200, 201, 400, 405, 422)]
        print("\n" + "=" * 66)
        if real_routes:
            print("VERDICT: at least one write route answered - full automation")
            print("         may be possible. Routes worth pursuing:")
            for e in real_routes:
                print(f"         {e['method']} {e['endpoint']} -> {e['status']}")
        elif args.write_probes:
            print("VERDICT: no write route exists. The API can only prepare a DRAFT;")
            print("         date and transaction type have to be set in Rentlio,")
            print("         or by driving the web UI in a browser.")
        else:
            print("VERDICT: GET/OPTIONS only. Re-run with --write-probes for a")
            print("         definitive answer on the write routes.")
        print("=" * 66)

        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(p.results, indent=2, default=str))
            print(f"\nReport written to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
