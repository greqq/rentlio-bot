#!/usr/bin/env python3
"""
Rentlio API Explorer Script

Run this to see what data the Rentlio API returns.
This helps us understand the exact field names and structure.

Usage:
    python scripts/explore_api.py
"""
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.rentlio_api import RentlioAPI, RentlioAPIError
from src.config import config


def pretty_print(title: str, data: any):
    """Print data in a readable format"""
    print(f"\n{'='*60}")
    print(f"📋 {title}")
    print('='*60)
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


async def explore_api():
    """Explore Rentlio API endpoints"""
    
    # Validate configuration
    if not config.RENTLIO_API_KEY:
        print("❌ Error: RENTLIO_API_KEY not set in .env file")
        print("Please create a .env file with your Rentlio API key")
        return
    
    api = RentlioAPI()
    
    try:
        print("\n🔍 Starting Rentlio API Exploration...")
        print(f"Base URL: {api.base_url}")
        
        # 1. Get Properties
        print("\n📡 Fetching properties...")
        try:
            properties = await api.get_properties()
            pretty_print("PROPERTIES", properties)
            
            if properties:
                property_id = properties[0].get("id")
                print(f"\n✅ Found {len(properties)} property(ies)")
                print(f"   Using first property ID: {property_id}")
            else:
                property_id = None
                print("⚠️  No properties found")
        except RentlioAPIError as e:
            print(f"❌ Error fetching properties: {e}")
            property_id = None
        
        # 2. Get Reservations
        print("\n📡 Fetching reservations...")
        try:
            # Get reservations for next 30 days
            date_from = datetime.now().strftime("%Y-%m-%d")
            date_to = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            
            reservations = await api.get_reservations(
                property_id=property_id,
                date_from=date_from,
                date_to=date_to,
                limit=10
            )
            pretty_print("RESERVATIONS (Next 30 days)", reservations)
            
            if reservations:
                reservation_id = reservations[0].get("id")
                print(f"\n✅ Found {len(reservations)} reservation(s)")
                print(f"   Using first reservation ID: {reservation_id}")
            else:
                reservation_id = None
                print("⚠️  No upcoming reservations found")
        except RentlioAPIError as e:
            print(f"❌ Error fetching reservations: {e}")
            reservation_id = None
        
        # 3. Get Reservation Details (if we have one)
        if reservation_id:
            print("\n📡 Fetching reservation details...")
            try:
                details = await api.get_reservation_details(reservation_id)
                pretty_print(f"RESERVATION DETAILS (ID: {reservation_id})", details)
                
                # Look for online check-in URL
                checkin_url = (
                    details.get("onlineCheckinUrl") or 
                    details.get("online_checkin_url") or
                    details.get("checkinUrl")
                )
                if checkin_url:
                    print(f"\n🔗 Online Check-in URL found: {checkin_url}")
                else:
                    print("\n⚠️  No online check-in URL in response")
                    print("   Check the raw data above for the correct field name")
                    
            except RentlioAPIError as e:
                print(f"❌ Error fetching reservation details: {e}")
            
            # 4. Get Guests for Reservation
            print("\n📡 Fetching reservation guests...")
            try:
                guests = await api.get_reservation_guests(reservation_id)
                pretty_print(f"GUESTS (Reservation: {reservation_id})", guests)
            except RentlioAPIError as e:
                print(f"❌ Error fetching guests: {e}")
        
        # 5. Get Checked-in Guests (if we have property)
        if property_id:
            print("\n📡 Fetching checked-in guests...")
            try:
                checked_in = await api.get_checked_in_guests(property_id)
                pretty_print(f"CHECKED-IN GUESTS (Property: {property_id})", checked_in)
            except RentlioAPIError as e:
                print(f"❌ Error fetching checked-in guests: {e}")
        
        # 6. Get Invoices
        print("\n📡 Fetching invoices...")
        try:
            invoices = await api.get_invoices(property_id=property_id, limit=5)
            pretty_print("RECENT INVOICES", invoices)
        except RentlioAPIError as e:
            print(f"❌ Error fetching invoices: {e}")
        
        # Summary
        print("\n" + "="*60)
        print("📊 EXPLORATION COMPLETE")
        print("="*60)
        print("""
Next Steps:
1. Review the JSON output above
2. Note the exact field names used by Rentlio
3. Update src/services/rentlio_api.py if field names differ
4. Look for 'onlineCheckinUrl' or similar in reservation details
        """)
        
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(explore_api())
