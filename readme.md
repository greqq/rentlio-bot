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

## Environment Variables
```env
TELEGRAM_BOT_TOKEN=your_token
RENTLIO_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=path_to_json
TELEGRAM_ALLOWED_USERS=123456789  # For notifications
```

## Docker Deployment (Recommended 🐳)

### Prerequisites
- Docker & Docker Compose installed
- GitHub account (for Container Registry)

### 1. Quick Setup on Raspberry Pi
```bash
# Clone and run automated setup
git clone <your-repo-url> ~/rentlio-bot
cd ~/rentlio-bot
./deploy/raspberry-pi-setup.sh
```

The script will:
- Install Docker if needed
- Set up `.env` configuration
- Login to GitHub Container Registry
- Start the bot with auto-update enabled (Watchtower)

### 2. Manual Setup
```bash
# Clone repository
git clone <your-repo-url> ~/rentlio-bot
cd ~/rentlio-bot

# Create credentials directory and add Google Cloud JSON
mkdir -p credentials
# Copy your google-cloud-vision.json here

# Configure environment
cp .env.example .env
nano .env  # Add your tokens/keys

# Login to GitHub Container Registry
docker login ghcr.io -u YOUR_GITHUB_USERNAME

# Start containers
docker-compose up -d
```

### 3. CI/CD Pipeline
Every push to `main` automatically:
1. 🏗️ Builds multi-arch Docker image (amd64, arm64, arm/v7)
2. 📦 Pushes to GitHub Container Registry
3. 🔄 Watchtower on Raspberry Pi pulls & restarts (within 5 min)

**Setup GitHub Container Registry:**
1. Go to Settings → Developer Settings → Personal Access Tokens
2. Create token with `write:packages` permission
3. On Raspberry Pi: `docker login ghcr.io -u YOUR_USERNAME`

### 4. Commands
```bash
# View logs
docker-compose logs -f rentlio-bot

# Check status
docker-compose ps

# Restart bot
docker-compose restart rentlio-bot

# Stop everything
docker-compose down

# Update manually (Watchtower does this automatically)
docker-compose pull && docker-compose up -d
```

---

## Traditional Deployment (Python venv)

<details>
<summary>Click to expand non-Docker deployment</summary>

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

### 3. Run
```bash
source venv/bin/activate
python -m src.bot
```

### 4. Update
```bash
cd ~/rentlio-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
```

</details>
