# Rentlio Automation Telegram Bot

## Project Overview
A Python-based Telegram bot designed to automate the guest check-in process and invoice creation for a private rental host using the Rentlio PMS. The bot processes ID photos via OCR, adds guests directly to reservations via API, and creates non-fiscalized invoices.

## Tech Stack
* **Language:** Python 3.10+
* **Interface:** `python-telegram-bot` (Async)
* **OCR:** Google Cloud Vision API (Text Detection + MRZ parsing)
* **PMS Integration:** Rentlio API (direct guest registration via API)
* **Revenue analysis:** occupancy / pricing engine over 2 seasons of history, with an optional Claude briefing

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

### 4. AI Occupancy & Pricing Analysis (`/analiza`)
* **Question it answers:** which of the next 30 / 60 nights are not selling, and
  what to change - the price, or the minimum stay.
* **Inputs:** the live calendar for both apartments plus the **same calendar
  period in the previous seasons** (2 years by default), read straight from the
  Rentlio API - no extra data entry.
* **What it computes** (deterministic, in `src/services/occupancy_analyzer.py`):
  * night-by-night occupancy per apartment for the horizon;
  * historical demand per date (±3 day window, recent seasons weighted higher)
    and the average price those nights used to sell for;
  * **booking pace** - how much of a night's final occupancy was normally
    already sold N days before arrival. Measured from the account's own booking
    dates when the API exposes them, otherwise a generic pickup curve (the
    report says which);
  * **gaps** - runs of free nights per apartment, flagging the ones stuck
    between two reservations that a 2-night minimum makes unbookable.
* **What it recommends:** discounts stepped by how close the date is (20% inside
  a week, 15% / 10% / 5% further out), minimum-stay changes per gap, weekend
  policy from the weekday history, and where to hold the rate because the period
  is running ahead of previous seasons.
* **AI layer (optional):** with `ANTHROPIC_API_KEY` set, Claude turns the
  computed numbers into a short Croatian briefing ("napravi danas" / "prati").
  It reasons only from the computed report - without a key the bot still prints
  the full rule-based analysis.

```
/analiza          # next 30 days
/analiza 60       # next 60 days
```

Buttons under the report switch the horizon, show the night-by-night calendar,
or force a refresh (results are cached for 30 minutes).

Run it outside Telegram:

```bash
python scripts/analyze_occupancy.py --days 60 --calendar
python scripts/analyze_occupancy.py --demo      # synthetic data, no API key
python scripts/analyze_occupancy.py --json      # the payload the AI layer sees
```

### 5. Daily Notifications
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

## Environment Variables
```env
TELEGRAM_BOT_TOKEN=your_token
RENTLIO_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=path_to_json
TELEGRAM_ALLOWED_USERS=123456789  # For notifications

# Occupancy analysis (/analiza) - all optional
RENTLIO_PROPERTY_ID=          # only if the account has several properties
RENTLIO_TOTAL_UNITS=2         # keeps the occupancy denominator right in quiet periods
ANTHROPIC_API_KEY=            # enables the AI briefing; without it the report is rule-based
ANTHROPIC_MODEL=claude-opus-5
AI_ADVISOR_EFFORT=medium      # low | medium | high
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
