# Rentlio Automation Telegram Bot

## Project Overview
A Python-based Telegram bot designed to automate the guest check-in process and invoice creation for a private rental host using the Rentlio PMS. The bot processes ID photos, matches them to reservations, performs online check-ins, and issues non-fiscalized invoices.

## Tech Stack
* **Language:** Python 3.10+
* **Interface:** `python-telegram-bot` (Async)
* **OCR:** Google Cloud Vision API (Text Detection / Document Text Detection)
* **PMS Integration:** Rentlio API + Webhooks
* **Browser Automation:** Playwright (for filling the Online Check-in form)
* **Database:** SQLite or Redis (for temporary state management)

## Key Features

### 1. ID Scanning & OCR
* **Input:** User sends a photo of an ID or Passport to the Telegram bot.
* **Processing:** The bot sends the image to Google Cloud Vision API.
* **Extraction:** Priority is given to **MRZ (Machine Readable Zone)** parsing for 100% accuracy.
* **Data Extracted:** First Name, Last Name, Date of Birth, Document Number, Nationality (ISO code), Gender.

### 2. Reservation Matching
* **Logic:** The bot queries Rentlio API for upcoming arrivals (`GET /reservations`).
* **Matching:** Uses Fuzzy String Matching to compare the name on the ID with the guest name on the reservation.
* **Interaction:** Bot asks the user to confirm the match via Inline Buttons.

### 3. Automated Check-in
* **Mechanism:** Since the public API may not support updating guest details directly, the bot retrieves the `online_checkin_url` from the reservation details.
* **Action:** Uses **Playwright (Headless)** to open the link, fill in the extracted OCR data into the web form, and submit it.

### 4. Invoice Generation (Non-Fiscalized)
* **Trigger:** After a successful check-in, the bot offers to create an invoice.
* **Logic:** Since the host is a private renter ("paušalist"), **no fiscalization (ZKI/JIR)** is required.
* **Payload:**
    * Payment Type: `TRANSACTION_ACCOUNT` (if prepaid/booking) or `CASH`.
    * **Mandatory Note:** "Oslobođeno plaćanja PDV-a temeljem članka 90. stavka 2. Zakona o PDV-u."
* **Output:** The bot returns the generated PDF invoice to the chat.

## Workflow

1.  **Webhook Event:** Rentlio sends `reservation_created` -> Bot notifies User.
2.  **User Action:** User forwards ID photo to Bot.
3.  **Bot:** Extracts data -> Finds Reservation -> Confirms with User.
4.  **Bot:** Runs Playwright script to fill Online Check-in form.
5.  **Bot:** Sends "Check-in Successful".
6.  **Bot:** Asks "Create Invoice?".
7.  **Bot:** Calls `POST /invoices`, attaches mandatory VAT exemption note, returns PDF.
8.  **Cleanup:** Deletes local images (GDPR compliance).

## Environment Variables
```env
TELEGRAM_BOT_TOKEN=your_token
RENTLIO_API_KEY=your_key
GOOGLE_APPLICATION_CREDENTIALS=path_to_json