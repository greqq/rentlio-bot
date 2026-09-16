# Rentlio Automation Telegram Bot

## Project Overview
A Python-based Telegram bot designed to automate the guest check-in process and invoice creation for a private rental host using the Rentlio PMS. The bot processes ID photos via OCR, adds guests directly to reservations via API, and creates non-fiscalized invoices.

## Tech Stack
* **Language:** Python 3.10+
* **Interface:** `python-telegram-bot` (Async)
* **OCR:** Google Cloud Vision API (Text Detection + MRZ parsing)
* **PMS Integration:** Rentlio API (direct guest registration via API)

## Key Features

### 1. ID Scanning & OCR
* **Input:** User sends a photo of an ID or Passport to the Telegram bot.
* **Processing:** The bot sends the image to Google Cloud Vision API.
* **Extraction:** Priority is given to **MRZ (Machine Readable Zone)** parsing for high accuracy.
* **Data Extracted:** First Name, Last Name, Date of Birth, Document Number, Nationality (ISO code), Gender.

### 2. Direct API Check-in
* **Mechanism:** Bot uses `POST /reservations-guests/{id}` to add guests directly to Rentlio.
* **No form filling needed!** Guest data is pushed via API.
* **Country Mapping:** Automatic mapping from ISO codes to Rentlio country IDs.

### 3. Invoice Generation (Non-Fiscalized)
* **Trigger:** After check-in, bot offers to create an invoice.
* **Logic:** For private renters ("paušalist"), **no fiscalization (ZKI/JIR)** required.
* **Auto-detection:** Payment type based on booking channel (OTA vs direct).
* **Output:** Invoice created in Rentlio (Draft status).

### 4. Daily Notifications
* **Scheduled:** Every day at 8:00 AM.
* **Content:** Today's check-ins, today's check-outs, tomorrow's arrivals (reminder to send instructions).
* **Smart:** Only sends if there's activity - no spam on quiet days.

## Workflow

1. 📷 **User sends ID photos** to bot
2. 🔍 **Bot extracts data** via OCR (Google Cloud Vision)
3. ✅ **User clicks "Nastavi"** when done adding guests
4. 📋 **Bot shows upcoming reservations** to select from
5. 🚀 **Bot adds guests** directly to Rentlio via API
6. 🧾 **Optional:** Create invoice for the reservation
7. 🗑️ **Cleanup:** Photos deleted for GDPR compliance

### 5. Self Check-in Form (guest photographs their own ID)

Rentlio's own online check-in link cannot be read through the API (verified:
grepping the raw text of every response for a known check-in UUID finds
nothing, and all five `checkin-url` endpoint variants return 404), so the bot
serves its own form.

* **Two ways in:**
  * `{PUBLIC_BASE_URL}/checkin/<token>` — one reservation, link sent to the guest
  * `{PUBLIC_BASE_URL}/checkin` — no token; the guest types surname + arrival
    date. Static, so it can go in a Rentlio message, the WhatsApp Business
    greeting, or a QR code on the door.
* **The guest photographs, they do not type.** The page opens the camera
  directly (`capture="environment"`), the same Google Vision OCR the bot
  already uses reads the document, and the guest only confirms or corrects
  what was read.
* **Nothing is written to Rentlio without approval.** A confirmed submission
  is parked in SQLite and the owner gets a Telegram card with
  ✅ Prihvati / ❌ Odbij. Only ✅ triggers the write.
* **Delivery:** `/link` lists arrivals in the next 7 days, says which ones
  still have data missing, and offers a `wa.me` button that opens WhatsApp
  with the message already written — one tap instead of finding the
  reservation in Rentlio's app. The same cards come with the 8:00
  notification for tomorrow's arrivals.
* **Numbers are never guessed.** A `wa.me` button appears only when the
  number carries its own country code (`+385…`, `00385…`). A bare `091…`
  could be any country, and sending a check-in link to a stranger is worse
  than sending it by hand.
* **The image never touches disk.** It lives in memory for the length of the
  OCR call and is dropped immediately after.
* **State survives a redeploy.** Tokens and pending submissions are in
  SQLite (`DB_PATH`), so a restart mid-flow no longer loses anything. Use
  `/pending` to re-send cards still awaiting a decision.

New commands: `/link`, `/pending`.

## Environment Variables
```env
TELEGRAM_BOT_TOKEN=your_token
RENTLIO_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=path_to_json
TELEGRAM_ALLOWED_USERS=123456789  # For notifications and approvals

# Self check-in form
PUBLIC_BASE_URL=https://sun-apartments.co  # no trailing slash
WEB_ENABLED=true
WEB_PORT=8080
DB_PATH=/app/data/bot.sqlite3   # mount this path, or tokens die on redeploy
CHECKIN_TOKEN_TTL_DAYS=30
```

See `.env.example` for the full list.

## Docker Deployment

```bash
docker compose pull && docker compose up -d
```

The form listens on `WEB_PORT`, bound to `127.0.0.1` by compose on purpose —
it is reached through the Cloudflare tunnel, never straight off the internet.

### Cloudflare tunnel

With `cloudflared` on the host, in `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: sun-apartments.co
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Then `cloudflared tunnel route dns <tunnel-id> sun-apartments.co`.

If `cloudflared` runs as a container on the same compose network instead,
drop the `ports:` block from `docker-compose.yml` and point the ingress at
`http://rentlio-bot:8080`.

Check it came up:

```bash
curl -s localhost:8080/healthz          # {"ok": true}
curl -s https://sun-apartments.co/healthz
```

## Raspberry Pi Deployment

### 1. Clone & Setup
```bash
cd ~
git clone <repo-url> rentlio-bot
cd rentlio-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure
```bash
# Copy and edit environment file
cp .env.example .env
nano .env

# Add your Google Cloud credentials JSON
# Set GOOGLE_APPLICATION_CREDENTIALS to point to it
```

### 3. Install as Service
```bash
# Copy service file
sudo cp rentlio-bot.service /etc/systemd/system/

# Edit if your user isn't 'pi' or path is different
sudo nano /etc/systemd/system/rentlio-bot.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable rentlio-bot
sudo systemctl start rentlio-bot

# Check status
sudo systemctl status rentlio-bot

# View logs
journalctl -u rentlio-bot -f
```

### 4. Update
```bash
cd ~/rentlio-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart rentlio-bot
```
