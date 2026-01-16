#!/usr/bin/env python3
"""
Rentlio Telegram Bot

Features:
- /start - Welcome message
- /upcoming - Get reservations arriving in next 7 days
- /today - Get today's arrivals
- /tomorrow - Get tomorrow's arrivals
- /reservation <id> - Get details of a specific reservation
- Daily notifications for check-ins and check-outs
"""
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, time

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from src.config import config
from src.services.rentlio_api import RentlioAPI, RentlioAPIError
from src.services.ocr_service import ocr_service, ExtractedGuestData
from src.services.form_filler import form_filler

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize API
api = RentlioAPI()

# Notification settings
NOTIFICATION_TIME = time(hour=8, minute=0)  # 8:00 AM


def format_date(timestamp: int) -> str:
    """Convert Unix timestamp to readable date"""
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y")


def format_reservation(res: dict, detailed: bool = False) -> str:
    """Format a reservation for display"""
    guest_name = res.get("guestName", "Unknown")
    unit_name = res.get("unitName", "")
    arrival = format_date(res.get("arrivalDate", 0))
    departure = format_date(res.get("departureDate", 0))
    nights = res.get("totalNights", 0)
    adults = res.get("adults", 0)
    children = res.get("childrenUnder12", 0) + res.get("childrenAbove12", 0)
    total_price = res.get("totalPrice", 0)
    currency = "EUR"  # Assuming EUR
    status = "✅" if res.get("checkedIn") == "Y" else "⏳"
    channel = res.get("otaChannelName", "Direct")
    
    text = f"""
{status} **{guest_name}**
🏠 {unit_name}
📅 {arrival} → {departure} ({nights} noći)
👥 {adults} adults{f' + {children} kids' if children else ''}
💰 {total_price:.0f} {currency}
📱 {channel}
"""
    
    if detailed:
        phone = res.get("guestContactNumber", "N/A")
        email = res.get("guestEmail", "N/A")
        note = res.get("note", "").strip()
        res_id = res.get("id", "")
        
        text += f"""
📞 {phone}
✉️ {email}
🔑 ID: `{res_id}`
"""
        if note:
            # Truncate long notes
            if len(note) > 200:
                note = note[:200] + "..."
            text += f"\n📝 Note: _{note}_"
    
    return text.strip()


