#!/usr/bin/env python3
"""
Test script for Rentlio Invoice API

Tests the invoice creation and retrieval functionality.
"""
import asyncio
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.rentlio_api import RentlioAPI, RentlioAPIError


async def main():
    api = RentlioAPI()
    
    print("🧪 Testing Rentlio Invoice API\n")
    print("=" * 50)
    
    # First, let's get a recent reservation to test with
    print("\n1. Getting recent reservations...")
    try:
        reservations = await api.get_reservations(limit=5)
        if not reservations:
            print("❌ No reservations found")
            return
        
        print(f"✅ Found {len(reservations)} reservations")
        for r in reservations[:3]:
            print(f"   - #{r.get('id')} | {r.get('guestName')} | {r.get('unitName')}")
        
        # Use the first reservation for testing
        test_reservation_id = str(reservations[0].get('id'))
        print(f"\n📋 Using reservation #{test_reservation_id} for testing")
        
    except RentlioAPIError as e:
        print(f"❌ API Error: {e.message}")
        return
    
    # Test getting reservation details
    print("\n2. Getting reservation details...")
    try:
        details = await api.get_reservation_details(test_reservation_id)
        print(f"   Raw response keys: {list(details.keys()) if details else 'None'}")
        
        holder = details.get('holder') if details else None
        guest_name = holder.get('name', 'N/A') if holder else details.get('guestName', 'N/A') if details else 'N/A'
        
        print(f"✅ Guest: {guest_name}")
        print(f"   Unit: {details.get('unitName', 'N/A') if details else 'N/A'}")
        print(f"   Total: {details.get('totalPrice', 0) if details else 0} EUR")
        print(f"   Nights: {details.get('totalNights', 0) if details else 0}")
    except RentlioAPIError as e:
        print(f"❌ API Error: {e.message}")
    
    # Test getting invoices for reservation
    print("\n3. Getting invoices for reservation...")
    try:
        invoices = await api.get_reservation_invoices(test_reservation_id)
        if invoices:
            print(f"✅ Found {len(invoices)} invoice(s)")
            for inv in invoices:
                print(f"   - #{inv.get('id')} | {inv.get('totalValue', 0)} EUR | Status: {inv.get('status', {}).get('name', 'N/A')}")
        else:
            print("📭 No invoices for this reservation")
    except RentlioAPIError as e:
        print(f"❌ API Error: {e.message}")
    
    # Ask before creating test invoice item
    print("\n" + "=" * 50)
    response = input("\n🔴 Do you want to create a TEST invoice item? (yes/no): ")
    
    if response.lower() == 'yes':
        print("\n4. Creating test invoice item...")
        try:
            result = await api.add_invoice_item(
                reservation_id=test_reservation_id,
                description="TEST ITEM - delete me",
                price=0.01,
                quantity=1,
                vat_included="Y",
                taxes=[{"label": "PDV", "rate": 25}]
            )
            print(f"✅ Invoice item created!")
            print(f"   ID: {result.get('id')}")
            print(f"   Total: {result.get('totalPrice')} EUR")
            print(f"\n⚠️  Remember to delete this test item from Rentlio!")
        except RentlioAPIError as e:
            print(f"❌ API Error: {e.message}")
            print(f"   Response: {e.response_data}")
    else:
        print("⏭️  Skipped invoice creation")
    
    # Clean up
    await api.close()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(main())
