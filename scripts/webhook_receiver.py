#!/usr/bin/env python3
"""
Simple webhook receiver to inspect what Rentlio sends

Run this locally first to test, then we can deploy it properly
"""
from flask import Flask, request, jsonify
import json
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# Store received webhooks for inspection
WEBHOOK_LOG = Path(__file__).parent.parent / "data" / "webhook_log.json"
WEBHOOK_LOG.parent.mkdir(exist_ok=True)


def log_webhook(data: dict):
    """Save webhook data to file for inspection"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "data": data
    }
    
    # Append to log file
    logs = []
    if WEBHOOK_LOG.exists():
        with open(WEBHOOK_LOG, 'r') as f:
            try:
                logs = json.load(f)
            except:
                logs = []
    
    logs.append(log_entry)
    
    # Keep only last 100 entries
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
    """Receive Rentlio webhooks"""
    try:
        # Get the raw data
        data = request.get_json(force=True)
        
        # Log it
        log_webhook(data)
        
        # Look for check-in URL
        if isinstance(data, dict):
            # Search for check-in URL in the payload
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
        
        return jsonify({"status": "success", "message": "Webhook received"}), 200
        
    except Exception as e:
        print(f"❌ Error processing webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/webhook/test', methods=['GET'])
def test():
    """Test endpoint to verify server is running"""
    return jsonify({
        "status": "ok",
        "message": "Rentlio webhook receiver is running",
        "timestamp": datetime.now().isoformat()
    })


@app.route('/webhook/logs', methods=['GET'])
def get_logs():
    """View recent webhook logs"""
    if WEBHOOK_LOG.exists():
        with open(WEBHOOK_LOG, 'r') as f:
            logs = json.load(f)
        return jsonify(logs), 200
    return jsonify([]), 200


if __name__ == '__main__':
    print("🚀 Starting Rentlio Webhook Receiver...")
    print("="*60)
    print("Webhook URL: http://localhost:5000/webhook/rentlio")
    print("Test URL:    http://localhost:5000/webhook/test")
    print("Logs URL:    http://localhost:5000/webhook/logs")
    print("="*60)
    print("\nWaiting for webhooks...\n")
    
    # Run on all interfaces so it can receive external webhooks
    app.run(host='0.0.0.0', port=5000, debug=True)
