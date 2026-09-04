#!/usr/bin/env python3
"""
Rentlio Telegram Bot

Features:
- /start - Welcome message
- /checkin - NEW API-based check-in (no form filling!)
- /upcoming - Get reservations arriving in next 7 days
- /today - Get today's arrivals
- /tomorrow - Get tomorrow's arrivals
- /reservation <id> - Get details of a specific reservation
- Daily notifications for check-ins and check-outs
"""
import asyncio
import calendar
import logging
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, time
from typing import Optional

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
from src.services.rentlio_api import (
    RentlioAPI,
    RentlioAPIError,
    is_checked_in,
    is_live_reservation,
)
from src.services import ai_advisor, occupancy_service
from src.services.ocr_service import ocr_service, ExtractedGuestData, strip_diacritics
from src.services.country_mapper import country_mapper

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
    status = "✅" if is_checked_in(res) else "⏳"
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
STATE_CHECKIN_WAITING_FOR_PHOTO = "checkin_waiting_for_photo"
STATE_CHECKIN_SELECTING_RESERVATION = "checkin_selecting_reservation"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with menu"""
    keyboard = [
        [KeyboardButton("📅 Upcoming"), KeyboardButton("🌅 Today")],
        [KeyboardButton("🌄 Tomorrow"), KeyboardButton("🔍 Search")],
        [KeyboardButton("🤖 Analiza"), KeyboardButton("❓ Help")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🏠 **Rentlio Bot**\n\n"
        "Dobrodošli! Odaberi opciju iz menija ispod 👇\n\n"
        "**📷 Check-in:**\n"
        "Samo pošalji slike osobnih iskaznica!\n"
        "Bot automatski prepozna goste i ponudi check-in.\n\n"
        "**Komande:**\n"
        "/upcoming - Rezervacije sljedećih 7 dana\n"
        "/today - Današnji dolasci\n"
        "/tomorrow - Sutrašnji dolasci\n"
        "/search <ime> - Pretraži po imenu gosta\n",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def upcoming_reservations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get reservations arriving in next 7 days"""
    await update.message.reply_text("🔍 Dohvaćam dolaske u sljedećih 7 dana...")
    
    try:
        # Get dates
        today = datetime.now()
        week_later = today + timedelta(days=7)
        
        today_str = today.strftime("%Y-%m-%d")
        week_str = week_later.strftime("%Y-%m-%d")
        
        today_ts = int(today.replace(hour=0, minute=0, second=0).timestamp())
        week_ts = int(week_later.replace(hour=23, minute=59, second=59).timestamp())
        
        # Fetch reservations
        all_reservations = await api.get_reservations(
            date_from=today_str,
            date_to=week_str,
            limit=50
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        arrivals = [r for r in all_reservations 
                   if is_live_reservation(r) and today_ts <= r.get("arrivalDate", 0) <= week_ts]
        
        if not arrivals:
            await update.message.reply_text("📭 Nema dolazaka u sljedećih 7 dana.")
            return
        
        # Sort by arrival date
        arrivals.sort(key=lambda x: x.get("arrivalDate", 0))
        
        # Build message grouped by unit
        text = f"📅 **Dolasci - sljedećih 7 dana**\n"
        text += f"Ukupno: {len(arrivals)} dolazaka\n\n"
        
        # Group by unit (apartment)
        from collections import defaultdict
        by_unit = defaultdict(list)
        for res in arrivals:
            unit = res.get("unitName", "Unknown")
            by_unit[unit].append(res)
        
        for unit in sorted(by_unit.keys()):
            text += f"🏠 **{unit}**\n"
            # Sort by arrival date within unit
            unit_arrivals = sorted(by_unit[unit], key=lambda x: x.get("arrivalDate", 0))
            for res in unit_arrivals:
                arrival_date = datetime.fromtimestamp(res.get("arrivalDate", 0)).strftime("%d.%m")
                guest = res.get("guestName", "Unknown")
                nights = res.get("totalNights", 0)
                adults = res.get("adults", 0)
                price = res.get("totalPrice", 0)
                text += f"  • {arrival_date}: {guest} ({nights} {'noć' if nights == 1 else 'noći'}, {adults} os., {price:.0f}€)\n"
            text += "\n"
        
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
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        today_display = today.strftime("%d.%m.%Y")
        today_ts_start = int(today.replace(hour=0, minute=0, second=0).timestamp())
        today_ts_end = int(today.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=today_str,
            date_to=today_str,
            limit=50
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        arrivals = [r for r in reservations 
                   if is_live_reservation(r) and today_ts_start <= r.get("arrivalDate", 0) <= today_ts_end]
        
        if not arrivals:
            await update.message.reply_text(f"📭 Nema dolazaka danas ({today_display}).")
            return
        
        text = f"📅 **Dolasci danas - {today_display}**\n"
        text += f"Ukupno: {len(arrivals)}\n\n"
        
        # Group by unit
        from collections import defaultdict
        by_unit = defaultdict(list)
        for res in arrivals:
            unit = res.get("unitName", "Unknown")
            by_unit[unit].append(res)
        
        for unit in sorted(by_unit.keys()):
            text += f"🏠 **{unit}**\n"
            for res in by_unit[unit]:
                guest = res.get("guestName", "Unknown")
                phone = res.get("guestContactNumber", "")
                nights = res.get("totalNights", 0)
                adults = res.get("adults", 0)
                price = res.get("totalPrice", 0)
                text += f"  • {guest} ({nights} {'noć' if nights == 1 else 'noći'}, {adults} os., {price:.0f}€)\n"
                if phone:
                    text += f"    📞 {phone}\n"
            text += "\n"
        
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
        tomorrow_display = tomorrow.strftime("%d.%m.%Y")
        tomorrow_ts_start = int(tomorrow.replace(hour=0, minute=0, second=0).timestamp())
        tomorrow_ts_end = int(tomorrow.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=tomorrow_str,
            date_to=tomorrow_str,
            limit=50
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        arrivals = [r for r in reservations 
                   if is_live_reservation(r) and tomorrow_ts_start <= r.get("arrivalDate", 0) <= tomorrow_ts_end]
        
        if not arrivals:
            await update.message.reply_text(f"📭 Nema dolazaka sutra ({tomorrow_display}).")
            return
        
        text = f"📅 **Dolasci sutra - {tomorrow_display}**\n"
        text += f"Ukupno: {len(arrivals)}\n\n"
        
        # Group by unit
        from collections import defaultdict
        by_unit = defaultdict(list)
        for res in arrivals:
            unit = res.get("unitName", "Unknown")
            by_unit[unit].append(res)
        
        for unit in sorted(by_unit.keys()):
            text += f"🏠 **{unit}**\n"
            for res in by_unit[unit]:
                guest = res.get("guestName", "Unknown")
                phone = res.get("guestContactNumber", "")
                nights = res.get("totalNights", 0)
                adults = res.get("adults", 0)
                price = res.get("totalPrice", 0)
                text += f"  • {guest} ({nights} {'noć' if nights == 1 else 'noći'}, {adults} os., {price:.0f}€)\n"
                if phone:
                    text += f"    📞 {phone}\n"
            text += "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def checkouts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get today's and tomorrow's departures"""
    await update.message.reply_text("🔍 Dohvaćam odlaske...")
    
    try:
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        
        today_str = today.strftime("%Y-%m-%d")
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")
        today_display = today.strftime("%d.%m.%Y")
        tomorrow_display = tomorrow.strftime("%d.%m.%Y")
        
        today_ts_start = int(today.replace(hour=0, minute=0, second=0).timestamp())
        today_ts_end = int(today.replace(hour=23, minute=59, second=59).timestamp())
        tomorrow_ts_start = int(tomorrow.replace(hour=0, minute=0, second=0).timestamp())
        tomorrow_ts_end = int(tomorrow.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=today_str,
            date_to=tomorrow_str,
            limit=50
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        reservations = [r for r in reservations if is_live_reservation(r)]
        
        today_departures = [r for r in reservations 
                          if today_ts_start <= r.get("departureDate", 0) <= today_ts_end]
        tomorrow_departures = [r for r in reservations 
                              if tomorrow_ts_start <= r.get("departureDate", 0) <= tomorrow_ts_end]
        
        if not today_departures and not tomorrow_departures:
            await update.message.reply_text("📭 Nema odlazaka danas ni sutra.")
            return
        
        text = "🔴 **Odlasci**\n\n"
        
        if today_departures:
            text += f"**Danas - {today_display}**\n"
            for res in today_departures:
                guest = res.get("guestName", "Unknown")
                unit = res.get("unitName", "")
                text += f"  • {guest} ← {unit}\n"
            text += "\n"
        
        if tomorrow_departures:
            text += f"**Sutra - {tomorrow_display}**\n"
            for res in tomorrow_departures:
                guest = res.get("guestName", "Unknown")
                unit = res.get("unitName", "")
                text += f"  • {guest} ← {unit}\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def cleaning_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show departures for next 7 days - for cleaning schedule"""
    await update.message.reply_text("🧹 Dohvaćam raspored čišćenja...")
    
    try:
        today = datetime.now()
        week_later = today + timedelta(days=7)
        
        today_str = today.strftime("%Y-%m-%d")
        week_str = week_later.strftime("%Y-%m-%d")
        
        today_ts = int(today.replace(hour=0, minute=0, second=0).timestamp())
        week_ts = int(week_later.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=today_str,
            date_to=week_str,
            limit=100
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        departures = [r for r in reservations 
                     if is_live_reservation(r) and today_ts <= r.get("departureDate", 0) <= week_ts]
        
        if not departures:
            await update.message.reply_text("📭 Nema odlazaka u sljedećih 7 dana.")
            return
        
        # Sort by departure date
        departures.sort(key=lambda x: x.get("departureDate", 0))
        
        text = f"🧹 **Raspored čišćenja - sljedećih 7 dana**\n\n"
        
        # Croatian full day names
        CROATIAN_DAYS = {
            0: "Ponedjeljak", 1: "Utorak", 2: "Srijeda",
            3: "Četvrtak", 4: "Petak", 5: "Subota", 6: "Nedjelja"
        }

        # Apartment codes
        APARTMENT_CODES = {
            "Sunset": 1,
            "Sunrise": 2,
        }

        # Group by date
        from collections import defaultdict
        by_date = defaultdict(list)
        for res in departures:
            dt = datetime.fromtimestamp(res.get("departureDate", 0))
            day_name = CROATIAN_DAYS.get(dt.weekday(), "")
            departure_date = dt.strftime("%d.%m") + f" ({day_name})"
            by_date[departure_date].append(res)
        
        # Get sorted dates
        sorted_dates = sorted(by_date.keys(), key=lambda d: datetime.strptime(d.split(" ")[0], "%d.%m"))
        
        for date_str in sorted_dates:
            text += f"📅 **{date_str}**\n"
            
            # Group by unit
            unit_groups = defaultdict(list)
            for res in by_date[date_str]:
                unit = res.get("unitName", "Unknown")
                unit_groups[unit].append(res)
            
            for unit in sorted(unit_groups.keys()):
                code = APARTMENT_CODES.get(unit)
                unit_label = f"{unit} ({code})" if code else unit
                text += f"  🏠 {unit_label}\n"
                for res in unit_groups[unit]:
                    guest = res.get("guestName", "Unknown")
                    text += f"    • {guest}\n"
            text += "\n"
        
        text += f"📊 Ukupno: {len(departures)} odlazaka\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def current_guests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show who's currently staying in each apartment"""
    await update.message.reply_text("🔍 Dohvaćam trenutne goste...")
    
    try:
        today = datetime.now()

        # Get reservations that overlap with today
        week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        week_later = (today + timedelta(days=7)).strftime("%Y-%m-%d")
        
        reservations = await api.get_reservations(
            date_from=week_ago,
            date_to=week_later,
            limit=50
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        today_date = today.date()
        current = []
        for r in reservations:
            if not is_live_reservation(r):
                continue
            arrival = r.get("arrivalDate", 0)
            departure = r.get("departureDate", 0)
            # Compare using dates only (API timestamps are at midnight, so
            # timestamp comparison breaks on the checkout day itself)
            arrival_date = datetime.fromtimestamp(arrival).date()
            departure_date = datetime.fromtimestamp(departure).date()
            # Guest is currently staying if: arrived <= today <= departure day
            if arrival_date <= today_date <= departure_date:
                current.append(r)
        
        if not current:
            await update.message.reply_text("📭 Trenutno nema gostiju.")
            return
        
        text = f"🏠 **Trenutni gosti** ({today.strftime('%d.%m.%Y %H:%M')})\n\n"
        
        # Group by unit
        from collections import defaultdict
        by_unit = defaultdict(list)
        for res in current:
            unit = res.get("unitName", "Unknown")
            by_unit[unit].append(res)
        
        for unit in sorted(by_unit.keys()):
            text += f"🏠 **{unit}**\n"
            for res in by_unit[unit]:
                guest = res.get("guestName", "Unknown")
                departure = datetime.fromtimestamp(res.get("departureDate", 0))
                days_left = (departure.date() - today_date).days
                checkout_str = departure.strftime("%d.%m")
                phone = res.get("guestContactNumber", "")
                
                if days_left == 0:
                    status = "🔴 odlazi danas"
                elif days_left == 1:
                    status = "🟡 odlazi sutra"
                else:
                    status = f"odlazi {checkout_str}"
                
                text += f"  • {guest} ({status})\n"
                if phone:
                    text += f"    📞 {phone}\n"
            text += "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
        
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Error: {e.message}")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


async def week_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show weekly statistics with occupancy and revenue"""
    await update.message.reply_text("📊 Računam tjednu statistiku...")
    
    try:
        today = datetime.now()
        # Get current week (Monday to Sunday)
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        start_str = start_of_week.strftime("%Y-%m-%d")
        end_str = end_of_week.strftime("%Y-%m-%d")
        start_display = start_of_week.strftime("%d.%m")
        end_display = end_of_week.strftime("%d.%m")
        
        start_ts = int(start_of_week.replace(hour=0, minute=0, second=0).timestamp())
        end_ts = int(end_of_week.replace(hour=23, minute=59, second=59).timestamp())
        
        reservations = await api.get_reservations(
            date_from=start_str,
            date_to=end_str,
            limit=100
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        reservations = [r for r in reservations if is_live_reservation(r)]
        
        # Get unique units
        units = set()
        for r in reservations:
            units.add(r.get("unitName", "Unknown"))
        
        # Calculate stats per unit
        from collections import defaultdict
        unit_stats = defaultdict(lambda: {"nights": 0, "revenue": 0, "guests": []})
        
        for res in reservations:
            unit = res.get("unitName", "Unknown")
            arrival = res.get("arrivalDate", 0)
            departure = res.get("departureDate", 0)
            price = res.get("totalPrice", 0)
            total_nights = res.get("totalNights", 1)
            guest = res.get("guestName", "Unknown")
            
            # Calculate nights within this week only
            res_start = max(arrival, start_ts)
            res_end = min(departure, end_ts)
            
            if res_end > res_start:
                nights_in_week = (res_end - res_start) // 86400  # seconds in a day
                nights_in_week = max(1, nights_in_week)  # at least 1 night
                
                # Proportional revenue for nights in this week
                if total_nights > 0:
                    revenue_per_night = price / total_nights
                    week_revenue = revenue_per_night * nights_in_week
                else:
                    week_revenue = price
                
                unit_stats[unit]["nights"] += nights_in_week
                unit_stats[unit]["revenue"] += week_revenue
                unit_stats[unit]["guests"].append(guest)
        
        # Build message
        text = f"📊 **Tjedna statistika**\n"
        text += f"📅 {start_display} - {end_display}\n\n"
        
        total_revenue = 0
        total_nights = 0
        total_possible = 0
        
        for unit in sorted(unit_stats.keys()):
            stats = unit_stats[unit]
            nights = stats["nights"]
            revenue = stats["revenue"]
            occupancy = (nights / 7) * 100  # 7 days in a week
            
            total_revenue += revenue
            total_nights += nights
            total_possible += 7
            
            # Occupancy bar
            filled = int(occupancy / 10)
            bar = "█" * filled + "░" * (10 - filled)
            
            text += f"🏠 **{unit}**\n"
            text += f"  {bar} {occupancy:.0f}%\n"
            text += f"  📅 {nights}/7 noći\n"
            text += f"  💰 {revenue:.0f}€\n"
            if stats["guests"]:
                text += f"  👥 {', '.join(stats['guests'][:3])}\n"
            text += "\n"
        
        # Total summary
        if total_possible > 0:
            total_occupancy = (total_nights / total_possible) * 100
        else:
            total_occupancy = 0
        
        num_units = len(unit_stats)
        text += "─" * 20 + "\n"
        text += f"**UKUPNO** ({num_units} apartmana)\n"
        text += f"💰 Prihod: **{total_revenue:.0f}€**\n"
        text += f"📈 Popunjenost: **{total_occupancy:.0f}%**\n"
        text += f"🛏️ {total_nights} noći\n"
        
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
        "**📷 Check-in:**\n"
        "1️⃣ Pošalji slike osobnih iskaznica\n"
        "2️⃣ Odaberi rezervaciju\n"
        "3️⃣ Gosti se dodaju direktno u Rentlio!\n\n"
        "**Rezervacije:**\n"
        "📅 Upcoming - Sljedećih 7 dana\n"
        "🌅 Today - Današnji dolasci\n"
        "🌄 Tomorrow - Sutrašnji dolasci\n"
        "🔍 Search - Pretraži gosta\n\n"
        "**Statistika i cijene:**\n"
        "/week - Tjedna statistika\n"
        "/analiza [30|60] - AI analiza popunjenosti:\n"
        "  gdje spustiti cijenu, gdje mijenjati min. broj noćenja,\n"
        "  usporedba s istim terminima prijašnjih sezona\n\n"
        "**Računi:**\n"
        "/invoice <id> - Upravljaj računima\n",
        parse_mode="Markdown"
    )


# ========== Occupancy & Pricing Analysis ==========

ANALYSIS_HORIZONS = (30, 60)


def _analysis_keyboard(horizon_days: int, calendar_shown: bool) -> InlineKeyboardMarkup:
    """Buttons to re-run the analysis over another horizon or with the calendar."""
    horizon_row = [
        InlineKeyboardButton(
            f"{'✅ ' if days == horizon_days else ''}{days} dana",
            callback_data=f"occ:{days}:report"
        )
        for days in ANALYSIS_HORIZONS
    ]
    second_row = []
    if not calendar_shown:
        second_row.append(
            InlineKeyboardButton("🗓️ Kalendar", callback_data=f"occ:{horizon_days}:cal")
        )
    second_row.append(
        InlineKeyboardButton("🔄 Osvježi", callback_data=f"occ:{horizon_days}:refresh")
    )
    return InlineKeyboardMarkup([horizon_row, second_row])


async def _deliver_analysis(
    message,
    horizon_days: int,
    include_calendar: bool = False,
    use_cache: bool = True,
):
    """Run the analysis and push it to Telegram, rule-based part first."""
    status = await message.reply_text(
        f"📊 Analiziram iducih {horizon_days} dana i usporedujem s prijasnjim sezonama..."
    )

    try:
        report = await occupancy_service.run_analysis(
            api,
            horizon_days=horizon_days,
            history_years=2,
            use_cache=use_cache,
        )
    except RentlioAPIError as e:
        await status.edit_text(f"❌ Rentlio API greška: {e.message}")
        return
    except Exception as e:
        logger.exception("Occupancy analysis failed")
        await status.edit_text(f"❌ Greška u analizi: {e}")
        return

    chunks = occupancy_service.format_full_report(report, include_calendar=include_calendar)
    await status.edit_text(chunks[0])
    for chunk in chunks[1:-1]:
        await message.reply_text(chunk)

    keyboard = _analysis_keyboard(horizon_days, include_calendar)
    if len(chunks) > 1:
        await message.reply_text(chunks[-1], reply_markup=keyboard)
    else:
        await status.edit_reply_markup(reply_markup=keyboard)

    # The AI brief is a bonus on top of a report that already stands on its own.
    if not ai_advisor.is_available():
        reason = ai_advisor.unavailable_reason()
        if reason:
            await message.reply_text(f"ℹ️ {reason}")
        return

    thinking = await message.reply_text("🤖 Pišem AI brief...")
    advice = await ai_advisor.generate_advice(report)
    if advice:
        for i, chunk in enumerate(occupancy_service.split_message("🤖 AI BRIEF\n\n" + advice)):
            if i == 0:
                await thinking.edit_text(chunk)
            else:
                await message.reply_text(chunk)
    else:
        await thinking.edit_text(
            "⚠️ AI brief nije uspio (vidi logove). Preporuke iznad su izracunate "
            "iz podataka i vrijede i bez njega."
        )


async def analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/analiza [30|60] - occupancy and pricing analysis for the coming weeks"""
    horizon_days = 30
    if context.args:
        try:
            requested = int(context.args[0])
            # Anything between a fortnight and a quarter is a sensible window;
            # beyond that the historical comparison gets thin.
            horizon_days = max(7, min(requested, 120))
        except ValueError:
            await update.message.reply_text(
                "❓ Korištenje: /analiza [broj dana]\n\nPrimjer: /analiza 60"
            )
            return

    await _deliver_analysis(update.message, horizon_days)


async def handle_analysis_callback(query, context):
    """Buttons under the analysis message."""
    _, horizon_raw, mode = query.data.split(":")
    horizon_days = int(horizon_raw)
    await _deliver_analysis(
        query.message,
        horizon_days,
        include_calendar=(mode == "cal"),
        use_cache=(mode != "refresh"),
    )


# ========== NEW API-Based Check-in Flow ==========

async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the new API-based check-in flow"""
    # Clear any previous state
    context.user_data.clear()
    
    # Initialize check-in session
    context.user_data['checkin_guests'] = []
    context.user_data['state'] = STATE_CHECKIN_WAITING_FOR_PHOTO
    
    # Load countries if not loaded
    await country_mapper.load_countries(api)
    
    await update.message.reply_text(
        "🛎️ **API Check-in**\n\n"
        "📷 Pošalji slike osobnih iskaznica/putovnica.\n\n"
        "Podržano:\n"
        "• 🇭🇷 Hrvatske osobne iskaznice\n"
        "• 🌍 Putovnice s MRZ zonom\n"
        "• 🪪 EU osobne iskaznice\n\n"
        "Možeš poslati više slika za više gostiju.\n"
        "Kada završiš, klikni **Nastavi** 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Nastavi s odabirom rezervacije", callback_data="checkin_select_reservation")],
            [InlineKeyboardButton("❌ Odustani", callback_data="checkin_cancel")]
        ])
    )


async def handle_checkin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo in new check-in flow"""
    state = context.user_data.get('state')
    
    if state != STATE_CHECKIN_WAITING_FOR_PHOTO:
        return False  # Not in check-in flow
    
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
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="🗑️ _Slika obrisana iz sigurnosnih razloga_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not delete photo: {e}")
        
        if not guest_data.is_valid():
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="❌ **Nisam uspio izvući podatke**\n\n"
                     f"Pokušaj s boljom slikom (fokus, osvjetljenje).\n\n"
                     f"Raw text:\n```\n{guest_data.raw_text[:300]}...```",
                parse_mode="Markdown"
            )
            return True
        
        # Add to guests list
        context.user_data['checkin_guests'].append(guest_data)
        guest_count = len(context.user_data['checkin_guests'])
        
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"{guest_data.format_telegram()}\n\n"
                 f"✅ **Gost {guest_count} dodan!**\n\n"
                 f"📷 Pošalji još slika ili klikni **Nastavi** 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Nastavi ({guest_count} gost/a)", callback_data="checkin_select_reservation")],
                [InlineKeyboardButton("❌ Odustani", callback_data="checkin_cancel")]
            ])
        )
        
        return True
        
    except Exception as e:
        logger.error(f"Check-in photo processing error: {e}")
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"❌ Greška: {str(e)}"
        )
        return True


