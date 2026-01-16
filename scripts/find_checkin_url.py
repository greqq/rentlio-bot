#!/usr/bin/env python3
"""
Find how Rentlio generates online check-in URLs
Looking for UUID/token fields in API responses
"""
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.rentlio_api import RentlioAPI
from src.config import config


def search_for_uuid_fields(data: dict, path: str = "") -> list:
    """Recursively search for UUID-like fields or check-in related fields"""
    findings = []
    
    if isinstance(data, dict):
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key
            
            # Look for keywords
            if any(keyword in key.lower() for keyword in ['checkin', 'check_in', 'token', 'uuid', 'code', 'link', 'url', 'hash']):
                findings.append({
                    "path": current_path,
                    "key": key,
                    "value": value,
                    "type": type(value).__name__
                })
            
            # Look for UUID-like strings (format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
            if isinstance(value, str) and len(value) == 36 and value.count('-') == 4:
                findings.append({
                    "path": current_path,
                    "key": key,
                    "value": value,
                    "type": "UUID-like string"
                })
            
            # Recurse
            if isinstance(value, (dict, list)):
                findings.extend(search_for_uuid_fields(value, current_path))
    
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            current_path = f"{path}[{idx}]"
            findings.extend(search_for_uuid_fields(item, current_path))
    
    return findings


async def main():
    if not config.RENTLIO_API_KEY:
        print("❌ RENTLIO_API_KEY not set")
        return
    
    api = RentlioAPI()
    
    try:
        print("🔍 Searching for online check-in URL patterns...\n")
        
        # Get property
        properties = await api.get_properties()
        if not properties:
            print("❌ No properties found")
            return
        
        property_id = properties[0]["id"]
        print(f"✅ Property: {properties[0]['name']} (ID: {property_id})\n")
        
        # Get upcoming reservations
        date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        date_to = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        
        print(f"📡 Fetching reservations from {date_from} to {date_to}...\n")
        
        reservations = await api.get_reservations(
            property_id=property_id,
            date_from=date_from,
            date_to=date_to,
            limit=20
        )
        
        print(f"✅ Found {len(reservations)} reservations\n")
        
        # Search each reservation in detail
        all_findings = []
        
        for idx, reservation in enumerate(reservations[:5]):  # Check first 5
            res_id = reservation["id"]
            guest_name = reservation.get("guestName", "Unknown")
            
            print(f"{'='*60}")
            print(f"Reservation #{idx+1}: {guest_name} (ID: {res_id})")
            print(f"{'='*60}")
            
            # Get detailed info
            details = await api.get_reservation_details(res_id)
            
            # Search for UUID/token fields
            findings = search_for_uuid_fields(details)
            
            if findings:
                print("🎯 Found potential check-in URL fields:")
                for finding in findings:
                    print(f"   • {finding['path']}")
                    print(f"     Key: {finding['key']}")
                    print(f"     Value: {finding['value']}")
                    print(f"     Type: {finding['type']}\n")
                all_findings.extend(findings)
            else:
                print("❌ No UUID/token fields found in this reservation\n")
            
            # Also print full JSON to inspect manually
            print("\n📄 Full reservation details:")
            print(json.dumps(details, indent=2, default=str))
            print("\n")
        
        # Summary
        print(f"{'='*60}")
        print("📊 SUMMARY")
        print(f"{'='*60}")
        
        if all_findings:
            print(f"\n✅ Found {len(all_findings)} potential fields:")
            for finding in all_findings:
                print(f"   • {finding['path']}: {finding['value'][:50] if isinstance(finding['value'], str) else finding['value']}")
        else:
            print("\n❌ No UUID/check-in URL fields found in API responses")
            print("\nThis confirms your documentation:")
            print("→ Online check-in URLs are NOT provided via the API")
            print("→ You'll need to get them from Rentlio's web interface")
            print("→ Or use Playwright to automate the form directly")
        
        print(f"\n{'='*60}")
        print("🔗 URL Pattern Analysis")
        print(f"{'='*60}")
        print("\nYour check-in URL format:")
        print("https://sun-apartments.book.rentl.io/reservation/check-in/552193f5-9d33-4561-9e9e-dbaaf5c72587")
        print("\nPattern:")
        print("https://<property-subdomain>.book.rentl.io/reservation/check-in/<UUID>")
        print("\nPossible UUID sources to check:")
        print("1. Reservation confirmation emails from Rentlio")
        print("2. Rentlio web dashboard (manual copy)")
        print("3. Rentlio might have a separate endpoint we haven't found")
        print("4. It might be generated server-side and only sent via email")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
