#!/usr/bin/env python3
"""
Run the occupancy / pricing analysis from the terminal.

    python scripts/analyze_occupancy.py                 # next 30 days
    python scripts/analyze_occupancy.py --days 60       # next 60 days
    python scripts/analyze_occupancy.py --calendar      # night-by-night view
    python scripts/analyze_occupancy.py --ai            # add the Claude brief
    python scripts/analyze_occupancy.py --json          # raw analysis payload
    python scripts/analyze_occupancy.py --demo          # synthetic data, no API key

`--demo` runs the whole engine on generated reservations, which is the quickest
way to see what the bot will print before pointing it at a live account.
"""
import argparse
import asyncio
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services import occupancy_service  # noqa: E402
from src.services.occupancy_analyzer import (  # noqa: E402
    OccupancyAnalyzer,
    Stay,
    shift_years,
)
from src.services.rentlio_api import RentlioAPI, RentlioAPIError  # noqa: E402


def build_demo_report(days: int):
    """Generate two past seasons plus a partly booked horizon, then analyze."""
    rng = random.Random(7)
    today = date.today()
    units = ["Apartman A", "Apartman B"]

    def season(year_offset: int, fill: float) -> list[Stay]:
        """Fill a season's window with stays at roughly `fill` occupancy."""
        stays: list[Stay] = []
        start = shift_years(today, -year_offset) - timedelta(days=30)
        end = shift_years(today + timedelta(days=days), -year_offset) + timedelta(days=30)
        for unit in units:
            cursor = start
            while cursor < end:
                nights = rng.choice([2, 3, 4, 5, 7])
                if rng.random() < fill:
                    departure = cursor + timedelta(days=nights)
                    stays.append(Stay(
                        reservation_id=f"demo-{unit}-{cursor}",
                        unit=unit,
                        arrival=cursor,
                        departure=departure,
                        total_price=nights * rng.uniform(85, 135),
                        channel=rng.choice(["Booking.com", "Airbnb", "Direct"]),
                        guest_name="Demo",
                        booked_on=cursor - timedelta(days=rng.randint(5, 90)),
                    ))
                    cursor = departure + timedelta(days=rng.choice([0, 0, 1, 2]))
                else:
                    cursor += timedelta(days=nights)
        return stays

    history = {1: season(1, 0.82), 2: season(2, 0.75)}

    # Current horizon: a couple of bookings, one deliberate one-night orphan.
    current = [
        Stay("c1", "Apartman A", today + timedelta(days=2), today + timedelta(days=6),
             430, "Booking.com", "Gost 1", today - timedelta(days=20)),
        Stay("c2", "Apartman A", today + timedelta(days=7), today + timedelta(days=11),
             450, "Airbnb", "Gost 2", today - timedelta(days=9)),
        Stay("c3", "Apartman B", today + timedelta(days=1), today + timedelta(days=4),
             330, "Direct", "Gost 3", today - timedelta(days=35)),
        Stay("c4", "Apartman B", today + timedelta(days=18), today + timedelta(days=23),
             560, "Booking.com", "Gost 4", today - timedelta(days=15)),
    ]

    return OccupancyAnalyzer().analyze(
        today=today,
        horizon_days=days,
        current_stays=current,
        history=history,
        units=units,
    )


async def main():
    parser = argparse.ArgumentParser(description="Rentlio occupancy analysis")
    parser.add_argument("--days", type=int, default=30, help="horizon length (default 30)")
    parser.add_argument("--years", type=int, default=2, help="seasons of history to read")
    parser.add_argument("--property-id", default=None, help="restrict to one property")
    parser.add_argument("--calendar", action="store_true", help="print the night-by-night calendar")
    parser.add_argument("--ai", action="store_true", help="add the Claude brief")
    parser.add_argument("--json", action="store_true", help="print the analysis payload as JSON")
    parser.add_argument("--demo", action="store_true", help="use synthetic data (no API key needed)")
    parser.add_argument("--no-cache", action="store_true", help="ignore the in-process cache")
    args = parser.parse_args()

    if args.demo:
        report = build_demo_report(args.days)
    else:
        api = RentlioAPI()
        try:
            report = await occupancy_service.run_analysis(
                api,
                horizon_days=args.days,
                history_years=args.years,
                property_id=args.property_id,
                use_cache=not args.no_cache,
            )
        except RentlioAPIError as e:
            print(f"Rentlio API error: {e}")
            return 1
        finally:
            await api.close()

    if args.json:
        print(json.dumps(report.to_payload(), ensure_ascii=False, indent=2, default=str))
        return 0

    for chunk in occupancy_service.format_full_report(report, include_calendar=args.calendar):
        print(chunk)
        print()

    if args.ai:
        from src.services import ai_advisor
        reason = ai_advisor.unavailable_reason()
        if reason:
            print(reason)
        else:
            advice = await ai_advisor.generate_advice(report)
            print("=== AI BRIEF ===")
            print(advice or "(AI brief nije uspio - vidi logove)")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