# ========== Conversation States ==========
STATE_WAITING_FOR_URL = "waiting_for_url"
STATE_WAITING_FOR_INVOICE_CONFIRM = "waiting_for_invoice"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with menu"""
    keyboard = [
        [KeyboardButton("📅 Upcoming"), KeyboardButton("🌅 Today")],
        [KeyboardButton("🌄 Tomorrow"), KeyboardButton("🔍 Search")],
        [KeyboardButton("❓ Help")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🏠 **Rentlio Bot**\n\n"
        "Dobrodošli! Odaberi opciju iz menija ispod 👇\n\n"
        "**Komande:**\n"
        "/upcoming - Rezervacije sljedećih 7 dana\n"
        "/today - Današnji dolasci\n"
        "/tomorrow - Sutrašnji dolasci\n"
        "/search <ime> - Pretraži po imenu gosta\n\n"
        "**Check-in:**\n"
        "📷 Pošalji sliku osobne iskaznice za OCR\n",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def upcoming_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get reservations for next 7 days"""
    await update.message.reply_text("🔍 Dohvaćam rezervacije...")
    
    try:
        # Get dates
        today = datetime.now().strftime("%Y-%m-%d")
        week_later = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        # Fetch reservations
        reservations = await api.get_reservations(
            date_from=today,
            date_to=week_later,
            limit=20
        )
        
        if not reservations:
            await update.message.reply_text("📭 Nema rezervacija u sljedećih 7 dana.")
            return
        
        # Sort by arrival date
        reservations.sort(key=lambda x: x.get("arrivalDate", 0))
        
        # Group by date
        text = f"📅 **Rezervacije {today} - {week_later}**\n"
        text += f"Ukupno: {len(reservations)} rezervacija\n"
        text += "─" * 30
        
        for res in reservations:
            text += "\n" + format_reservation(res) + "\n"
        
        # Split message if too long
        if len(text) > 4000:
            # Send in chunks
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
            
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error fetching reservations: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def today_arrivals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get today's arrivals"""
    await update.message.reply_text("🔍 Dohvaćam današnje dolaske...")
    
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        today_ts_start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
        today_ts_end = int(datetime.now().replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=today,
            date_to=today,
            limit=50
        )
        
        # Filter to arrivals today only
        arrivals = [r for r in reservations 
                   if today_ts_start <= r.get("arrivalDate", 0) <= today_ts_end]
        
        if not arrivals:
            await update.message.reply_text("📭 Nema dolazaka danas.")
            return
        
        text = f"📅 **Dolasci danas ({today})**\n"
        text += f"Ukupno: {len(arrivals)}\n"
        text += "─" * 30
        
        for res in arrivals:
            text += "\n" + format_reservation(res, detailed=True) + "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def tomorrow_arrivals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get tomorrow's arrivals"""
    await update.message.reply_text("🔍 Dohvaćam sutrašnje dolaske...")
    
    try:
        tomorrow = (datetime.now() + timedelta(days=1))
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")
        tomorrow_ts_start = int(tomorrow.replace(hour=0, minute=0, second=0).timestamp())
        tomorrow_ts_end = int(tomorrow.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=tomorrow_str,
            date_to=tomorrow_str,
            limit=50
        )
        
        # Filter to arrivals tomorrow only
        arrivals = [r for r in reservations 
                   if tomorrow_ts_start <= r.get("arrivalDate", 0) <= tomorrow_ts_end]
        
        if not arrivals:
            await update.message.reply_text(f"📭 Nema dolazaka sutra ({tomorrow_str}).")
            return
        
        text = f"📅 **Dolasci sutra ({tomorrow_str})**\n"
        text += f"Ukupno: {len(arrivals)}\n"
        text += "─" * 30
        
        for res in arrivals:
            text += "\n" + format_reservation(res, detailed=True) + "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def search_guest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search reservations by guest name"""
    if not context.args:
        await update.message.reply_text("❓ Korištenje: /search <ime gosta>")
        return
    
    search_name = " ".join(context.args)
    await update.message.reply_text(f"🔍 Tražim '{search_name}'...")
    
    try:
        # Search in upcoming 30 days
        today = datetime.now().strftime("%Y-%m-%d")
        month_later = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        
        reservations = await api.get_reservations(
            date_from=today,
            date_to=month_later,
            limit=100
        )
        
        # Filter by name (case insensitive)
        search_lower = search_name.lower()
        matches = [r for r in reservations 
                  if search_lower in r.get("guestName", "").lower()]
        
        if not matches:
            await update.message.reply_text(f"📭 Nema rezultata za '{search_name}'")
            return
        
        text = f"🔍 **Rezultati za '{search_name}'**\n"
        text += f"Pronađeno: {len(matches)}\n"
        text += "─" * 30
        
        for res in matches:
            text += "\n" + format_reservation(res, detailed=True) + "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    await update.message.reply_text(
        "📖 **Pomoć**\n\n"
        "**Rezervacije:**\n"
        "📅 Upcoming - Sljedećih 7 dana\n"
        "🌅 Today - Današnji dolasci\n"
        "🌄 Tomorrow - Sutrašnji dolasci\n"
        "🔍 Search - Pretraži gosta\n\n"
        "**Check-in:**\n"
        "📷 Pošalji sliku osobne iskaznice\n"
        "🔗 Bot će izvući podatke i tražiti URL\n"
        "✅ Ispunit će formu automatski\n",
        parse_mode="Markdown"
    )


