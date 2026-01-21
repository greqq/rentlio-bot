#!/usr/bin/env python3
"""
SQLite Database Service for Rentlio Bot

Stores:
- Reservations with check-in URLs
- Processed guests (from ID photos)
- Webhook events log

Perfect for Raspberry Pi - lightweight, no server needed!
"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import threading

# Database file location
DB_PATH = Path(__file__).parent.parent.parent / "data" / "rentlio.db"
DB_PATH.parent.mkdir(exist_ok=True)

# Thread-local storage for connections
_local = threading.local()


@dataclass
class Reservation:
    """Reservation data from webhook or API"""
    id: str
    guest_name: str
    guest_email: Optional[str]
    guest_phone: Optional[str]
    unit_name: str
    unit_id: Optional[str]
    arrival_date: datetime
    departure_date: datetime
    adults: int
    children: int
    checkin_url: Optional[str]
    checkin_token: Optional[str]  # UUID from the URL
    status: str  # pending, checked_in, checked_out, cancelled
    channel: Optional[str]  # Booking.com, Airbnb, Direct, etc.
    total_price: float
    currency: str
    note: Optional[str]
    raw_data: Optional[str]  # JSON dump of full webhook payload
    created_at: datetime
    updated_at: datetime
    
    @classmethod
    def from_webhook(cls, data: dict) -> 'Reservation':
        """Create Reservation from Rentlio webhook payload"""
        now = datetime.now()
        
        # Extract check-in URL and token
        checkin_url = data.get('onlineCheckInUrl') or data.get('online_checkin_url') or data.get('checkinUrl')
        checkin_token = None
        if checkin_url:
            # Extract UUID from URL: ci.book.rentl.io/c/{uuid}/{code}
            import re
            match = re.search(r'/c/([a-f0-9-]+)/', checkin_url)
            if match:
                checkin_token = match.group(1)
        
        # Parse dates - could be timestamp or ISO string
        arrival = data.get('arrivalDate') or data.get('arrival_date') or data.get('checkIn')
        departure = data.get('departureDate') or data.get('departure_date') or data.get('checkOut')
        
        def parse_date(val):
            if isinstance(val, int):
                return datetime.fromtimestamp(val)
            elif isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace('Z', '+00:00'))
                except:
                    return datetime.strptime(val[:10], '%Y-%m-%d')
            return now
        
        return cls(
            id=str(data.get('id') or data.get('reservationId') or data.get('reservation_id', '')),
            guest_name=data.get('guestName') or data.get('guest_name') or data.get('name', 'Unknown'),
            guest_email=data.get('guestEmail') or data.get('guest_email') or data.get('email'),
            guest_phone=data.get('guestContactNumber') or data.get('guest_phone') or data.get('phone'),
            unit_name=data.get('unitName') or data.get('unit_name') or data.get('propertyName', ''),
            unit_id=str(data.get('unitId') or data.get('unit_id', '')),
            arrival_date=parse_date(arrival),
            departure_date=parse_date(departure),
            adults=data.get('adults', 1),
            children=data.get('children', 0) or (data.get('childrenUnder12', 0) + data.get('childrenAbove12', 0)),
            checkin_url=checkin_url,
            checkin_token=checkin_token,
            status='pending',
            channel=data.get('otaChannelName') or data.get('channel') or data.get('source'),
            total_price=float(data.get('totalPrice', 0) or 0),
            currency=data.get('currency', 'EUR'),
            note=data.get('note') or data.get('notes'),
            raw_data=json.dumps(data),
            created_at=now,
            updated_at=now
        )


@dataclass 
class ProcessedGuest:
    """Guest data extracted from ID photo via OCR"""
    id: Optional[int]
    reservation_id: Optional[str]  # Linked reservation
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    date_of_birth: Optional[str]
    gender: Optional[str]
    nationality: Optional[str]
    document_type: Optional[str]
    document_number: Optional[str]
    document_expiry: Optional[str]
    place_of_residence: Optional[str]
    raw_ocr_data: Optional[str]
    created_at: datetime
    
    def to_form_dict(self) -> dict:
        """Convert to dictionary for form filling"""
        return {
            'fullName': self.full_name,
            'firstName': self.first_name,
            'lastName': self.last_name,
            'dateOfBirth': self.date_of_birth,
            'gender': self.gender,
            'nationality': self.nationality,
            'documentType': self.document_type,
            'documentNumber': self.document_number,
            'documentExpiry': self.document_expiry,
            'placeOfResidence': self.place_of_residence,
        }


class Database:
    """SQLite database manager for Rentlio bot"""
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection"""
        if not hasattr(_local, 'connection') or _local.connection is None:
            _local.connection = sqlite3.connect(
                str(self.db_path),
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            )
            _local.connection.row_factory = sqlite3.Row
        return _local.connection
    
    @contextmanager
    def _cursor(self):
        """Context manager for database cursor with auto-commit"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    def _init_db(self):
        """Initialize database schema"""
        with self._cursor() as cursor:
            # Reservations table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY,
                    guest_name TEXT NOT NULL,
                    guest_email TEXT,
                    guest_phone TEXT,
                    unit_name TEXT,
                    unit_id TEXT,
                    arrival_date TIMESTAMP,
                    departure_date TIMESTAMP,
                    adults INTEGER DEFAULT 1,
                    children INTEGER DEFAULT 0,
                    checkin_url TEXT,
                    checkin_token TEXT,
                    status TEXT DEFAULT 'pending',
                    channel TEXT,
                    total_price REAL DEFAULT 0,
                    currency TEXT DEFAULT 'EUR',
                    note TEXT,
                    raw_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Processed guests table (from OCR)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_guests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id TEXT,
                    full_name TEXT NOT NULL,
                    first_name TEXT,
                    last_name TEXT,
                    date_of_birth TEXT,
                    gender TEXT,
                    nationality TEXT,
                    document_type TEXT,
                    document_number TEXT,
                    document_expiry TEXT,
                    place_of_residence TEXT,
                    raw_ocr_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (reservation_id) REFERENCES reservations(id)
                )
            ''')
            
            # Webhook events log
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    reservation_id TEXT,
                    payload TEXT,
                    processed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for faster queries
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reservations_arrival ON reservations(arrival_date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reservations_token ON reservations(checkin_token)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_guests_reservation ON processed_guests(reservation_id)')
    
    # ========== Reservation Methods ==========
    
    def upsert_reservation(self, res: Reservation) -> None:
        """Insert or update a reservation"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO reservations (
                    id, guest_name, guest_email, guest_phone, unit_name, unit_id,
                    arrival_date, departure_date, adults, children, checkin_url,
                    checkin_token, status, channel, total_price, currency, note,
                    raw_data, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    guest_name = excluded.guest_name,
                    guest_email = excluded.guest_email,
                    guest_phone = excluded.guest_phone,
                    unit_name = excluded.unit_name,
                    checkin_url = COALESCE(excluded.checkin_url, checkin_url),
                    checkin_token = COALESCE(excluded.checkin_token, checkin_token),
                    status = excluded.status,
                    total_price = excluded.total_price,
                    note = excluded.note,
                    raw_data = excluded.raw_data,
                    updated_at = CURRENT_TIMESTAMP
            ''', (
                res.id, res.guest_name, res.guest_email, res.guest_phone,
                res.unit_name, res.unit_id, res.arrival_date, res.departure_date,
                res.adults, res.children, res.checkin_url, res.checkin_token,
                res.status, res.channel, res.total_price, res.currency, res.note,
                res.raw_data, res.created_at, res.updated_at
            ))
    
    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        """Get a reservation by ID"""
        with self._cursor() as cursor:
            cursor.execute('SELECT * FROM reservations WHERE id = ?', (reservation_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_reservation(row)
        return None
    
    def get_reservation_by_token(self, token: str) -> Optional[Reservation]:
        """Get a reservation by check-in token (UUID from URL)"""
        with self._cursor() as cursor:
            cursor.execute('SELECT * FROM reservations WHERE checkin_token = ?', (token,))
            row = cursor.fetchone()
            if row:
                return self._row_to_reservation(row)
        return None
    
    def get_upcoming_reservations(self, days: int = 7) -> List[Reservation]:
        """Get reservations arriving in next N days"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        future = today + timedelta(days=days)
        
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT * FROM reservations 
                WHERE arrival_date >= ? AND arrival_date <= ?
                AND status != 'cancelled'
                ORDER BY arrival_date ASC
            ''', (today, future))
            return [self._row_to_reservation(row) for row in cursor.fetchall()]
    
    def get_pending_checkins(self) -> List[Reservation]:
        """Get reservations that need check-in (arriving today/tomorrow, not checked in)"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = today + timedelta(days=2)
        
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT * FROM reservations 
                WHERE arrival_date >= ? AND arrival_date < ?
                AND status = 'pending'
                AND checkin_url IS NOT NULL
                ORDER BY arrival_date ASC
            ''', (today, tomorrow_end))
            return [self._row_to_reservation(row) for row in cursor.fetchall()]
    
    def update_reservation_status(self, reservation_id: str, status: str) -> None:
        """Update reservation status"""
        with self._cursor() as cursor:
            cursor.execute('''
                UPDATE reservations 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (status, reservation_id))
    
    def search_reservations(self, query: str, limit: int = 10) -> List[Reservation]:
        """Search reservations by guest name"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT * FROM reservations 
                WHERE guest_name LIKE ?
                ORDER BY arrival_date DESC
                LIMIT ?
            ''', (f'%{query}%', limit))
            return [self._row_to_reservation(row) for row in cursor.fetchall()]
    
    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        """Convert database row to Reservation object"""
        return Reservation(
            id=row['id'],
            guest_name=row['guest_name'],
            guest_email=row['guest_email'],
            guest_phone=row['guest_phone'],
            unit_name=row['unit_name'],
            unit_id=row['unit_id'],
            arrival_date=row['arrival_date'],
            departure_date=row['departure_date'],
            adults=row['adults'],
            children=row['children'],
            checkin_url=row['checkin_url'],
            checkin_token=row['checkin_token'],
            status=row['status'],
            channel=row['channel'],
            total_price=row['total_price'],
            currency=row['currency'],
            note=row['note'],
            raw_data=row['raw_data'],
            created_at=row['created_at'],
            updated_at=row['updated_at']
        )
    
    # ========== Processed Guest Methods ==========
    
    def add_processed_guest(self, guest: ProcessedGuest) -> int:
        """Add a processed guest from OCR"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO processed_guests (
                    reservation_id, full_name, first_name, last_name,
                    date_of_birth, gender, nationality, document_type,
                    document_number, document_expiry, place_of_residence,
                    raw_ocr_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                guest.reservation_id, guest.full_name, guest.first_name,
                guest.last_name, guest.date_of_birth, guest.gender,
                guest.nationality, guest.document_type, guest.document_number,
                guest.document_expiry, guest.place_of_residence,
                guest.raw_ocr_data, guest.created_at
            ))
            return cursor.lastrowid
    
    def link_guest_to_reservation(self, guest_id: int, reservation_id: str) -> None:
        """Link a processed guest to a reservation"""
        with self._cursor() as cursor:
            cursor.execute('''
                UPDATE processed_guests 
                SET reservation_id = ?
                WHERE id = ?
            ''', (reservation_id, guest_id))
    
    def get_guests_for_reservation(self, reservation_id: str) -> List[ProcessedGuest]:
        """Get all processed guests for a reservation"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT * FROM processed_guests 
                WHERE reservation_id = ?
                ORDER BY created_at ASC
            ''', (reservation_id,))
            return [self._row_to_guest(row) for row in cursor.fetchall()]
    
    def get_recent_unlinked_guests(self, limit: int = 5) -> List[ProcessedGuest]:
        """Get recently processed guests not yet linked to a reservation"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT * FROM processed_guests 
                WHERE reservation_id IS NULL
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))
            return [self._row_to_guest(row) for row in cursor.fetchall()]
    
    def _row_to_guest(self, row: sqlite3.Row) -> ProcessedGuest:
        """Convert database row to ProcessedGuest object"""
        return ProcessedGuest(
            id=row['id'],
            reservation_id=row['reservation_id'],
            full_name=row['full_name'],
            first_name=row['first_name'],
            last_name=row['last_name'],
            date_of_birth=row['date_of_birth'],
            gender=row['gender'],
            nationality=row['nationality'],
            document_type=row['document_type'],
            document_number=row['document_number'],
            document_expiry=row['document_expiry'],
            place_of_residence=row['place_of_residence'],
            raw_ocr_data=row['raw_ocr_data'],
            created_at=row['created_at']
        )
    
    # ========== Webhook Event Methods ==========
    
    def log_webhook_event(self, event_type: str, payload: dict, reservation_id: str = None) -> int:
        """Log a webhook event"""
        with self._cursor() as cursor:
            cursor.execute('''
                INSERT INTO webhook_events (event_type, reservation_id, payload, created_at)
                VALUES (?, ?, ?, ?)
            ''', (event_type, reservation_id, json.dumps(payload), datetime.now()))
            return cursor.lastrowid
    
    def mark_webhook_processed(self, event_id: int) -> None:
        """Mark a webhook event as processed"""
        with self._cursor() as cursor:
            cursor.execute('UPDATE webhook_events SET processed = 1 WHERE id = ?', (event_id,))
    
    def get_unprocessed_webhooks(self, limit: int = 100) -> List[Dict]:
        """Get unprocessed webhook events"""
        with self._cursor() as cursor:
            cursor.execute('''
                SELECT id, event_type, reservation_id, payload, created_at 
                FROM webhook_events 
                WHERE processed = 0
                ORDER BY created_at ASC
                LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    # ========== Stats Methods ==========
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        with self._cursor() as cursor:
            stats = {}
            
            cursor.execute('SELECT COUNT(*) FROM reservations')
            stats['total_reservations'] = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM reservations WHERE status = 'pending'")
            stats['pending_checkins'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM reservations WHERE checkin_url IS NOT NULL')
            stats['with_checkin_url'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM processed_guests')
            stats['processed_guests'] = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM webhook_events')
            stats['webhook_events'] = cursor.fetchone()[0]
            
            return stats


# Global database instance
db = Database()


# ========== Convenience Functions ==========

def store_reservation_from_webhook(data: dict) -> Reservation:
    """Store a reservation from webhook data"""
    res = Reservation.from_webhook(data)
    db.upsert_reservation(res)
    return res

def get_pending_checkins_for_bot() -> List[Dict]:
    """Get pending check-ins formatted for bot display"""
    reservations = db.get_pending_checkins()
    return [
        {
            'id': r.id,
            'guest_name': r.guest_name,
            'unit_name': r.unit_name,
            'arrival_date': r.arrival_date.strftime('%d.%m.%Y'),
            'departure_date': r.departure_date.strftime('%d.%m.%Y'),
            'adults': r.adults,
            'children': r.children,
            'checkin_url': r.checkin_url,
            'phone': r.guest_phone,
            'email': r.guest_email,
            'channel': r.channel,
        }
        for r in reservations
    ]


if __name__ == '__main__':
    # Test the database
    print(f"📦 Database path: {DB_PATH}")
    print(f"📊 Stats: {db.get_stats()}")
    
    # Test with sample data
    sample_webhook = {
        'id': 'TEST-001',
        'guestName': 'Test Guest',
        'guestEmail': 'test@example.com',
        'guestContactNumber': '+385 91 123 4567',
        'unitName': 'Apartment 1',
        'arrivalDate': int(datetime.now().timestamp()),
        'departureDate': int((datetime.now() + timedelta(days=3)).timestamp()),
        'adults': 2,
        'children': 0,
        'onlineCheckInUrl': 'https://ci.book.rentl.io/c/abc-123-def/12345',
        'totalPrice': 450.00,
        'otaChannelName': 'Booking.com'
    }
    
    res = store_reservation_from_webhook(sample_webhook)
    print(f"✅ Stored test reservation: {res.id} - {res.guest_name}")
    print(f"🔗 Check-in URL: {res.checkin_url}")
    print(f"🎫 Check-in token: {res.checkin_token}")
    
    # Retrieve it back
    retrieved = db.get_reservation('TEST-001')
    print(f"📥 Retrieved: {retrieved.guest_name}")
    
    print(f"\n📊 Updated stats: {db.get_stats()}")
