#!/usr/bin/env python3
"""Try to find or generate check-in URL via API"""
import asyncio
import aiohttp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config


async def test_checkin_endpoints(api_key: str, reservation_id: int):
    """Try various endpoint patterns to get check-in URL"""
    
    base_url = "https://api.rentl.io/v1"
    headers = {
        "apikey": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Endpoints to try
    test_patterns = [
        f"/reservations/{reservation_id}/checkin-url",
        f"/reservations/{reservation_id}/online-checkin",
        f"/reservations/{reservation_id}/online-checkin-url",
        f"/reservations/{reservation_id}/checkin-link",
        f"/reservations/{reservation_id}/guest-url",
        f"/reservations/{reservation_id}/public-url",
        f"/reservations/{reservation_id}/token",
        f"/online-checkin/{reservation_id}",
        f"/online-checkin/url/{reservation_id}",
    ]
    
    print(f"🔍 Testing possible check-in URL endpoints for reservation {reservation_id}...\n")
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for endpoint in test_patterns:
            url = f"{base_url}{endpoint}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    status = response.status
                    
                    if status == 200:
                        print(f"✅ {endpoint}")
                        data = await response.json()
                        print(f"   Response: {data}\n")
                        return data
                    elif status == 404:
                        print(f"❌ {endpoint} → 404")
                    elif status == 400:
                        print(f"⚠️  {endpoint} → 400 (Bad Request)")
                        try:
                            error = await response.json()
                            print(f"   Error: {error}")
                        except:
                            pass
                    elif status == 401:
                        print(f"🔑 {endpoint} → 401 (Unauthorized)")
                    else:
                        print(f"❓ {endpoint} → {status}")
                        
            except asyncio.TimeoutError:
                print(f"⏱️  {endpoint} → TIMEOUT")
            except Exception as e:
                print(f"💥 {endpoint} → ERROR: {str(e)[:50]}")
    
    print("\n" + "="*60)
    print("❌ No working check-in URL endpoint found")
    print("="*60)
    return None


async def main():
    if not config.RENTLIO_API_KEY:
        print("❌ RENTLIO_API_KEY not set")
        return
    
    # Use one of the reservation IDs we found earlier
    reservation_id = 9609866  # Syed Munawar Suleiman reservation
    
    result = await test_checkin_endpoints(config.RENTLIO_API_KEY, reservation_id)
    
    if not result:
        print("\n💡 CONCLUSION:")
        print("="*60)
        print("The online check-in URL is NOT available via any API endpoint.")
        print("\nYour options:")
        print("1. Extract it from Rentlio confirmation emails")
        print("2. Copy it manually from Rentlio web dashboard")
        print("3. Have users forward the email with the link")
        print("4. Store the UUID mapping yourself when you first get it")
        print("\nOR - skip the URL entirely and use Playwright to:")
        print("→ Log into Rentlio dashboard")
        print("→ Navigate to the reservation")  
        print("→ Click the check-in button")
        print("→ Fill the form programmatically")


if __name__ == "__main__":
    asyncio.run(main())