# ========== Photo / Check-in Flow ==========

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ID photo for OCR extraction"""
    await update.message.reply_text("🔍 Procesiram sliku...")
    
    try:
        # Get the largest photo
        photo = update.message.photo[-1]
        
        # Download photo to memory
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        
        # Extract data with OCR
        guest_data = await ocr_service.extract_from_bytes(bytes(image_bytes))
        
        # Delete the photo message for privacy
        try:
            await update.message.delete()
            await update.message.reply_text("🗑️ _Slika obrisana iz sigurnosnih razloga_", parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Could not delete photo: {e}")
        
        if not guest_data.is_valid():
            await update.message.reply_text(
                "❌ **Nisam uspio izvući podatke**\n\n"
                f"Raw text:\n```\n{guest_data.raw_text[:500]}```\n\n"
                "Pokušaj s boljom slikom (fokus, osvjetljenje).",
                parse_mode="Markdown"
            )
            return
        
        # Store extracted data in context
        context.user_data['guest_data'] = guest_data.to_dict()
        context.user_data['state'] = STATE_WAITING_FOR_URL
        
        # Create keyboard with cancel option
        keyboard = [[InlineKeyboardButton("❌ Odustani", callback_data="cancel_checkin")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"{guest_data.format_telegram()}\n\n"
            "✅ Podaci izvučeni!\n\n"
            "📎 Sada pošalji **check-in URL** link:\n"
            "`https://sun-apartments.book.rentl.io/reservation/check-in/...`",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await update.message.reply_text(f"❌ Greška: {str(e)}")


async def handle_checkin_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle check-in URL after photo was processed"""
    text = update.message.text.strip()
    
    # Check if it's a valid check-in URL (support both short and full format)
    # Short: ci.book.rentl.io/c/{uuid}/{code}
    # Full: sun-apartments.book.rentl.io/reservation/check-in/{uuid}
    is_short_url = 'ci.book.rentl.io' in text
    is_full_url = 'book.rentl.io' in text and 'check-in' in text
    
    if not (is_short_url or is_full_url):
        return False  # Not a check-in URL, let other handlers process it
    
    # Check if we're expecting a URL
    if context.user_data.get('state') != STATE_WAITING_FOR_URL:
        await update.message.reply_text(
            "⚠️ Prvo pošalji sliku osobne iskaznice, pa onda URL."
        )
        return True
    
    guest_data = context.user_data.get('guest_data')
    if not guest_data:
        await update.message.reply_text(
            "⚠️ Nema podataka o gostu. Pošalji prvo sliku osobne."
        )
        return True
    
    # Store URL (form_filler will transform if needed)
    context.user_data['checkin_url'] = text
    context.user_data['state'] = None
    
    # Show confirmation
    keyboard = [
        [
            InlineKeyboardButton("✅ Ispuni formu", callback_data="fill_form"),
            InlineKeyboardButton("❌ Odustani", callback_data="cancel_checkin")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔗 **URL primljen!**\n\n"
        f"👤 Gost: **{guest_data.get('fullName', 'N/A')}**\n"
        f"🪪 ID: {guest_data.get('documentNumber', 'N/A')}\n"
        f"🎂 DOB: {guest_data.get('dateOfBirth', 'N/A')}\n\n"
        f"Želiš da ispunim check-in formu?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return True


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_checkin":
        context.user_data.clear()
        await query.edit_message_text("❌ Check-in otkazan.")
        
    elif query.data == "fill_form":
        checkin_url = context.user_data.get('checkin_url')
        guest_data = context.user_data.get('guest_data')
        
        if not checkin_url or not guest_data:
            await query.edit_message_text("❌ Greška: nedostaju podaci.")
            return
        
        await query.edit_message_text("⏳ Ispunjavam formu... (ovo može potrajati 10-30 sec)")
        
        try:
            # Fill the form using Playwright
            result = await form_filler.fill_form(checkin_url, guest_data)
            
            if result.success:
                # Send screenshot of filled form
                if result.screenshot:
                    await context.bot.send_photo(
                        chat_id=query.message.chat_id,
                        photo=result.screenshot,
                        caption="✅ **Forma ispunjena!**\n\nPregledaj podatke i ručno potvrdi na stranici.",
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"✅ Forma ispunjena!\n\n{result.message}",
                        parse_mode="Markdown"
                    )
                
                # Ask about invoice
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Da, generiraj", callback_data="generate_invoice"),
                        InlineKeyboardButton("❌ Ne", callback_data="skip_invoice")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="🧾 Želiš generirati račun za ovog gosta?",
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"❌ **Greška pri ispunjavanju forme:**\n\n{result.message}\n\n"
                         f"Pokušaj ručno ispuniti formu:\n{checkin_url}",
                    parse_mode="Markdown"
                )
                context.user_data.clear()
                
        except Exception as e:
            logger.error(f"Form filling error: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Greška: {str(e)}\n\nPokušaj ručno: {checkin_url}"
            )
            context.user_data.clear()
        
    elif query.data == "generate_invoice":
        await query.edit_message_text("🧾 Generiranje računa... (TODO)")
        # TODO: Call Rentlio API to generate invoice
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✅ Račun generiran! (placeholder)\n\n_Invoice API integration coming soon_",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        
    elif query.data == "skip_invoice":
        await query.edit_message_text("👍 OK, bez računa.")
        context.user_data.clear()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages - check if it's a URL or menu button"""
    text = update.message.text
    
    # Check if it's a check-in URL (short or full format)
    if 'ci.book.rentl.io' in text or ('book.rentl.io' in text and 'check-in' in text):
        handled = await handle_checkin_url(update, context)
        if handled:
            return
    
    # Check if it's a menu button
    if any(emoji in text for emoji in ['📅', '🌅', '🌄', '🔍', '❓']):
        await handle_menu_buttons(update, context)
        return
    
    # Unknown text
    # Don't respond to avoid spam


async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu button presses"""
    text = update.message.text
    
    if "Upcoming" in text:
        await upcoming_reservations(update, context)
    elif "Today" in text:
        await today_arrivals(update, context)
    elif "Tomorrow" in text:
        await tomorrow_arrivals(update, context)
    elif "Search" in text:
        await update.message.reply_text("🔍 Za pretragu koristi:\n/search <ime gosta>\n\nPrimjer: /search Marko")
    elif "Help" in text:
        await help_command(update, context)


async def setup_bot_commands(app: Application):
    """Set up bot commands menu in Telegram"""
    commands = [
        BotCommand("start", "Pokreni bota"),
        BotCommand("upcoming", "Rezervacije sljedećih 7 dana"),
        BotCommand("today", "Današnji dolasci"),
        BotCommand("tomorrow", "Sutrašnji dolasci"),
        BotCommand("search", "Pretraži po imenu gosta"),
        BotCommand("notifications", "Uključi/isključi notifikacije"),
        BotCommand("help", "Pomoć"),
    ]
    await app.bot.set_my_commands(commands)


async def get_daily_summary() -> tuple[list, list]:
    """Get today's arrivals and departures"""
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    today_ts_start = int(today.replace(hour=0, minute=0, second=0).timestamp())
    today_ts_end = int(today.replace(hour=23, minute=59, second=59).timestamp())
    
    # Get reservations for today
    reservations = await api.get_reservations(
        date_from=today_str,
        date_to=today_str,
        limit=50
    )
    
    arrivals = []
    departures = []
    
    for res in reservations:
        arrival_ts = res.get("arrivalDate", 0)
        departure_ts = res.get("departureDate", 0)
        
        if today_ts_start <= arrival_ts <= today_ts_end:
            arrivals.append(res)
        if today_ts_start <= departure_ts <= today_ts_end:
            departures.append(res)
    
    return arrivals, departures


async def send_daily_notification(context: ContextTypes.DEFAULT_TYPE):
    """Send daily check-in/check-out notification"""
    logger.info("Checking for daily arrivals/departures...")
    
    try:
        arrivals, departures = await get_daily_summary()
        
        # Skip if nothing happening today
        if not arrivals and not departures:
            logger.info("No arrivals or departures today - skipping notification")
            return
        
        today_str = datetime.now().strftime("%d.%m.%Y")
        
        # Build message
        text = f"🌅 **Dnevni pregled - {today_str}**\n"
        text += "─" * 30 + "\n"
        
        # Arrivals
        if arrivals:
            text += f"\n🟢 **DOLASCI ({len(arrivals)})**\n"
            for res in arrivals:
                guest = res.get("guestName", "Unknown")
                unit = res.get("unitName", "")
                phone = res.get("guestContactNumber", "")
                nights = res.get("totalNights", 0)
                text += f"• {guest} → {unit} ({nights} noći)\n"
                if phone:
                    text += f"  📞 {phone}\n"
        
        # Departures
        if departures:
            text += f"\n🔴 **ODLASCI ({len(departures)})**\n"
            for res in departures:
                guest = res.get("guestName", "Unknown")
                unit = res.get("unitName", "")
                text += f"• {guest} ← {unit}\n"
        
        # Send to all allowed users
        for user_id in config.TELEGRAM_ALLOWED_USERS:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="Markdown"
                )
                logger.info(f"Sent daily notification to user {user_id}")
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {e}")
        
    except Exception as e:
        logger.error(f"Error sending daily notification: {e}")