async def show_reservation_selection(query, context):
    """Show upcoming reservations for check-in"""
    guests = context.user_data.get('checkin_guests', [])
    
    if not guests:
        await query.edit_message_text(
            "⚠️ Nema gostiju za check-in.\n\n"
            "Koristi /checkin za početak i pošalji slike osobnih."
        )
        context.user_data.clear()
        return
    
    await query.edit_message_text("⏳ Dohvaćam nadolazeće rezervacije...")
    
    try:
        # Fetch upcoming reservations (today + next 5 days)
        today = datetime.now().strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        
        reservations = await api.get_reservations(
            date_from=today,
            date_to=future,
            limit=20
        )
        
        # Keep every live reservation; only refused/cancelled/deleted are dropped
        reservations = [r for r in reservations if is_live_reservation(r)]
        
        if not reservations:
            await query.edit_message_text(
                "📭 Nema rezervacija u sljedećih 5 dana.\n\n"
                "Provjeri datume rezervacija u Rentlio sustavu."
            )
            context.user_data.clear()
            return
        
        # Sort by arrival date
        reservations.sort(key=lambda x: x.get("arrivalDate", 0))
        
        # Store reservations for later use
        context.user_data['checkin_reservations'] = {str(r['id']): r for r in reservations}
        context.user_data['state'] = STATE_CHECKIN_SELECTING_RESERVATION
        
        # Build keyboard with reservation options (max 6)
        keyboard = []
        for res in reservations[:6]:
            res_id = str(res.get('id', ''))
            guest_name = res.get('guestName', 'N/A')[:15]
            unit_name = res.get('unitName', '')[:10]
            arrival = format_date(res.get('arrivalDate', 0))
            nights = res.get('totalNights', 0)
            checked_in = "✅" if is_checked_in(res) else "⏳"
            
            btn_text = f"{checked_in} {guest_name} | {unit_name} | {arrival}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"checkin_res_{res_id}")])
        
        keyboard.append([InlineKeyboardButton("❌ Odustani", callback_data="checkin_cancel")])
        
        # Guest summary
        guest_summary = ""
        for i, guest in enumerate(guests, 1):
            name = guest.full_name or f"{guest.first_name} {guest.last_name}".strip()
            guest_summary += f"\n👤 Gost {i}: **{name}**"
            if guest.nationality:
                guest_summary += f" ({guest.nationality})"
        
        await query.edit_message_text(
            f"🛎️ **API Check-in**\n\n"
            f"**Gosti za prijavu:**{guest_summary}\n\n"
            f"**Odaberi rezervaciju:**\n"
            f"_(rezervacije sljedećih 5 dana)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except RentlioAPIError as e:
        await query.edit_message_text(f"❌ API Greška: {e.message}")
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Fetch reservations error: {e}")
        await query.edit_message_text(f"❌ Greška: {str(e)}")
        context.user_data.clear()


def convert_date_to_timestamp(date_str: str) -> Optional[str]:
    """Convert DD.MM.YYYY to Unix timestamp string (UTC midnight)"""
    if not date_str:
        return None
    
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            # Use calendar.timegm to treat as UTC midnight
            # (datetime.timestamp() uses local tz, causing off-by-one day)
            return str(int(calendar.timegm(dt.timetuple())))
        except ValueError:
            continue
    
    return None


def convert_gender_to_id(gender: str) -> Optional[int]:
    """Convert M/F to Rentlio gender ID (1=Female, 2=Male)"""
    if not gender:
        return None
    
    g = gender.upper().strip()
    if g in ('M', 'MALE', 'MUŠKO', 'MUSKI'):
        return 2
    elif g in ('F', 'FEMALE', 'ŽENSKO', 'ZENSKO', 'ŽENSKI'):
        return 1
    return None


# Direct mapping: (document_type, is_croatian) -> eVisitorDocumentTypeId.
# IDs from /enums/guests/document-types; verified against a reservation
# registered through Rentlio's own UI, which stores 25 for a Croatian ID card.
_DOCUMENT_TYPE_IDS = {
    ("ID_CARD", True): 25,       # Personal ID card (Croatian)
    ("ID_CARD", False): 23,      # Personal ID card (foreign)
    ("PASSPORT", True): 14,      # Personal passport (Croatian)
    ("PASSPORT", False): 18,     # Personal passport (foreign)
}


def _get_document_type_id(doc_type: str, nationality: str = None) -> Optional[int]:
    """Get Rentlio document type ID.
    
    Args:
        doc_type: "ID_CARD" or "PASSPORT"
        nationality: Guest nationality string
    
    Returns:
        eVisitorDocumentTypeId or None
    """
    if not doc_type:
        return None
    is_croatian = nationality and nationality.lower() in ('hrvatska', 'croatia', 'hrv', 'cro')
    type_id = _DOCUMENT_TYPE_IDS.get((doc_type, is_croatian))
    if type_id is None:
        # Fallback: try without nationality
        type_id = _DOCUMENT_TYPE_IDS.get((doc_type, False))
    logger.info(f"Document type mapping: {doc_type}, croatian={is_croatian} -> id={type_id}")
    return type_id


# Tourist tax categories from /enums/guests/tax-categories. eVisitor splits
# guests by age; a reservation registered through Rentlio's UI stores 3 for an
# adult.
TAX_CATEGORY_ADULT = 3          # Tourist staying in a property
TAX_CATEGORY_CHILD_12_TO_18 = 4  # Children: between 12 and 18 years
TAX_CATEGORY_CHILD_UNDER_12 = 7  # Children up to 12 years


def _get_tourist_tax_category(date_of_birth: str) -> int:
    """Pick the eVisitor tourist tax category from the guest's date of birth.

    Falls back to the adult category when the date is missing or unparseable -
    charging tourist tax that may not be due is recoverable, omitting a guest
    from eVisitor is not.
    """
    if not date_of_birth:
        return TAX_CATEGORY_ADULT

    born = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            born = datetime.strptime(date_of_birth, fmt)
            break
        except ValueError:
            continue
    if born is None:
        logger.warning(f"Unparseable date of birth {date_of_birth!r}, assuming adult")
        return TAX_CATEGORY_ADULT

    today = datetime.now()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    if age < 12:
        return TAX_CATEGORY_CHILD_UNDER_12
    if age < 18:
        return TAX_CATEGORY_CHILD_12_TO_18
    return TAX_CATEGORY_ADULT


def _name_key(name: str) -> str:
    """Comparable form of a guest name.

    OCR gives "DORA MASANOVIC", Rentlio may hold "Dora Mašanović", and a
    booking channel may put the surname first. Compare on sorted, accent-free,
    lowercase words so all three land on the same key.
    """
    if not name:
        return ""
    words = re.findall(r"[^\W\d_]+", strip_diacritics(name).lower(), re.UNICODE)
    return " ".join(sorted(words))


def _match_existing_guest(guest, existing: list) -> Optional[dict]:
    """Find the guest already on the reservation, if this is the same person.

    Booking channels put the holder on the reservation before anyone scans a
    document, so the common case is that the person we just read from an ID is
    already there with empty fields. Adding them again is what produced
    duplicate registrations.
    """
    name = guest.full_name or f"{guest.first_name or ''} {guest.last_name or ''}"
    key = _name_key(name)
    if not key:
        return None
    for candidate in existing:
        if _name_key(candidate.get("name", "")) == key:
            return candidate
    return None


async def perform_api_checkin(query, context, reservation_id: str):
    """Perform the actual API check-in"""
    guests = context.user_data.get('checkin_guests', [])
    reservations = context.user_data.get('checkin_reservations', {})
    reservation_data = reservations.get(reservation_id, {})
    
    if not guests:
        await query.edit_message_text("⚠️ Nema gostiju za prijavu.")
        context.user_data.clear()
        return
    
    await query.edit_message_text(
        f"⏳ Prijavljujem {len(guests)} gost(a) na rezervaciju #{reservation_id}..."
    )
    
    try:
        # A channel booking already carries its holder, so scanning that
        # person's ID and blindly POSTing them added the same human twice.
        # Read what is there first and update in place where it is the same
        # person.
        existing_guests = []
        try:
            existing_guests = await api.get_reservation_guests_v2(reservation_id)
        except Exception as e:
            logger.warning(f"Could not read existing guests, treating all as new: {e}")
        has_primary = any(g.get("isPrimary") == "Y" for g in existing_guests)
        logger.info(
            f"Reservation {reservation_id} already has {len(existing_guests)} guest(s), "
            f"primary present: {has_primary}"
        )

        # Only guests without a match are POSTed; document and eVisitor fields
        # always go through a PUT, which needs the guest to have an id.
        api_guests = []
        guest_doc_data = []
        guest_ids = []  # existing id per guest, or None until POST assigns one

        for i, guest in enumerate(guests):
            # Build full name
            name = guest.full_name
            if not name and (guest.first_name or guest.last_name):
                name = f"{guest.first_name or ''} {guest.last_name or ''}".strip()
            
            if not name:
                name = f"Gost {i + 1}"
            
            # Get country ID
            country_id = None
            if guest.nationality:
                country_id = country_mapper.get_country_id(guest.nationality)
            
            matched = _match_existing_guest(guest, existing_guests)
            if matched:
                # Keep the role the reservation already assigned - overwriting
                # a booker or primary flag would reshuffle the reservation.
                roles = {
                    "isBooker": matched.get("isBooker") or "N",
                    "isPrimary": matched.get("isPrimary") or "N",
                    "isAdditional": matched.get("isAdditional") or "N",
                }
                guest_ids.append(matched.get("id"))
                logger.info(f"Guest {i+1} ({name}) matches existing guest {matched.get('id')}")
            else:
                # Only one guest can be primary; if the reservation already has
                # one, everyone we add is an additional guest.
                takes_primary = not has_primary
                has_primary = has_primary or takes_primary
                roles = {
                    "isBooker": "N",
                    "isPrimary": "Y" if takes_primary else "N",
                    "isAdditional": "N" if takes_primary else "Y",
                }
                guest_ids.append(None)

            api_guest = {"name": name, **roles}
            
            # Date of birth (UTC midnight to avoid timezone off-by-one)
            if guest.date_of_birth:
                ts = convert_date_to_timestamp(guest.date_of_birth)
                if ts:
                    api_guest["dateOfBirth"] = ts
                    logger.info(f"Guest {name}: dateOfBirth={guest.date_of_birth} -> ts={ts}")
            
            # Gender
            if guest.gender:
                gender_id = convert_gender_to_id(guest.gender)
                if gender_id:
                    api_guest["genderId"] = gender_id
            
            # Country fields
            if country_id:
                api_guest["countryId"] = country_id
                api_guest["citizenshipCountryId"] = country_id
                api_guest["countryOfBirthId"] = country_id
                api_guest["countryOfResidenceId"] = country_id
            
            # City of residence
            if guest.place_of_residence:
                api_guest["cityOfResidence"] = guest.place_of_residence
            
            # Street address
            if hasattr(guest, 'address') and guest.address:
                api_guest["address"] = guest.address
            
            # Build note with document info as backup
            note_parts = []
            if guest.document_number:
                note_parts.append(f"Doc: {guest.document_number}")
            if guest.expiry_date:
                note_parts.append(f"Exp: {guest.expiry_date}")
            if guest.oib:
                note_parts.append(f"OIB: {guest.oib}")
            if note_parts:
                api_guest["note"] = " | ".join(note_parts)
            
            # Collect document data for PUT update
            doc_fields = {}
            if guest.document_number:
                doc_fields["documentNumber"] = str(guest.document_number)
            doc_type = getattr(guest, 'document_type', None)
            if doc_type:
                doc_type_id = _get_document_type_id(doc_type, guest.nationality)
                if doc_type_id:
                    doc_fields["eVisitorDocumentTypeId"] = doc_type_id
            doc_fields["arrivalArrangementId"] = 2   # Personal (1 is Agency)
            doc_fields["providedServicesTypeId"] = 1  # Accommodation
            doc_fields["eVisitorTouristTaxCategoryId"] = _get_tourist_tax_category(
                guest.date_of_birth
            )
            guest_doc_data.append(doc_fields)
            
            logger.info(f"Guest {i+1} POST data: {api_guest}")
            logger.info(f"Guest {i+1} doc fields (for PUT): {doc_fields}")
            api_guests.append(api_guest)
        
        messages = []
        added = []

        # Phase 1: POST - create only the guests not already on the reservation
        new_indices = [i for i, gid in enumerate(guest_ids) if gid is None]
        if new_indices:
            result = await api.add_reservation_guests(
                reservation_id, [api_guests[i] for i in new_indices]
            )
            added = result.get('guestAdded', [])
            messages = list(result.get('messages', []))
            logger.info(f"POST result: added={added}, messages={messages}")
            for slot, new_id in zip(new_indices, added):
                guest_ids[slot] = new_id
            if len(added) != len(new_indices):
                logger.warning(
                    f"POSTed {len(new_indices)} guest(s) but got {len(added)} id(s) back"
                )
        else:
            logger.info("Every guest already existed on the reservation - nothing to POST")

        # Phase 2: PUT - document and eVisitor fields, for matched and new alike
        update_guests = []
        for i, guest_id in enumerate(guest_ids):
            if guest_id is None or not guest_doc_data[i]:
                continue
            update_obj = {
                "id": guest_id,
                **api_guests[i],
                **guest_doc_data[i],
            }
            update_guests.append(update_obj)
            logger.info(f"Guest {i+1} PUT data: {update_obj}")

        if update_guests:
            try:
                update_result = await api.update_reservation_guests(
                    reservation_id, update_guests
                )
                updated_ids = update_result.get('guestUpdated', [])
                update_msgs = update_result.get('messages', [])
                logger.info(f"PUT result: updated={updated_ids}, messages={update_msgs}")
                if update_msgs:
                    messages.extend(update_msgs)
            except Exception as e:
                logger.error(f"PUT update failed: {e}")
                messages.append("⚠️ Dokument polja: potreban ručni unos")
        
        # Verify against the same endpoint we wrote to, so a field that did not
        # stick shows up here instead of surfacing weeks later in eVisitor.
        REQUIRED_FOR_EVISITOR = (
            "documentNumber",
            "eVisitorDocumentTypeId",
            "eVisitorTouristTaxCategoryId",
            "dateOfBirth",
        )
        try:
            saved = await api.get_reservation_guests_v2(reservation_id)
            incomplete = []
            for g in saved:
                missing = [f for f in REQUIRED_FOR_EVISITOR if not g.get(f)]
                logger.info(
                    f"Verify guest {g.get('id')}: "
                    + ", ".join(f"{f}={g.get(f)}" for f in REQUIRED_FOR_EVISITOR)
                    + f", arrivalArrangementId={g.get('arrivalArrangementId')}"
                    + f", providedServicesTypeId={g.get('providedServicesTypeId')}"
                )
                if missing:
                    incomplete.append(f"{g.get('name', g.get('id'))}: {', '.join(missing)}")

            if incomplete:
                messages.append("⚠️ Nedostaje za eVisitor — " + " | ".join(incomplete))
        except Exception as e:
            logger.warning(f"Verify GET failed: {e}")
            messages.append("⚠️ Nisam mogao provjeriti spremljene podatke")
        
        # Mark checked-in once every guest is on the reservation, whether we
        # created them or updated one that was already there.
        checkin_status = ""
        if any(guest_ids):
            try:
                checkin_result = await api.checkin_reservation(reservation_id)
                logger.info(f"Checkin result: {checkin_result}")
                checkin_status = "\n✅ Rezervacija označena kao checked-in"
            except RentlioAPIError as e:
                logger.warning(f"Checkin status update failed: {e.message}")
                checkin_status = f"\n⚠️ Gosti dodani, ali checkin status: {e.message}"
        
        # Build success message
        guest_name = reservation_data.get('guestName', 'N/A')
        unit_name = reservation_data.get('unitName', 'N/A')
        arrival = format_date(reservation_data.get('arrivalDate', 0))
        departure = format_date(reservation_data.get('departureDate', 0))
        
        # Guest summary
        guest_summary = ""
        for i, guest in enumerate(guests):
            name = guest.full_name or f"{guest.first_name} {guest.last_name}".strip()
            country = guest.nationality or "N/A"
            success = "✅" if guest_ids[i] else "⚠️"
            guest_summary += f"\n{success} {name} ({country})"
        
        # Check if every guest ended up on the reservation
        if all(guest_ids):
            status_text = "✅ **Check-in uspješan!**"
        elif any(guest_ids):
            status_text = "⚠️ **Djelomično uspješno**"
        else:
            status_text = "❌ **Check-in nije uspio**"
        
        # Show any messages from API
        msg_text = ""
        if messages:
            msg_text = "\n\n📝 API poruke:\n" + "\n".join(f"• {m[:100]}" for m in messages[:3])
        
        await query.edit_message_text(
            f"{status_text}\n\n"
            f"📋 Rezervacija: #{reservation_id}\n"
            f"👤 Booker: {guest_name}\n"
            f"🏠 {unit_name}\n"
            f"📅 {arrival} → {departure}\n\n"
            f"**Prijavljeni gosti:**{guest_summary}"
            f"{checkin_status}"
            f"{msg_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 Generiraj račun", callback_data=f"checkin_invoice_{reservation_id}")],
                [InlineKeyboardButton("✅ Gotovo", callback_data="checkin_done")]
            ])
        )
        
        # Store for potential invoice generation
        context.user_data['checkin_completed_reservation'] = reservation_id
        context.user_data['checkin_completed_reservation_data'] = reservation_data
        
    except RentlioAPIError as e:
        logger.error(f"API Check-in error: {e.message}, data: {e.response_data}")
        await query.edit_message_text(
            f"❌ **API Greška**\n\n"
            f"{e.message}\n\n"
            f"Pokušaj ponovo ili koristi Rentlio UI za ručni unos."
        )
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Check-in error: {e}")
        await query.edit_message_text(f"❌ Greška: {str(e)}")
        context.user_data.clear()


