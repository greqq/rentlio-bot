#!/usr/bin/env python3
"""Test different Rentlio API URL patterns to find the correct one"""
import asyncio
import aiohttp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config


async def test_url(base_url: str, api_key: str):
    """Test if a base URL works"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    test_endpoints = [
        "/reservations",
        "/properties",
        "/api/reservations",
        "/api/properties",
        "/v1/reservations",
        "/v1/properties",
    ]
    
    print(f"\n{'='*60}")
    print(f"Testing Base URL: {base_url}")
    print('='*60)
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for endpoint in test_endpoints:
            url = f"{base_url}{endpoint}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    status = response.status
                    if status == 200:
                        print(f"✅ {url} → {status} (SUCCESS!)")
                        data = await response.json()
                        print(f"   Sample response: {str(data)[:200]}")
                        return url
                    elif status == 401:
                        print(f"🔑 {url} → {status} (Auth error - API key might be wrong)")
                    elif status == 404:
                        print(f"❌ {url} → {status}")
                    else:
                        print(f"⚠️  {url} → {status}")
            except asyncio.TimeoutError:
                print(f"⏱️  {url} → TIMEOUT")
            except Exception as e:
                print(f"💥 {url} → ERROR: {str(e)[:50]}")
    
    return None


async def main():
    if not config.RENTLIO_API_KEY:
        print("❌ RENTLIO_API_KEY not set in .env")
        return
    
    print(f"🔑 API Key: {config.RENTLIO_API_KEY[:10]}...{config.RENTLIO_API_KEY[-5:]}")
    
    # Test various base URL patterns
    base_urls = [
        "https://api.rentl.io",
        "https://api.rentl.io/v1",
        "https://api.rentl.io/api",
        "https://api.rentl.io/api/v1",
        "https://app.rentl.io/api",
        "https://app.rentl.io/api/v1",
        "https://rentl.io/api",
        "https://rentl.io/api/v1",
    ]
    
    for base_url in base_urls:
        working_url = await test_url(base_url, config.RENTLIO_API_KEY)
        if working_url:
            print(f"\n{'='*60}")
            print(f"🎉 FOUND WORKING ENDPOINT!")
            print(f"{'='*60}")
            print(f"Working URL: {working_url}")
            print(f"\nUpdate your .env file:")
            print(f'RENTLIO_API_URL={base_url}')
            return
    
    print(f"\n{'='*60}")
    print("❌ No working endpoint found")
    print("{'='*60}")
    print("\nPossible issues:")
    print("1. API key might be incorrect")
    print("2. API base URL might be different")
    print("3. You might need additional authentication")
    print("\nCheck Rentlio documentation or contact support at integrations@rentl.io")


if __name__ == "__main__":
    asyncio.run(main())