async def toggle_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle notifications and show current user ID"""
    user_id = update.effective_user.id
    
    is_allowed = user_id in config.TELEGRAM_ALLOWED_USERS
    
    text = f"🔔 **Notifikacije**\n\n"
    text += f"Tvoj User ID: `{user_id}`\n\n"
    
    if is_allowed:
        text += "✅ Notifikacije su UKLJUČENE\n"
        text += f"⏰ Šaljem dnevni pregled u {NOTIFICATION_TIME.strftime('%H:%M')}\n\n"
        text += "_Za isključivanje, ukloni svoj ID iz .env filea_"
    else:
        text += "❌ Notifikacije su ISKLJUČENE\n\n"
        text += "Za uključivanje, dodaj svoj User ID u .env:\n"
        text += f"`TELEGRAM_ALLOWED_USERS={user_id}`"
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")


def main():
    """Start the bot"""
    # Validate config
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        print("Get your token from @BotFather on Telegram")
        return
    
    if not config.RENTLIO_API_KEY:
        print("❌ RENTLIO_API_KEY not set in .env")
        return
    
    print("🤖 Starting Rentlio Bot...")
    print(f"API URL: {config.RENTLIO_API_URL}")
    
    # Create application
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upcoming", upcoming_reservations))
    app.add_handler(CommandHandler("today", today_arrivals))
    app.add_handler(CommandHandler("tomorrow", tomorrow_arrivals))
    app.add_handler(CommandHandler("search", search_guest))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("notifications", toggle_notifications))
    
    # Handle photo messages (for OCR)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Handle callback queries (inline buttons)
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Handle text messages (URLs and menu buttons)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_message
    ))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Set up commands menu and scheduled jobs
    async def post_init(application: Application):
        await setup_bot_commands(application)
        
        # Schedule daily notification
        if config.TELEGRAM_ALLOWED_USERS:
            job_queue = application.job_queue
            job_queue.run_daily(
                send_daily_notification,
                time=NOTIFICATION_TIME,
                name="daily_notification"
            )
            print(f"📅 Daily notifications scheduled for {NOTIFICATION_TIME.strftime('%H:%M')}")
            print(f"👤 Notifying users: {config.TELEGRAM_ALLOWED_USERS}")
        else:
            print("⚠️  No TELEGRAM_ALLOWED_USERS set - notifications disabled")
            print("   Use /notifications in the bot to get your user ID")
    
    # Run bot
    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
