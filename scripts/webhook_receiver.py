#!/usr/bin/env python3
"""
Rentlio Webhook Receiver Server

Receives webhook events from Rentlio and stores them in SQLite database.
Run this on your Raspberry Pi!

Features:
- Receives and parses Rentlio webhooks
- Stores reservations with check-in URLs in database
- Sends Telegram notifications for new reservations
- Logs all webhook events for debugging

Usage:
    python webhook_receiver.py

For production on Raspberry Pi, use gunicorn:
    gunicorn -w 1 -b 0.0.0.0:5000 scripts.webhook_receiver:app
"""
import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.database import db, Reservation, store_reservation_from_webhook

# Optional: Telegram notifications
try:
    import requests as req_lib
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Store received webhooks for inspection (legacy - keeping for backwards compatibility)
WEBHOOK_LOG = Path(__file__).parent.parent / "data" / "webhook_log.json"
WEBHOOK_LOG.parent.mkdir(exist_ok=True)

# Configuration from environment
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')  # Your user ID for notifications
WEBHOOK_SECRET = os.getenv('RENTLIO_WEBHOOK_SECRET')  # Optional: for verifying webhooks


def send_telegram_notification(message: str):
    """Send notification to Telegram (if configured)"""
    if not TELEGRAM_AVAILABLE or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram notifications not configured, skipping")
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = req_lib.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown'
        }, timeout=10)
        
        if response.status_code == 200:
            logger.info("Telegram notification sent successfully")
        else:
            logger.warning(f"Telegram notification failed: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


def format_reservation_notification(res: Reservation) -> str:
    """Format a reservation for Telegram notification"""
    arrival = res.arrival_date.strftime('%d.%m.%Y') if res.arrival_date else 'N/A'
    departure = res.departure_date.strftime('%d.%m.%Y') if res.departure_date else 'N/A'
    
    text = f"""
🆕 **Nova rezervacija!**

👤 {res.guest_name}
🏠 {res.unit_name}
📅 {arrival} → {departure}
👥 {res.adults} odraslih{f' + {res.children} djece' if res.children else ''}
💰 {res.total_price:.0f} {res.currency}
📱 {res.channel or 'Direct'}
"""
    
    if res.checkin_url:
        text += f"\n🔗 Check-in URL spremljen!"
    
    if res.guest_phone:
        text += f"\n📞 {res.guest_phone}"
    
    return text.strip()


def extract_event_type(data: dict) -> str:
    """
    Try to determine the webhook event type.
    
    Rentlio format:
    {
        "token": "...",
        "event": {
            "type": "reservation-created",  <-- this is what we want
            "id": "uuid",
            "payload": { ... }
        }
    }
    """
    # Rentlio puts event info in data['event']['type']
    if 'event' in data and isinstance(data['event'], dict):
        return data['event'].get('type', 'unknown')
    
    # Fallback for other formats
    event_type = data.get('eventType') or data.get('type')
    if event_type:
        return event_type
    
    if 'reservation' in data:
        return 'reservation-created'
    if 'cancellation' in str(data).lower():
        return 'reservation-canceled'
    
    return 'unknown'


def extract_reservation_data(data: dict) -> dict:
    """
    Extract reservation data from webhook payload.
    
    Rentlio format: data['event']['payload'] contains the reservation
    """
    # Rentlio format: event.payload
    if 'event' in data and isinstance(data['event'], dict):
        payload = data['event'].get('payload', {})
        if payload:
            return payload
    
    # Fallback for other formats
    if 'reservation' in data:
        return data['reservation']
    if 'data' in data:
        return data['data']
    if 'payload' in data:
        return data['payload']
    return data


def log_webhook(data: dict):
    """Save webhook data to file for inspection (legacy)"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "data": data
    }
    
    logs = []
    if WEBHOOK_LOG.exists():
        with open(WEBHOOK_LOG, 'r') as f:
            try:
                logs = json.load(f)
            except:
                logs = []
    
    logs.append(log_entry)
    logs = logs[-100:]
    
    with open(WEBHOOK_LOG, 'w') as f:
        json.dump(logs, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"🎣 WEBHOOK RECEIVED at {log_entry['timestamp']}")
    print(f"{'='*60}")
    print(json.dumps(data, indent=2))
    print(f"{'='*60}\n")


@app.route('/webhook/rentlio', methods=['POST'])
def rentlio_webhook():
    """
    Main webhook endpoint for Rentlio
    
    Expected payload structure (may vary):
    {
        "event": "reservation.created",
        "reservation": {
            "id": "12345",
            "guestName": "John Doe",
            "arrivalDate": 1672531200,
            "departureDate": 1672876800,
            "onlineCheckInUrl": "https://ci.book.rentl.io/c/uuid/12345",
            ...
        }
    }
    
    Rentlio sends token IN THE BODY, not in headers:
    {
        "token": "your-secret-token",
        "event": {
            "type": "reservation-created",
            "id": "uuid",
            "payload": { ... }
        }
    }
    """
    try:
        # Parse JSON payload first
        data = request.get_json(force=True)
        
        # Verify webhook secret if configured
        # Rentlio sends token IN THE BODY, not in headers!
        if WEBHOOK_SECRET:
            received_token = data.get('token', '')
            if received_token != WEBHOOK_SECRET:
                logger.warning(f"Invalid webhook token. Expected: {WEBHOOK_SECRET}, Got: {received_token}")
                return jsonify({'error': 'Unauthorized'}), 401
        data = request.get_json(force=True)
        
        # Log the raw webhook (legacy file log)
        log_webhook(data)
        
        # Determine event type
        event_type = extract_event_type(data)
        logger.info(f"Event type: {event_type}")
        
        # Log webhook event to database
        event_id = db.log_webhook_event(event_type, data)
        
        # Extract reservation data
        reservation_data = extract_reservation_data(data)
        
        # Look for check-in URL in the payload (for debugging)
        def find_checkin_url(obj, path=""):
            """Recursively search for check-in URL"""
            findings = []
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if 'checkin' in key.lower() or 'check_in' in key.lower() or 'url' in key.lower():
                        findings.append(f"{path}.{key}: {value}")
                    findings.extend(find_checkin_url(value, f"{path}.{key}"))
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    findings.extend(find_checkin_url(item, f"{path}[{i}]"))
            return findings
        
        findings = find_checkin_url(data)
        if findings:
            print("🔗 Potential check-in URL fields found:")
            for f in findings:
                print(f"   {f}")
        
        # Handle different event types
        if 'cancel' in event_type.lower():
            res_id = str(reservation_data.get('id') or reservation_data.get('reservationId', ''))
            if res_id:
                db.update_reservation_status(res_id, 'cancelled')
                logger.info(f"Marked reservation {res_id} as cancelled")
                send_telegram_notification(f"❌ **Rezervacija otkazana**\n\nID: `{res_id}`")
        
        elif 'checkin' in event_type.lower() or 'check_in' in event_type.lower():
            res_id = str(reservation_data.get('id') or reservation_data.get('reservationId', ''))
            if res_id:
                db.update_reservation_status(res_id, 'checked_in')
                logger.info(f"Marked reservation {res_id} as checked in")
                guest_name = reservation_data.get('guestName', 'Unknown')
                send_telegram_notification(f"✅ **Check-in obavljen**\n\n👤 {guest_name}\nID: `{res_id}`")
        
        elif 'checkout' in event_type.lower() or 'check_out' in event_type.lower():
            res_id = str(reservation_data.get('id') or reservation_data.get('reservationId', ''))
            if res_id:
                db.update_reservation_status(res_id, 'checked_out')
                logger.info(f"Marked reservation {res_id} as checked out")
        
        else:
            # New or updated reservation - store in database
            res = store_reservation_from_webhook(reservation_data)
            logger.info(f"Stored reservation: {res.id} - {res.guest_name}")
            
            # Send notification if check-in URL is present
            if res.checkin_url:
                send_telegram_notification(format_reservation_notification(res))
        
        # Mark webhook as processed
        db.mark_webhook_processed(event_id)
        
        return jsonify({
            'status': 'success',
            'message': 'Webhook processed',
            'event_type': event_type
        }), 200
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/webhook/test', methods=['GET', 'POST'])
def test():
    """Test endpoint to verify server is running"""
    if request.method == 'POST':
        data = request.get_json(force=True) if request.is_json else {}
        logger.info(f"Test webhook received: {data}")
        return jsonify({'status': 'ok', 'received': data})
    
    return jsonify({
        "status": "ok",
        "message": "Rentlio webhook receiver is running",
        "timestamp": datetime.now().isoformat(),
        "stats": db.get_stats()
    })


@app.route('/webhook/logs', methods=['GET'])
def get_logs():
    """View recent webhook logs"""
    if WEBHOOK_LOG.exists():
        with open(WEBHOOK_LOG, 'r') as f:
            logs = json.load(f)
        return jsonify(logs), 200
    return jsonify([]), 200


@app.route('/reservations', methods=['GET'])
def list_reservations():
    """API endpoint to list upcoming reservations"""
    days = request.args.get('days', 7, type=int)
    reservations = db.get_upcoming_reservations(days=days)
    
    return jsonify({
        'count': len(reservations),
        'reservations': [
            {
                'id': r.id,
                'guest_name': r.guest_name,
                'unit_name': r.unit_name,
                'arrival_date': r.arrival_date.isoformat() if r.arrival_date else None,
                'departure_date': r.departure_date.isoformat() if r.departure_date else None,
                'status': r.status,
                'has_checkin_url': r.checkin_url is not None,
                'channel': r.channel
            }
            for r in reservations
        ]
    })


@app.route('/reservations/pending', methods=['GET'])
def pending_checkins():
    """Get reservations pending check-in (with URLs)"""
    reservations = db.get_pending_checkins()
    
    return jsonify({
        'count': len(reservations),
        'reservations': [
            {
                'id': r.id,
                'guest_name': r.guest_name,
                'unit_name': r.unit_name,
                'arrival_date': r.arrival_date.strftime('%d.%m.%Y') if r.arrival_date else None,
                'adults': r.adults,
                'children': r.children,
                'checkin_url': r.checkin_url,
                'phone': r.guest_phone
            }
            for r in reservations
        ]
    })


@app.route('/stats', methods=['GET'])
def stats():
    """Get database statistics"""
    return jsonify(db.get_stats())


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })


def test_local_webhook():
    """Send a test webhook to local server"""
    import requests
    
    test_data = {
        'event': 'reservation.created',
        'reservation': {
            'id': f'TEST-{datetime.now().strftime("%H%M%S")}',
            'guestName': 'Test Gost',
            'guestEmail': 'test@example.com',
            'guestContactNumber': '+385 91 123 4567',
            'unitName': 'Apartment Sunce',
            'arrivalDate': int(datetime.now().timestamp()),
            'departureDate': int(datetime.now().timestamp()) + 86400 * 3,
            'adults': 2,
            'children': 1,
            'totalPrice': 450.00,
            'currency': 'EUR',
            'otaChannelName': 'Booking.com',
            'onlineCheckInUrl': 'https://ci.book.rentl.io/c/test-uuid-123/12345',
            'note': 'Test reservation via webhook'
        }
    }
    
    print("📤 Sending test webhook...")
    response = requests.post(
        'http://localhost:5000/webhook/rentlio',
        json=test_data,
        headers={'Content-Type': 'application/json'}
    )
    
    print(f"📥 Response: {response.status_code}")
    print(json.dumps(response.json(), indent=2))


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Rentlio Webhook Receiver')
    parser.add_argument('--test', action='store_true', help='Send a test webhook')
    parser.add_argument('--port', type=int, default=5000, help='Port to run on')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    
    args = parser.parse_args()
    
    if args.test:
        test_local_webhook()
    else:
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║           🎣 Rentlio Webhook Receiver                        ║
╠══════════════════════════════════════════════════════════════╣
║  Server starting on http://{args.host}:{args.port}                    ║
║                                                              ║
║  Endpoints:                                                  ║
║    POST /webhook/rentlio  - Receive Rentlio webhooks         ║
║    GET  /webhook/test     - Check if server is running       ║
║    GET  /reservations     - List upcoming reservations       ║
║    GET  /reservations/pending - Pending check-ins            ║
║    GET  /stats            - Database statistics              ║
║    GET  /health           - Health check                     ║
║                                                              ║
║  For ngrok tunnel (development):                             ║
║    ngrok http {args.port}                                          ║
║                                                              ║
║  For production (Raspberry Pi):                              ║
║    gunicorn -w 1 -b 0.0.0.0:{args.port} webhook_receiver:app       ║
╚══════════════════════════════════════════════════════════════╝
""")
        app.run(host=args.host, port=args.port, debug=True)
