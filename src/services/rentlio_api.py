"""Rentlio API Client - Async implementation"""
import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from dataclasses import dataclass

from src.config import config

logger = logging.getLogger(__name__)


@dataclass
class RentlioReservation:
    """Reservation data structure"""
    id: str
    reservation_number: str
    guest_name: str
    guest_email: Optional[str]
    guest_phone: Optional[str]
    check_in_date: str
    check_out_date: str
    property_name: str
    unit_name: str
    status: str
    online_checkin_url: Optional[str]
    raw_data: dict


class RentlioAPIError(Exception):
    """Rentlio API Error"""
    def __init__(self, status_code: int, message: str, response_data: Any = None):
        self.status_code = status_code
        self.message = message
        self.response_data = response_data
        super().__init__(f"Rentlio API Error ({status_code}): {message}")


class RentlioAPI:
    """
    Async Rentlio API Client
    
    Documentation: https://docs.rentl.io/
    """
    
    def __init__(self, api_key: str = None, base_url: str = None):
        self.api_key = api_key or config.RENTLIO_API_KEY
        self.base_url = (base_url or config.RENTLIO_API_URL).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
    
    @property
    def headers(self) -> dict:
        """Default headers for API requests"""
        return {
            "apikey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session
    
    async def close(self):
        """Close the session"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _request(
        self, 
        method: str, 
        endpoint: str, 
        params: dict = None, 
        json_data: dict = None
    ) -> dict:
        """Make an API request"""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        
        logger.debug(f"API Request: {method} {url} params={params}")
        
        try:
            async with session.request(
                method=method,
                url=url,
                params=params,
                json=json_data
            ) as response:
                response_data = await response.json()
                
                if response.status >= 400:
                    raise RentlioAPIError(
                        status_code=response.status,
                        message=response_data.get("message", "Unknown error"),
                        response_data=response_data
                    )
                
                logger.debug(f"API Response: {response.status}")
                return response_data
                
        except aiohttp.ClientError as e:
            logger.error(f"HTTP Client Error: {e}")
            raise RentlioAPIError(status_code=0, message=str(e))
    
    # ========== Properties ==========
    
    async def get_properties(self) -> list[dict]:
        """Get all properties"""
        response = await self._request("GET", "/properties")
        return response.get("data", [])
    
    async def get_property(self, property_id: str) -> dict:
        """Get a single property"""
        return await self._request("GET", f"/properties/{property_id}")
    
    # ========== Reservations ==========
    
    async def get_reservations(
        self,
        property_id: str = None,
        date_from: str = None,
        date_to: str = None,
        status: str = None,
        guest_name: str = None,
        limit: int = 100
    ) -> list[dict]:
        """
        Get reservations with filtering
        
        Args:
            property_id: Filter by property
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            status: Filter by status (e.g., 'confirmed', 'checked_in')
            guest_name: Search by guest name
            limit: Maximum results
        """
        params = {"limit": limit}
        
        if property_id:
            params["propertyId"] = property_id
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        if status:
            params["status"] = status
        if guest_name:
            params["guestName"] = guest_name
        
        response = await self._request("GET", "/reservations", params=params)
        return response.get("data", [])
    
    async def get_reservation_details(self, reservation_id: str) -> dict:
        """Get detailed reservation info including guests"""
        return await self._request("GET", f"/reservations/{reservation_id}/details")
    
    async def get_reservation_guests(self, reservation_id: str) -> list[dict]:
        """Get guests for a reservation"""
        response = await self._request("GET", f"/reservations/{reservation_id}/guests")
        return response.get("data", [])
    
    async def checkin_reservation(self, reservation_id: str) -> dict:
        """Mark reservation as checked-in"""
        return await self._request("PUT", f"/reservations/{reservation_id}/checkin")
    
    async def checkout_reservation(self, reservation_id: str) -> dict:
        """Mark reservation as checked-out"""
        return await self._request("PUT", f"/reservations/{reservation_id}/checkout")
    
    # ========== Invoices ==========
    
    async def get_invoices(self, property_id: str = None, limit: int = 100) -> list[dict]:
        """Get invoices - requires propertyId parameter"""
        if not property_id:
            raise ValueError("property_id is required for fetching invoices")
        
        params = {"limit": limit, "propertiesIds": property_id}
        
        response = await self._request("GET", "/invoices", params=params)
        return response.get("data", [])
    
    async def add_invoice_item(
        self,
        reservation_id: str,
        description: str,
        amount: float,
        quantity: int = 1,
        vat_rate: float = 0.0
    ) -> dict:
        """Add item to reservation invoice"""
        return await self._request(
            "POST",
            f"/reservations/{reservation_id}/invoices/items",
            json_data={
                "description": description,
                "amount": amount,
                "quantity": quantity,
                "vatRate": vat_rate
            }
        )
    
    async def add_invoice_items_bulk(
        self,
        reservation_id: str,
        items: list[dict]
    ) -> dict:
        """Add multiple items to reservation invoice"""
        return await self._request(
            "POST",
            f"/reservations/{reservation_id}/invoices/items/bulk",
            json_data={"items": items}
        )
    
    # ========== Checked-in Guests ==========
    
    async def get_checked_in_guests(self, property_id: str, date_from: str = None, date_to: str = None) -> list[dict]:
        """
        Get all currently checked-in guests for a property
        
        Args:
            property_id: Property ID
            date_from: Start date (YYYY-MM-DD) - defaults to today
            date_to: End date (YYYY-MM-DD) - defaults to today
        """
        if not date_from:
            date_from = datetime.now().strftime("%Y-%m-%d")
        if not date_to:
            date_to = datetime.now().strftime("%Y-%m-%d")
        
        response = await self._request(
            "GET", 
            f"/properties/{property_id}/guests/checked-in",
            params={"dateFrom": date_from, "dateTo": date_to}
        )
        return response.get("data", [])
    
    # ========== Helper Methods ==========
    
    async def get_upcoming_arrivals(
        self, 
        property_id: str = None,
        days_ahead: int = 7
    ) -> list[RentlioReservation]:
        """
        Get reservations arriving in the next N days
        
        Returns parsed RentlioReservation objects
        """
        today = datetime.now().strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        
        raw_reservations = await self.get_reservations(
            property_id=property_id,
            date_from=today,
            date_to=future,
            status="confirmed"
        )
        
        return [self._parse_reservation(r) for r in raw_reservations]
    
    def _parse_reservation(self, data: dict) -> RentlioReservation:
        """Parse raw reservation data into structured object"""
        holder = data.get("holder", {})
        
        return RentlioReservation(
            id=str(data.get("id", "")),
            reservation_number=data.get("channelId", ""),
            guest_name=data.get("guestName", holder.get("name", "")),
            guest_email=data.get("guestEmail", holder.get("email")),
            guest_phone=data.get("guestContactNumber", holder.get("contactNumber")),
            check_in_date=self._timestamp_to_date(data.get("arrivalDate", 0)),
            check_out_date=self._timestamp_to_date(data.get("departureDate", 0)),
            property_name=data.get("propertyName", ""),
            unit_name=data.get("unitName", ""),
            status=self._status_code_to_string(data.get("status", 0)),
            online_checkin_url=None,  # Not provided by API
            raw_data=data
        )
    
    @staticmethod
    def _timestamp_to_date(timestamp: int) -> str:
        """Convert Unix timestamp to YYYY-MM-DD"""
        if not timestamp:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    
    @staticmethod
    def _status_code_to_string(status: int) -> str:
        """Convert status code to string"""
        status_map = {
            1: "confirmed",
            2: "tentative",
            3: "cancelled"
        }
        return status_map.get(status, "unknown")


# Singleton instance
rentlio_api = RentlioAPI()
