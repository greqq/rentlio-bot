#!/usr/bin/env python3
"""
Test script to preview daily notifications without sending them
Run this locally to see what the bot would send
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.rentlio_api import RentlioAPI


async def test_daily_notification():
    """Simulate the daily notification to see what would be sent"""
    print("🧪 Testing daily notification logic...\n")
    
    api = RentlioAPI()
    
    try:
        # Same logic as get_daily_summary()
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        
        today_str = today.strftime("%Y-%m-%d")
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")
        
        today_ts_start = int(today.replace(hour=0, minute=0, second=0).timestamp())
        today_ts_end = int(today.replace(hour=23, minute=59, second=59).timestamp())
        tomorrow_ts_start = int(tomorrow.replace(hour=0, minute=0, second=0).timestamp())
        tomorrow_ts_end = int(tomorrow.replace(hour=23, minute=59, second=59).timestamp())
        
        print(f"📅 Fetching reservations for {today_str} to {tomorrow_str}...\n")
        
        # Get reservations for today and tomorrow
        all_reservations = await api.get_reservations(
            date_from=today_str,
            date_to=tomorrow_str,
            limit=100
        )
        
        print(f"📦 Found {len(all_reservations)} total reservations in date range\n")
        
        # Debug: show ALL reservations with their status
        print("📋 RAW DATA FROM API:")
        print("-" * 60)
        for res in all_reservations:
            arrival_ts = res.get("arrivalDate", 0)
            departure_ts = res.get("departureDate", 0)
            arrival_date = datetime.fromtimestamp(arrival_ts).strftime("%Y-%m-%d") if arrival_ts else "N/A"
            departure_date = datetime.fromtimestamp(departure_ts).strftime("%Y-%m-%d") if departure_ts else "N/A"
            guest = res.get("guestName", "Unknown")
            unit = res.get("unitName", "")
            status = res.get("status", "")
            res_id = str(res.get("id", ""))
            checked_in = res.get("checkedIn", "")
            print(f"  ID: {res_id[:8] if len(res_id) > 8 else res_id} | {guest or '(no name)'} | {unit} | {arrival_date} -> {departure_date} | status: {status} | checkedIn: {checked_in}")
        print("-" * 60 + "\n")
        
        # Filter to only confirmed reservations (status=1)
        CONFIRMED_STATUS = 1
        all_reservations = [r for r in all_reservations if r.get("status") == CONFIRMED_STATUS]
        print(f"✅ Filtered to {len(all_reservations)} CONFIRMED reservations (status=1)\n")
        
        arrivals = []
        departures = []
        tomorrow_arrivals = []
        
        # Use set to track reservation IDs and avoid duplicates
        seen_arrival_ids = set()
        seen_departure_ids = set()
        seen_tomorrow_ids = set()
        
        for res in all_reservations:
            arrival_ts = res.get("arrivalDate", 0)
            departure_ts = res.get("departureDate", 0)
            res_id = res.get("id")
            
            arrival_date = datetime.fromtimestamp(arrival_ts).strftime("%Y-%m-%d") if arrival_ts else "N/A"
            departure_date = datetime.fromtimestamp(departure_ts).strftime("%Y-%m-%d") if departure_ts else "N/A"
            guest = res.get("guestName", "Unknown")
            
            # Today's arrivals - exact match on arrival date
            if today_ts_start <= arrival_ts <= today_ts_end and res_id not in seen_arrival_ids:
                arrivals.append(res)
                seen_arrival_ids.add(res_id)
                print(f"✅ TODAY ARRIVAL: {guest} (arrives: {arrival_date})")
            
            # Today's departures - exact match on departure date
            if today_ts_start <= departure_ts <= today_ts_end and res_id not in seen_departure_ids:
                departures.append(res)
                seen_departure_ids.add(res_id)
                print(f"🔴 TODAY DEPARTURE: {guest} (leaves: {departure_date})")
            
            # Tomorrow's arrivals - exact match on arrival date
            if tomorrow_ts_start <= arrival_ts <= tomorrow_ts_end and res_id not in seen_tomorrow_ids:
                tomorrow_arrivals.append(res)
                seen_tomorrow_ids.add(res_id)
                print(f"📅 TOMORROW ARRIVAL: {guest} (arrives: {arrival_date})")
        
        print(f"\n{'='*50}")
        print(f"Summary: {len(arrivals)} arrivals today, {len(departures)} departures today, {len(tomorrow_arrivals)} arrivals tomorrow")
        print(f"{'='*50}\n")
        
        # Skip if nothing happening today AND tomorrow
        if not arrivals and not departures and not tomorrow_arrivals:
            print("📭 No arrivals or departures - notification would be skipped")
            return
        
        today_display = today.strftime("%d.%m.%Y")
        tomorrow_display = tomorrow.strftime("%d.%m.%Y")
        
        # Build message with cleaner format
        text = f"🌅 **Dnevni pregled - {today_display}**\n\n"
        
        # Today's Departures (CHECK-OUT) - show first as they leave
        if departures:
            text += f"🔴 **ODLASCI DANAS ({len(departures)})**\n"
            # Group by unit
            by_unit = {}
            for res in departures:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    text += f"• {guest} ← {unit}\n"
            text += "\n"
        
        # Today's Arrivals (CHECK-IN)
        if arrivals:
            text += f"🟢 **DOLASCI DANAS ({len(arrivals)})**\n"
            # Group by unit
            by_unit = {}
            for res in arrivals:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                text += f"  🏠 _{unit}_\n"
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    phone = res.get("guestContactNumber", "")
                    nights = res.get("totalNights", 0)
                    text += f"  • {guest} ({nights} {'noć' if nights == 1 else 'noći'})\n"
                    if phone:
                        text += f"    📞 {phone}\n"
            text += "\n"
        
        # Tomorrow's Arrivals (REMINDER - send instructions!)
        if tomorrow_arrivals:
            text += f"📅 **SUTRA DOLAZE ({len(tomorrow_arrivals)}) - {tomorrow_display}**\n"
            text += "⚠️ _Pošalji upute gostima!_\n\n"
            
            # Group by unit
            by_unit = {}
            for res in tomorrow_arrivals:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                text += f"  🏠 _{unit}_\n"
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    phone = res.get("guestContactNumber", "")
                    nights = res.get("totalNights", 0)
                    email = res.get("guestEmail", "")
                    
                    text += f"  • **{guest}** ({nights} {'noć' if nights == 1 else 'noći'})\n"
                    if phone:
                        text += f"    📞 {phone}\n"
                    if email:
                        text += f"    ✉️ {email}\n"
        
        # Display the message that would be sent
        print("📤 MESSAGE THAT WOULD BE SENT TO TELEGRAM:")
        print("─" * 50)
        print(text)
        print("─" * 50)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await api.close()


async def test_upcoming():
    """Test the upcoming reservations logic"""
    print("\n\n🧪 Testing upcoming reservations (next 7 days)...\n")
    
    api = RentlioAPI()
    
    try:
        today = datetime.now()
        week_later = today + timedelta(days=7)
        
        today_str = today.strftime("%Y-%m-%d")
        week_str = week_later.strftime("%Y-%m-%d")
        
        today_ts = int(today.replace(hour=0, minute=0, second=0).timestamp())
        week_ts = int(week_later.replace(hour=23, minute=59, second=59).timestamp())
        
        print(f"📅 Fetching reservations for {today_str} to {week_str}...\n")
        
        # Fetch reservations
        all_reservations = await api.get_reservations(
            date_from=today_str,
            date_to=week_str,
            limit=50
        )
        
        print(f"📦 Found {len(all_reservations)} total reservations in date range")
        
        # Filter to only confirmed (status=1) arrivals in next 7 days (not ongoing stays)
        CONFIRMED_STATUS = 1
        arrivals = [r for r in all_reservations 
                   if r.get("status") == CONFIRMED_STATUS and today_ts <= r.get("arrivalDate", 0) <= week_ts]
        
        print(f"✅ Filtered to {len(arrivals)} CONFIRMED arrivals in next 7 days\n")
        
        # Sort by arrival date
        arrivals.sort(key=lambda x: x.get("arrivalDate", 0))
        
        # Group by unit
        from collections import defaultdict
        by_unit = defaultdict(list)
        for res in arrivals:
            unit = res.get("unitName", "Unknown")
            by_unit[unit].append(res)
        
        for unit in sorted(by_unit.keys()):
            print(f"🏠 {unit}")
            # Sort by arrival date within unit
            unit_arrivals = sorted(by_unit[unit], key=lambda x: x.get("arrivalDate", 0))
            for res in unit_arrivals:
                arrival_date = datetime.fromtimestamp(res.get("arrivalDate", 0)).strftime("%d.%m")
                guest = res.get("guestName", "Unknown")
                nights = res.get("totalNights", 0)
                print(f"  • {arrival_date}: {guest} ({nights} {'noć' if nights == 1 else 'noći'})")
            print()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await api.close()


if __name__ == "__main__":
    print("=" * 50)
    print("RENTLIO BOT - NOTIFICATION TEST")
    print("=" * 50)
    asyncio.run(test_daily_notification())
    asyncio.run(test_upcoming())
