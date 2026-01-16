#!/usr/bin/env python3
"""Test different auth header formats for Rentlio API"""
import asyncio
import aiohttp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import config


async def test_auth_method(url: str, api_key: str, auth_format: str):
    """Test different authorization header formats"""
    
    auth_headers = {
        "Bearer": {"Authorization": f"Bearer {api_key}"},
        "Token": {"Authorization": f"Token {api_key}"},
        "ApiKey": {"Authorization": f"ApiKey {api_key}"},
        "X-API-Key": {"X-API-Key": api_key},
        "api-key": {"api-key": api_key},
        "apikey": {"apikey": api_key},
        "Plain": {"Authorization": api_key},
    }
    
    headers = auth_headers.get(auth_format, {})
    headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json"
    })
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                status = response.status
                if status == 200:
                    data = await response.json()
                    print(f"✅ {auth_format:15} → {status} SUCCESS!")
                    print(f"   Sample: {str(data)[:150]}")
                    return True
                elif status == 401:
                    print(f"🔑 {auth_format:15} → {status} (Unauthorized)")
                elif status == 404:
                    print(f"❌ {auth_format:15} → {status} (Not Found)")
                else:
                    print(f"⚠️  {auth_format:15} → {status}")
                    try:
                        error_data = await response.json()
                        print(f"   Error: {error_data}")
                    except:
                        text = await response.text()
                        print(f"   Response: {text[:100]}")
    except Exception as e:
        print(f"💥 {auth_format:15} → ERROR: {str(e)[:50]}")
    
    return False


async def main():
    if not config.RENTLIO_API_KEY:
        print("❌ RENTLIO_API_KEY not set in .env")
        return
    
    print(f"🔑 API Key: {config.RENTLIO_API_KEY[:15]}...{config.RENTLIO_API_KEY[-5:]}")
    
    # Test the most promising URLs
    test_urls = [
        ("https://api.rentl.io/v1/properties", "Primary API endpoint"),
        ("https://api.rentl.io/v1/reservations", "Reservations endpoint"),
        ("https://app.rentl.io/api/properties", "App API endpoint"),
    ]
    
    for url, description in test_urls:
        print(f"\n{'='*60}")
        print(f"Testing: {description}")
        print(f"URL: {url}")
        print('='*60)
        
        for auth_format in ["Bearer", "Token", "ApiKey", "X-API-Key", "api-key", "apikey", "Plain"]:
            success = await test_auth_method(url, config.RENTLIO_API_KEY, auth_format)
            if success:
                print(f"\n🎉 WORKING CONFIGURATION FOUND!")
                print(f"   URL: {url}")
                print(f"   Auth Format: {auth_format}")
                return


if __name__ == "__main__":
    asyncio.run(main())
