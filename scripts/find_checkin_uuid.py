#!/usr/bin/env python3
"""
Decide once and for all whether the online check-in UUID is reachable via API.

We know one pair: reservation 10667569 -> 05166c33-5df2-4d3d-8720-3756a3ca6876
So instead of guessing which field might hold it, we fetch every plausible
endpoint and grep the RAW response text for that exact string. If it is not
in any response, the link genuinely cannot be built from the API and we stop
chasing it.

Also reports every UUID-shaped string found anywhere, in case the check-in
token lives under an unexpected name.

Usage:
    python scripts/find_checkin_uuid.py \
        --reservation-id 10667569 \
        --uuid 05166c33-5df2-4d3d-8720-3756a3ca6876
"""
import argparse
import asyncio
import re
import sys
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

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Anything that could plausibly carry a per-reservation token.
ENDPOINTS = [
    "/reservations/{res}",
    "/reservations/{res}/details",
    "/reservations/{res}/guests",
    "/reservations-guests/{res}",
    "/reservations/{res}/invoices",
    "/reservations/{res}/folios",
    "/reservations?perPage=1&page=1",
    "/properties",
    "/users/me",
    "/webhooks",
]

# Substrings that would betray a check-in link even under an odd field name.
NEEDLES = ("book.rentl.io", "check-in", "checkin", "reservation/check")


async def fetch(session, base, endpoint):
    url = f"{base}{endpoint}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return r.status, await r.text()
    except Exception as e:  # noqa: BLE001 - diagnostic script
        return "ERR", str(e)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reservation-id", required=True)
    ap.add_argument("--uuid", required=True, help="Known check-in UUID for that reservation")
    args = ap.parse_args()

    if not config.RENTLIO_API_KEY:
        print("RENTLIO_API_KEY not set")
        return 1

    base = config.RENTLIO_API_URL.rstrip("/")
    headers = {"apikey": config.RENTLIO_API_KEY, "Accept": "application/json"}
    target = args.uuid.lower()

    print(f"\nLooking for {target}")
    print(f"in every API response for reservation {args.reservation_id}\n")

    found_exact = []
    all_uuids = {}

    async with aiohttp.ClientSession(headers=headers) as session:
        for ep in ENDPOINTS:
            path = ep.format(res=args.reservation_id)
            status, body = await fetch(session, base, path)
            low = body.lower() if isinstance(body, str) else ""

            hit = target in low
            needle_hits = [n for n in NEEDLES if n in low]
            uuids = set(UUID_RE.findall(body)) if isinstance(body, str) else set()
            if uuids:
                all_uuids[path] = uuids

            flag = "  *** EXACT MATCH ***" if hit else ""
            extra = f"  needles={needle_hits}" if needle_hits else ""
            print(f"  {str(status):<6} {path:<44} {len(body):>7}b{extra}{flag}")

            if hit:
                # show surrounding context so we can see the field name
                i = low.index(target)
                print(f"        context: ...{body[max(0, i-120):i+60]}...")
                found_exact.append(path)
            await asyncio.sleep(0.2)

    print("\n" + "=" * 70)
    if found_exact:
        print("FOUND. The check-in UUID IS available via API, in:")
        for p in found_exact:
            print(f"   {p}")
        print("\n-> The bot can build the official Rentlio check-in link itself.")
    else:
        print("NOT FOUND. The check-in UUID appears in no API response.")
        print("-> The link cannot be built from the API. Own form it is.")

    if all_uuids:
        print("\nOther UUID-shaped values seen (in case the token is named oddly):")
        for path, uu in all_uuids.items():
            for u in sorted(uu):
                print(f"   {path}: {u}")
    else:
        print("\nNo UUID-shaped values anywhere in these responses.")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