# ========== Photo / Check-in Flow ==========

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ID photo - automatically starts API check-in flow"""
    
    # Initialize check-in session if not already in one
    if 'checkin_guests' not in context.user_data:
        context.user_data['checkin_guests'] = []
        # Load countries on first photo
        await country_mapper.load_countries(api)
    
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
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="🗑️ _Slika obrisana iz sigurnosnih razloga_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not delete photo: {e}")
        
        if not guest_data.is_valid():
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="❌ **Nisam uspio izvući podatke**\n\n"
                     f"Pokušaj s boljom slikom (fokus, osvjetljenje).\n\n"
                     f"Raw text:\n```\n{guest_data.raw_text[:300]}...```",
                parse_mode="Markdown"
            )
            return
        
        # Add guest to check-in list (using ExtractedGuestData object directly)
        context.user_data['checkin_guests'].append(guest_data)
        guest_count = len(context.user_data['checkin_guests'])
        
        # Show extracted data and offer to continue or proceed
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"{guest_data.format_telegram()}\n\n"
                 f"✅ **Gost {guest_count} dodan!**\n\n"
                 f"📷 Pošalji još slika ili klikni **Nastavi** 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Nastavi ({guest_count} gost/a)", callback_data="checkin_select_reservation")],
                [InlineKeyboardButton("❌ Odustani", callback_data="checkin_cancel")]
            ])
        )
        
    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=f"❌ Greška: {str(e)}"
        )


async def create_invoice_for_reservation(query, context, reservation_id: str, guest: dict, reservation_data: dict = None):
    """Create invoice for a reservation with guest info"""
    guest_name = guest.get('fullName', 'Gost')
    country = guest.get('nationality', guest.get('country', 'N/A'))
    today = datetime.now().strftime("%d.%m.%Y")
    
    await query.edit_message_text(
        f"⏳ Kreiram račun za rezervaciju #{reservation_id}..."
    )
    
    try:
        # Get reservation details
        if reservation_data:
            unit_name = reservation_data.get('unitName', 'Smještaj')
            price_per_night = reservation_data.get('pricePerNight', 60)
            total_nights = reservation_data.get('totalNights', 1)
            arrival_ts = reservation_data.get('arrivalDate', 0)
            departure_ts = reservation_data.get('departureDate', 0)
            
            # Format dates (dd.mm.)
            if arrival_ts:
                arrival_dt = datetime.fromtimestamp(arrival_ts)
                arrival_str = arrival_dt.strftime("%d.%m.")
            else:
                arrival_str = today[:6]
            
            if departure_ts:
                departure_dt = datetime.fromtimestamp(departure_ts)
                departure_str = departure_dt.strftime("%d.%m.")
            else:
                departure_str = today[:6]
            
            # Determine payment type based on channel
            channel = reservation_data.get('otaChannelName', '').lower()
            sales_channel = reservation_data.get('salesChannelName', '').lower()
            origin = reservation_data.get('origin', 0)
            
            # origin: 1 = manual, 2+ = channel booking
            # Check if it's from Booking.com, Airbnb, or other OTA
            is_ota = ('booking' in channel or 'airbnb' in channel or 
                      'booking' in sales_channel or 'airbnb' in sales_channel or
                      origin > 1)
            
            payment_type = "Transakcijski račun" if is_ota else "Gotovina"
        else:
            unit_name = "Smještaj"
            price_per_night = 60
            total_nights = 1
            arrival_str = today[:6]
            departure_str = today[:6]
            payment_type = "Gotovina"
        
        # Format description like: "Smještaj Sunset (19.01. - 22.01.)"
        description = f"Smještaj {unit_name} ({arrival_str} - {departure_str})"
        
        result = await api.add_invoice_item(
            reservation_id=reservation_id,
            description=description,
            price=price_per_night,
            quantity=total_nights,
            discount_percent=0,
            vat_included="Y",
            taxes=[{"label": "PDV", "rate": 13}]
        )
        
        if result:
            item_total = price_per_night * total_nights
            await query.edit_message_text(
                f"✅ **Račun kreiran!**\n\n"
                f"📋 Rezervacija: #{reservation_id}\n"
                f"👤 Gost: **{guest_name}**\n"
                f"🌍 Država: {country}\n"
                f"🏠 {description}\n"
                f"💰 {price_per_night:.2f}€ x {total_nights} noći = **{item_total:.2f}€**\n"
                f"💳 Plaćanje: {payment_type}\n"
                f"📅 Datum: {today}\n\n"
                f"⚠️ _Račun je kreiran kao DRAFT._\n"
                f"_Zaključi ga ručno u Rentlio sustavu (Izdaj račun)._",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"⚠️ Račun možda nije kreiran. Provjeri u Rentlio sustavu."
            )
        
    except RentlioAPIError as e:
        logger.error(f"Invoice API error: {e.message}, data: {e.response_data}")
        await query.edit_message_text(f"❌ API Greška: {e.message}")
    except Exception as e:
        logger.error(f"Invoice creation error: {e}")
        await query.edit_message_text(f"❌ Greška: {str(e)}")
    
    context.user_data.clear()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks"""
    query = update.callback_query
    await query.answer()
    
    # ========== NEW API Check-in Callbacks ==========
    
    if query.data.startswith("occ:"):
        await handle_analysis_callback(query, context)
        return

    if query.data == "checkin_cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Check-in otkazan.")
        return
    
    elif query.data == "checkin_select_reservation":
        await show_reservation_selection(query, context)
        return
    
    elif query.data.startswith("checkin_res_"):
        reservation_id = query.data.replace("checkin_res_", "")
        await perform_api_checkin(query, context, reservation_id)
        return
    
    elif query.data.startswith("checkin_invoice_"):
        reservation_id = query.data.replace("checkin_invoice_", "")
        guests = context.user_data.get('checkin_guests', [])
        reservation_data = context.user_data.get('checkin_completed_reservation_data', {})
        
        if guests:
            # Convert first guest to invoice format
            first_guest = guests[0]
            guest_dict = {
                'fullName': first_guest.full_name or f"{first_guest.first_name} {first_guest.last_name}".strip(),
                'nationality': first_guest.nationality or 'N/A'
            }
            await create_invoice_for_reservation(query, context, reservation_id, guest_dict, reservation_data)
        else:
            await query.edit_message_text("⚠️ Nema podataka o gostima za račun.")
            context.user_data.clear()
        return
    
    elif query.data == "checkin_done":
        await query.edit_message_text(
            "✅ **Check-in završen!**\n\n"
            "Gosti su prijavljeni u Rentlio sustav.\n"
            "Provjeri podatke u Rentlio aplikaciji."
        )
        context.user_data.clear()
        return
    
    # ========== Invoice Callbacks ==========
    
    if query.data == "skip_invoice":
        await query.edit_message_text("👍 OK, bez računa.")
        context.user_data.clear()
    
    # Invoice callbacks
    elif query.data.startswith("add_item_"):
        reservation_id = query.data.replace("add_item_", "")
        context.user_data['invoice_reservation_id'] = reservation_id
        context.user_data['state'] = 'waiting_for_invoice_item'
        
        await query.edit_message_text(
            f"➕ **Dodaj stavku na račun**\n\n"
            f"Rezervacija: #{reservation_id}\n\n"
            f"Upiši stavku u formatu:\n"
            f"`naziv, cijena, količina`\n\n"
            f"Primjeri:\n"
            f"• `Boravišna pristojba, 1.35, 4`\n"
            f"• `Parking, 10, 3`\n"
            f"• `Doručak, 8, 2`\n\n"
            f"Ili upiši samo `/cancel` za odustajanje.",
            parse_mode="Markdown"
        )
    
    elif query.data.startswith("invoice_details_"):
        invoice_id = query.data.replace("invoice_details_", "")
        
        try:
            invoice = await api.get_invoice_details(invoice_id)
            
            text = f"📋 **Račun #{invoice_id}**\n"
            text += f"━━━━━━━━━━━━━━━━━━━━\n\n"
            
            # Status
            status_code = invoice.get("status", 1)
            status_names = {1: "📝 Draft", 2: "📄 Issued", 3: "✅ Fiscalised"}
            text += f"Status: {status_names.get(status_code, 'Unknown')}\n"
            text += f"Datum: {format_date(invoice.get('date', 0))}\n\n"
            
            # Items
            items = invoice.get("items", [])
            if items:
                text += "**Stavke:**\n"
                for item in items:
                    desc = item.get("description", "N/A")
                    price = item.get("price", 0)
                    qty = item.get("quantity", 1)
                    total = item.get("totalPrice", price * qty)
                    text += f"• {desc}\n"
                    text += f"  {price:.2f} x {qty} = {total:.2f} EUR\n"
            
            # Totals
            text += f"\n━━━━━━━━━━━━━━━━━━━━\n"
            text += f"**Ukupno: {invoice.get('totalValue', 0):.2f} EUR**\n"
            
            # Taxes
            taxes = invoice.get("taxes", [])
            if taxes:
                text += "\nPorezi:\n"
                for tax in taxes:
                    text += f"• {tax.get('label', 'PDV')} ({tax.get('rate', 0)}%): {tax.get('value', 0):.2f} EUR\n"
            
            await query.edit_message_text(text, parse_mode="Markdown")
            
        except RentlioAPIError as e:
            await query.edit_message_text(f"❌ Greška: {e.message}")
        except Exception as e:
            logger.error(f"Invoice details error: {e}")
            await query.edit_message_text(f"❌ Greška: {str(e)}")
    
    elif query.data == "invoice_done":
        await query.edit_message_text(
            "✅ **Račun spremljen!**\n\n"
            "Račun je u draft statusu u Rentlio sustavu.\n"
            "Možeš ga pregledati i izdati u Rentlio web aplikaciji.",
            parse_mode="Markdown"
        )
        context.user_data.clear()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages - check if it's a URL or menu button"""
    text = update.message.text
    
    # Check for cancel command
    if text.lower() == '/cancel':
        context.user_data.clear()
        await update.message.reply_text("❌ Akcija otkazana.")
        return
    
    # Check if waiting for invoice item input
    if context.user_data.get('state') == 'waiting_for_invoice_item':
        reservation_id = context.user_data.get('invoice_reservation_id')
        
        try:
            # Parse input: "description, price, quantity"
            parts = [p.strip() for p in text.split(',')]
            
            if len(parts) < 2:
                await update.message.reply_text(
                    "⚠️ Format: `naziv, cijena, količina`\n\n"
                    "Primjer: `Parking, 10, 3`\n\n"
                    "Ili `/cancel` za odustajanje.",
                    parse_mode="Markdown"
                )
                return
            
            description = parts[0]
            price = float(parts[1])
            quantity = float(parts[2]) if len(parts) > 2 else 1
            
            await update.message.reply_text(f"⏳ Dodajem stavku na račun...")
            
            # Add item to invoice
            result = await api.add_invoice_item(
                reservation_id=reservation_id,
                description=description,
                price=price,
                quantity=quantity,
                vat_included="Y",
                taxes=[{"label": "PDV", "rate": 25}]  # Default 25% VAT
            )
            
            item_total = result.get("totalPrice", price * quantity)
            
            # Offer to add more or done
            keyboard = [
                [InlineKeyboardButton("➕ Dodaj još", callback_data=f"add_item_{reservation_id}")],
                [InlineKeyboardButton("✅ Gotovo", callback_data="invoice_done")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **Stavka dodana!**\n\n"
                f"📦 {description}\n"
                f"💰 {price:.2f} x {quantity} = {item_total:.2f} EUR\n\n"
                f"Dodaj još ili završi:",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            
            context.user_data.pop('state', None)
            
        except ValueError:
            await update.message.reply_text(
                "⚠️ Neispravan format. Cijena mora biti broj.\n\n"
                "Primjer: `Parking, 10, 3`",
                parse_mode="Markdown"
            )
        except RentlioAPIError as e:
            await update.message.reply_text(f"❌ API Greška: {e.message}")
            context.user_data.clear()
        except Exception as e:
            logger.error(f"Add invoice item error: {e}")
            await update.message.reply_text(f"❌ Greška: {str(e)}")
            context.user_data.clear()
        return
    
    # Check if waiting for reservation ID for invoice after check-in
    if context.user_data.get('state') == 'waiting_for_invoice_reservation_id':
        reservation_id = text.strip()
        
        if not reservation_id.isdigit():
            await update.message.reply_text(
                "⚠️ Reservation ID mora biti broj.\n\n"
                "Primjer: `12345`\n\n"
                "`/cancel` za odustajanje."
            )
            return
        
        # Get selected guest info
        selected_guest = context.user_data.get('invoice_selected_guest', {})
        guest_name = selected_guest.get('fullName', 'N/A')
        guest_country = selected_guest.get('nationality', selected_guest.get('country', 'N/A'))
        today_date = datetime.now().strftime("%d.%m.%Y")
        
        await update.message.reply_text(f"⏳ Kreiram račun za rezervaciju #{reservation_id}...")
        
        try:
            # Get reservation details for pricing
            reservation = await api.get_reservation_details(reservation_id)
            total_price = reservation.get("totalPrice", 0)
            nights = reservation.get("totalNights", 1)
            unit_name = reservation.get("unitName", "Smještaj")
            
            # Calculate dates for description
            arrival = format_date(reservation.get("arrivalDate", 0))
            departure = format_date(reservation.get("departureDate", 0))
            
            # Add accommodation as invoice item with guest info in description
            result = await api.add_invoice_item(
                reservation_id=reservation_id,
                description=f"Smještaj u {unit_name} ({arrival} - {departure})",
                price=total_price,
                quantity=1,
                vat_included="Y",  # Price includes VAT
                taxes=[{"label": "PDV", "rate": 13}]  # 13% VAT for accommodation in Croatia
            )
            
            item_total = result.get("totalPrice", total_price)
            
            # Offer to add more items
            keyboard = [
                [InlineKeyboardButton("➕ Dodaj stavku", callback_data=f"add_item_{reservation_id}")],
                [InlineKeyboardButton("✅ Gotovo", callback_data="invoice_done")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **Račun kreiran!**\n\n"
                f"👤 Gost: **{guest_name}**\n"
                f"🌍 Država: {guest_country}\n"
                f"📅 Datum: {today_date}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Smještaj u {unit_name}\n"
                f"🗓 {arrival} - {departure} ({nights} noći)\n"
                f"💰 Ukupno: {item_total:.2f} EUR\n\n"
                f"_Račun je u statusu 'Draft'_\n\n"
                f"Želiš dodati još stavki?",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            
            # Store for potential additional items
            context.user_data['invoice_reservation_id'] = reservation_id
            
        except RentlioAPIError as e:
            await update.message.reply_text(f"❌ API Greška: {e.message}")
            context.user_data.clear()
        except Exception as e:
            logger.error(f"Invoice creation error: {e}")
            await update.message.reply_text(f"❌ Greška: {str(e)}")
            context.user_data.clear()
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
    elif "Analiza" in text:
        await analysis_command(update, context)
    elif "Help" in text:
        await help_command(update, context)


async def setup_bot_commands(app: Application):
    """Set up bot commands menu in Telegram"""
    commands = [
        BotCommand("start", "Pokreni bota"),
        BotCommand("checkin", "🆕 API Check-in (bez forme!)"),
        BotCommand("current", "🏠 Trenutni gosti"),
        BotCommand("today", "Današnji dolasci"),
        BotCommand("tomorrow", "Sutrašnji dolasci"),
        BotCommand("checkouts", "Odlasci danas/sutra"),
        BotCommand("cleaning", "🧹 Raspored čišćenja (7 dana)"),
        BotCommand("upcoming", "Dolasci sljedećih 7 dana"),
        BotCommand("week", "📊 Tjedna statistika"),
        BotCommand("analiza", "🤖 AI analiza popunjenosti i cijena"),
        BotCommand("search", "Pretraži po imenu gosta"),
        BotCommand("invoice", "Upravljanje računima"),
        BotCommand("help", "Pomoć"),
    ]
    await app.bot.set_my_commands(commands)


async def get_daily_summary() -> tuple[list, list, list]:
    """Get today's arrivals, departures, and tomorrow's arrivals"""
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    
    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    
    today_ts_start = int(today.replace(hour=0, minute=0, second=0).timestamp())
    today_ts_end = int(today.replace(hour=23, minute=59, second=59).timestamp())
    tomorrow_ts_start = int(tomorrow.replace(hour=0, minute=0, second=0).timestamp())
    tomorrow_ts_end = int(tomorrow.replace(hour=23, minute=59, second=59).timestamp())
    
    # Get reservations for today and tomorrow
    # Note: Rentlio API returns reservations overlapping the date range
    all_reservations = await api.get_reservations(
        date_from=today_str,
        date_to=tomorrow_str,
        limit=100
    )
    
    # Keep every live reservation; only refused/cancelled/deleted are dropped
    all_reservations = [r for r in all_reservations if is_live_reservation(r)]
    
    arrivals = []
    departures = []
    tomorrow_arrivals = []
    
    # Use set to track reservation IDs and avoid duplicates
    seen_arrival_ids = set()
    seen_departure_ids = set()
    seen_tomorrow_ids = set()
    
    for res in all_reservations:
        arrival_ts = res.get("arrivalDate", 0)
        departure_ts = res.get("departureDate", 0)
        res_id = res.get("id")
        
        # Today's arrivals - exact match on arrival date
        if today_ts_start <= arrival_ts <= today_ts_end and res_id not in seen_arrival_ids:
            arrivals.append(res)
            seen_arrival_ids.add(res_id)
        
        # Today's departures - exact match on departure date
        if today_ts_start <= departure_ts <= today_ts_end and res_id not in seen_departure_ids:
            departures.append(res)
            seen_departure_ids.add(res_id)
        
        # Tomorrow's arrivals - exact match on arrival date
        if tomorrow_ts_start <= arrival_ts <= tomorrow_ts_end and res_id not in seen_tomorrow_ids:
            tomorrow_arrivals.append(res)
            seen_tomorrow_ids.add(res_id)
    
    return arrivals, departures, tomorrow_arrivals


async def send_daily_notification(context: ContextTypes.DEFAULT_TYPE):
    """Send daily check-in/check-out notification with tomorrow's reminder"""
    logger.info("Checking for daily arrivals/departures...")
    
    try:
        arrivals, departures, tomorrow_arrivals = await get_daily_summary()
        
        # Skip if nothing happening today AND tomorrow
        if not arrivals and not departures and not tomorrow_arrivals:
            logger.info("No arrivals or departures - skipping notification")
            return
        
        today = datetime.now()
        today_str = today.strftime("%d.%m.%Y")
        tomorrow_str = (today + timedelta(days=1)).strftime("%d.%m.%Y")
        
        # Build message with cleaner format
        text = f"🌅 **Dnevni pregled - {today_str}**\n\n"
        
        # Today's Departures (CHECK-OUT) - show first as they leave
        if departures:
            text += f"🔴 **ODLASCI DANAS ({len(departures)})**\n"
            # Group by unit
            by_unit = {}
            for res in departures:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    text += f"• {guest} ← {unit}\n"
            text += "\n"
        
        # Today's Arrivals (CHECK-IN)
        if arrivals:
            text += f"🟢 **DOLASCI DANAS ({len(arrivals)})**\n"
            # Group by unit
            by_unit = {}
            for res in arrivals:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                text += f"  🏠 _{unit}_\n"
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    phone = res.get("guestContactNumber", "")
                    nights = res.get("totalNights", 0)
                    text += f"  • {guest} ({nights} {'noć' if nights == 1 else 'noći'})\n"
                    if phone:
                        text += f"    📞 {phone}\n"
            text += "\n"
        
        # Tomorrow's Arrivals (REMINDER - send instructions!)
        if tomorrow_arrivals:
            text += f"📅 **SUTRA DOLAZE ({len(tomorrow_arrivals)}) - {tomorrow_str}**\n"
            text += "⚠️ _Pošalji upute gostima!_\n\n"
            
            # Group by unit
            by_unit = {}
            for res in tomorrow_arrivals:
                unit = res.get("unitName", "")
                if unit not in by_unit:
                    by_unit[unit] = []
                by_unit[unit].append(res)
            
            for unit in sorted(by_unit.keys()):
                text += f"  🏠 _{unit}_\n"
                for res in by_unit[unit]:
                    guest = res.get("guestName", "Unknown")
                    phone = res.get("guestContactNumber", "")
                    nights = res.get("totalNights", 0)
                    email = res.get("guestEmail", "")
                    
                    text += f"  • **{guest}** ({nights} {'noć' if nights == 1 else 'noći'})\n"
                    if phone:
                        text += f"    📞 {phone}\n"
                    if email:
                        text += f"    ✉️ {email}\n"
        
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


async def send_monthly_cleaning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Send monthly reminder to restock dishwasher supplies"""
    logger.info("Sending monthly cleaning supplies reminder...")
    text = (
        "🧹 *Mjesečni podsjetnik — Nadopunjavanje*\n\n"
        "Provjeri i nadopuni sljedeće:\n\n"
        "• 🫧 Tekućina za sjaj u perilici\n"
        "• 🧂 Sol u perilici\n"
        "• 🧂 Sol u kuhinji\n"
        "• 🍬 Šećer u kuhinji"
    )
    for user_id in config.TELEGRAM_ALLOWED_USERS:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown"
            )
            logger.info(f"Sent monthly cleaning reminder to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send monthly cleaning reminder to {user_id}: {e}")


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


# ========== Invoice Commands ==========

async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    View or manage invoices for a reservation
    Usage: /invoice <reservation_id>
    """
    if not context.args:
        await update.message.reply_text(
            "📋 **Upravljanje računima**\n\n"
            "Korištenje: `/invoice <reservation_id>`\n\n"
            "Primjer: `/invoice 12345`\n\n"
            "Možeš pronaći reservation ID:\n"
            "• U detaljima rezervacije\n"
            "• Koristi /search pa klikni na rezervaciju",
            parse_mode="Markdown"
        )
        return
    
    reservation_id = context.args[0]
    
    await update.message.reply_text(f"⏳ Dohvaćam račune za rezervaciju {reservation_id}...")
    
    try:
        # Get reservation details first
        reservation = await api.get_reservation_details(reservation_id)
        guest_name = reservation.get("holder", {}).get("name", "N/A")
        unit_name = reservation.get("unitName", "N/A")
        
        # Get invoices for this reservation
        invoices = await api.get_reservation_invoices(reservation_id)
        
        if not invoices:
            # No invoices yet - offer to create one
            keyboard = [
                [InlineKeyboardButton("➕ Dodaj stavku", callback_data=f"add_item_{reservation_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🧾 **Rezervacija #{reservation_id}**\n"
                f"👤 {guest_name}\n"
                f"🏠 {unit_name}\n\n"
                f"📭 Nema računa za ovu rezervaciju.\n\n"
                f"Klikni dolje za dodavanje stavke (kreira se draft račun automatski).",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            # Show existing invoices
            text = f"🧾 **Računi za rezervaciju #{reservation_id}**\n"
            text += f"👤 {guest_name} | 🏠 {unit_name}\n\n"
            
            for inv in invoices:
                inv_id = inv.get("id", "N/A")
                inv_date = format_date(inv.get("date", 0))
                status = inv.get("status", {})
                status_name = status.get("name", "Draft") if isinstance(status, dict) else "Draft"
                total = inv.get("totalValue", 0)
                
                status_emoji = {
                    "Draft": "📝",
                    "Issued": "📄",
                    "Fiscalised": "✅"
                }.get(status_name, "📋")
                
                text += f"{status_emoji} **Račun #{inv_id}**\n"
                text += f"   📅 {inv_date} | {status_name}\n"
                text += f"   💰 {total:.2f} EUR\n\n"
            
            keyboard = [
                [InlineKeyboardButton("➕ Dodaj stavku", callback_data=f"add_item_{reservation_id}")],
                [InlineKeyboardButton("📋 Detalji računa", callback_data=f"invoice_details_{invoices[0].get('id', '')}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            
    except RentlioAPIError as e:
        await update.message.reply_text(f"❌ API Greška: {e.message}")
    except Exception as e:
        logger.error(f"Invoice command error: {e}")
        await update.message.reply_text(f"❌ Greška: {str(e)}")


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
    app.add_handler(CommandHandler("checkouts", checkouts_command))
    app.add_handler(CommandHandler("cleaning", cleaning_schedule))
    app.add_handler(CommandHandler("current", current_guests))
    app.add_handler(CommandHandler("week", week_stats))
    app.add_handler(CommandHandler("analiza", analysis_command))
    app.add_handler(CommandHandler("analysis", analysis_command))
    app.add_handler(CommandHandler("search", search_guest))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("notifications", toggle_notifications))
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("checkin", checkin_command))  # NEW API check-in
    app.add_handler(CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Akcija otkazana.") or c.user_data.clear()))
    
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
        
        # Schedule daily notification (if job_queue is available)
        job_queue = application.job_queue
        if job_queue and config.TELEGRAM_ALLOWED_USERS:
            job_queue.run_daily(
                send_daily_notification,
                time=NOTIFICATION_TIME,
                name="daily_notification"
            )
            job_queue.run_monthly(
                send_monthly_cleaning_reminder,
                when=NOTIFICATION_TIME,
                day=1,
                name="monthly_cleaning_reminder"
            )
            print(f"📅 Daily notifications scheduled for {NOTIFICATION_TIME.strftime('%H:%M')}")
            print(f"🧹 Monthly cleaning reminder scheduled for 1st of each month at {NOTIFICATION_TIME.strftime('%H:%M')}")
            print(f"👤 Notifying users: {config.TELEGRAM_ALLOWED_USERS}")
        elif not job_queue:
            print("⚠️  JobQueue not available - install with: pip install 'python-telegram-bot[job-queue]'")
        else:
            print("⚠️  No TELEGRAM_ALLOWED_USERS set - notifications disabled")
            print("   Use /notifications in the bot to get your user ID")
    
    # Run bot
    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
