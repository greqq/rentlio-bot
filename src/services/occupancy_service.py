"""
Fetches the data the occupancy analysis needs and renders it for Telegram.

`occupancy_analyzer` is pure logic; everything that talks to Rentlio, caches
results or formats Croatian text lives here, so the bot only has to call
`run_analysis()` and print what comes back.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

from src.config import config
from src.services.rentlio_api import RentlioAPI, RentlioAPIError, is_live_reservation
from src.services.occupancy_analyzer import (
    Gap,
    OccupancyAnalyzer,
    OccupancyReport,
    PricingConfig,
    RateInfo,
    Stay,
    parse_stays,
    shift_years,
    to_date,
)

logger = logging.getLogger(__name__)

WEEKDAYS_HR = ["pon", "uto", "sri", "čet", "pet", "sub", "ned"]
MONTHS_HR = [
    "sijecanj", "veljaca", "ozujak", "travanj", "svibanj", "lipanj",
    "srpanj", "kolovoz", "rujan", "listopad", "studeni", "prosinac",
]

# Reservations that started before today still occupy tonight, so the fetch
# window reaches back before the horizon.
LOOKBACK_DAYS = 45
# Historical windows are padded so the +/- smoothing window has data at the edges.
HISTORY_PADDING_DAYS = 21

# Two seasons of history are four extra API round trips; a short cache keeps
# repeated /analiza taps from re-fetching everything.
CACHE_TTL_SECONDS = 30 * 60

_cache: dict[tuple, tuple[float, OccupancyReport]] = {}


def _unit_name(unit: dict) -> Optional[str]:
    for key in ("name", "unitName", "title", "label"):
        value = unit.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _fetch_units(api: RentlioAPI, property_id: Optional[str]) -> list[str]:
    try:
        raw_units = await api.get_units(property_id)
    except Exception as e:  # noqa: BLE001 - unit list is a nice-to-have
        logger.warning("Could not read units from API: %s", e)
        return []
    names = [_unit_name(u) for u in raw_units if isinstance(u, dict)]
    return sorted({n for n in names if n})


async def _fetch_stays(
    api: RentlioAPI,
    date_from: date,
    date_to: date,
    property_id: Optional[str],
) -> list[Stay]:
    reservations = await api.get_all_reservations(
        date_from=date_from.strftime("%Y-%m-%d"),
        date_to=date_to.strftime("%Y-%m-%d"),
        property_id=property_id,
    )
    live = [r for r in reservations if is_live_reservation(r)]
    return parse_stays(live)


async def _fetch_rate_calendar(
    api: RentlioAPI,
    property_id: Optional[str],
    start: date,
    end: date,
) -> dict[str, dict[date, RateInfo]]:
    """
    What the host currently has listed: price and minimum stay per night.

    Rates and restrictions hang off unit *types*, not units, and the two carry
    different ids - asking with a unit id answers 403, which looks like a
    permissions problem and is not. Unit type names match the unit names that
    reservations report, which is how the two sides are joined here.

    Returns {} rather than raising: the analysis still works off historical
    prices, it just cannot quote the host's own rate back to them.
    """
    if not property_id:
        try:
            properties = await api.get_properties()
        except RentlioAPIError as e:
            logger.warning("Could not list properties for rates: %s", e)
            return {}
        if len(properties) != 1:
            return {}
        property_id = str(properties[0].get("id", ""))

    try:
        unit_types = await api.get_unit_types(property_id)
    except RentlioAPIError as e:
        logger.warning("Could not list unit types: %s", e)
        return {}

    date_from = start.strftime("%Y-%m-%d")
    date_to = end.strftime("%Y-%m-%d")
    calendar: dict[str, dict[date, RateInfo]] = {}

    for unit_type in unit_types:
        type_id = unit_type.get("id")
        name = (unit_type.get("name") or "").strip()
        if type_id is None or not name:
            continue
        try:
            rates = await api.get_unit_type_rates(str(type_id), date_from, date_to)
            restrictions = await api.get_unit_type_restrictions(
                str(type_id), date_from, date_to
            )
        except RentlioAPIError as e:
            logger.warning("Rates unavailable for unit type %s: %s", type_id, e)
            continue

        per_day: dict[date, RateInfo] = {}
        for row in rates:
            day = to_date(row.get("date"))
            if day:
                per_day.setdefault(day, RateInfo()).price = row.get("price")
        for row in restrictions:
            day = to_date(row.get("date"))
            if not day:
                continue
            info = per_day.setdefault(day, RateInfo())
            info.min_stay = row.get("minStay") or None
            info.closed = bool(row.get("closed"))

        if per_day:
            calendar[name] = per_day

    return calendar


async def run_analysis(
    api: RentlioAPI,
    horizon_days: int = 30,
    history_years: int = 2,
    property_id: Optional[str] = None,
    today: Optional[date] = None,
    use_cache: bool = True,
    pricing: Optional[PricingConfig] = None,
) -> OccupancyReport:
    """Read Rentlio, compare against past seasons, return the analysis."""
    today = today or date.today()
    property_id = property_id or config.RENTLIO_PROPERTY_ID or None
    cache_key = (today, horizon_days, history_years, property_id)

    if use_cache:
        cached = _cache.get(cache_key)
        if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

    end = today + timedelta(days=horizon_days - 1)

    current = await _fetch_stays(
        api, today - timedelta(days=LOOKBACK_DAYS), end + timedelta(days=1), property_id
    )

    history: dict[int, list[Stay]] = {}
    for years_back in range(1, history_years + 1):
        hist_from = shift_years(today, -years_back) - timedelta(days=HISTORY_PADDING_DAYS)
        hist_to = shift_years(end, -years_back) + timedelta(days=HISTORY_PADDING_DAYS)
        try:
            stays = await _fetch_stays(api, hist_from, hist_to, property_id)
        except Exception as e:  # noqa: BLE001 - a missing season must not kill the report
            logger.warning("History for %s year(s) back unavailable: %s", years_back, e)
            continue
        if stays:
            history[years_back] = stays

    units = await _fetch_units(api, property_id)
    if not units:
        # No unit endpoint - the apartments the bookings mention are the truth.
        units = sorted({s.unit for s in current} | {s.unit for stays in history.values() for s in stays})
    if config.RENTLIO_TOTAL_UNITS and len(units) < config.RENTLIO_TOTAL_UNITS:
        # A quiet horizon can hide an apartment entirely; keep the denominator honest.
        units = units + [f"Apartman {i}" for i in range(len(units) + 1, config.RENTLIO_TOTAL_UNITS + 1)]

    rate_calendar = await _fetch_rate_calendar(api, property_id, today, end)

    if pricing is None:
        pricing = PricingConfig(price_floors=dict(config.PRICE_FLOORS))

    analyzer = OccupancyAnalyzer(pricing)
    report = analyzer.analyze(
        today=today,
        horizon_days=horizon_days,
        current_stays=current,
        history=history,
        units=units,
        rate_calendar=rate_calendar,
    )

    _cache[cache_key] = (time.time(), report)
    return report


def clear_cache() -> None:
    _cache.clear()


# ========== Telegram rendering ==========

def _fmt_day(day: date) -> str:
    return f"{WEEKDAYS_HR[day.weekday()]} {day.strftime('%d.%m')}"


def _fmt_range(start: date, end: date) -> str:
    if start == end:
        return _fmt_day(start)
    return f"{_fmt_day(start)} – {_fmt_day(end)}"


def _bar(value: float, width: int = 10) -> str:
    filled = max(0, min(width, round(value * width)))
    return "█" * filled + "░" * (width - filled)


def format_summary(report: OccupancyReport) -> str:
    """Headline: where occupancy stands against previous seasons."""
    lines = [
        f"📊 ANALIZA · idućih {report.horizon_days} dana",
        f"{report.start.strftime('%d.%m')} – {report.end.strftime('%d.%m.%Y')}"
        f" · {', '.join(report.units)}",
        "",
        f"Sada    {_bar(report.occupancy)}  {report.occupancy * 100:.0f}%",
    ]

    hist = report.hist_occupancy
    if hist is not None:
        delta = (report.occupancy - hist) * 100
        lines.append(f"Prije   {_bar(hist)}  {hist * 100:.0f}%")
        years = ", ".join(str(report.start.year - y) for y in report.history_years)
        mark = "🟢" if delta >= 0 else "🔴"
        smjer = "iznad" if delta >= 0 else "ispod"
        lines.append(f"{mark} {abs(delta):.0f} p.b. {smjer} prosjeka ({years})")

    free = report.total_unit_nights - report.booked_unit_nights
    lines += [
        "",
        f"🛏 {report.booked_unit_nights}/{report.total_unit_nights} noćenja prodano"
        f" · {free} slobodno",
        f"💰 {report.booked_revenue:.0f} € rezervirano",
    ]
    if report.free_nights_value:
        lines.append(f"💸 ~{report.free_nights_value:.0f} € leži u slobodnim noćima")

    live = [d.free_price for d in report.days if d.free_price]
    if live:
        live_avg = sum(live) / len(live)
        line = f"🏷 Tvoja cijena: ~{live_avg:.0f} €"
        hist_prices = [d.hist_adr for d in report.days if d.hist_adr]
        if hist_prices:
            hist_avg = sum(hist_prices) / len(hist_prices)
            delta = live_avg - hist_avg
            if abs(delta) < 1:
                line += " (u rangu prijašnjih sezona)"
            else:
                smjer = "iznad" if delta >= 0 else "ispod"
                line += f" ({abs(delta):.0f} € {smjer} prijašnjih sezona)"
        lines.append(line)

    return "\n".join(lines)


def format_actions(report: OccupancyReport, limit: int = 10) -> str:
    """
    The part the host acts on.

    One block per action: when and where, the move, then a single line of why.
    Telegram renders a proportional font, so the layout leans on short lines
    and blank space rather than aligned columns.
    """
    if not report.actions:
        return "✅ Nema hitnih preporuka — kalendar prati očekivani tempo."

    icons = {
        "discount": "💸", "min_stay": "🔓", "raise": "📈",
        "hold": "⏸", "info": "ℹ️",
    }
    headers = {1: "ODMAH", 2: "OVAJ TJEDAN", 3: "PRATI"}

    lines: list[str] = []
    current_priority = None
    for action in report.actions[:limit]:
        if action.priority != current_priority:
            current_priority = action.priority
            label = headers.get(action.priority, "")
            while lines and not lines[-1]:
                lines.pop()
            if lines:
                lines.append("")
            lines += [f"━━━━━  {label}  ━━━━━", ""]

        lines.append(f"{icons.get(action.kind, '•')} {action.title}")
        lines.append(f"    {action.move}")
        lines.append(f"    {action.why}")
        if action.value_at_risk:
            lines.append(f"    ~{action.value_at_risk:.0f} € u igri")
        lines.append("")

    return "\n".join(lines).strip()


def format_calendar(report: OccupancyReport, max_days: int = 62) -> str:
    """Night by night: what is free, your price, the minimum stay."""
    lines = ["🗓 KALENDAR", "🟩 puno · 🟨 djelomično · 🟥 prazno", ""]
    month = None
    for day in report.days[:max_days]:
        if day.day.month != month:
            if month is not None:
                lines.append("")
            month = day.day.month
            lines.append(f"▸ {MONTHS_HR[day.day.month - 1]}")
        if day.free_units == 0:
            marker = "🟩"
        elif day.free_units == day.total_units:
            marker = "🟥"
        else:
            marker = "🟨"

        bits = []
        price = day.free_price or next(
            (i.price for i in day.rates.values() if i.price), None
        )
        if price:
            bits.append(f"{price:.0f} €")
        elif day.hist_adr:
            bits.append(f"~{day.hist_adr:.0f} €*")
        stays = {i.min_stay for i in day.rates.values() if i.min_stay}
        if stays:
            bits.append(f"min {min(stays)}")
        if day.hist_occupancy is not None:
            bits.append(f"prije {day.hist_occupancy * 100:.0f}%")
        if day.free_unit_names:
            bits.append(", ".join(day.free_unit_names))

        lines.append(f"{marker} {_fmt_day(day.day)} · " + " · ".join(bits))

    lines.append("")
    lines.append("* povijesna cijena, trenutna nije učitana")
    return "\n".join(lines)


def format_gaps(report: OccupancyReport, limit: int = 12) -> str:
    """Free stretches per apartment, most urgent first."""
    if not report.gaps:
        return "🎉 Nema slobodnih termina u ovom razdoblju."

    def sort_key(gap: Gap):
        return (0 if gap.is_orphan else 1, gap.nights, gap.lead_days)

    by_day = {d.day: d for d in report.days}
    lines = ["🕳 SLOBODNI TERMINI", ""]
    for gap in sorted(report.gaps, key=sort_key)[:limit]:
        nights_word = "noć" if gap.nights == 1 else "noći"
        head = f"{gap.unit} · {_fmt_range(gap.start, gap.end)} · {gap.nights} {nights_word}"
        lines.append(head)

        notes = []
        if gap.is_orphan:
            notes.append("između dvije rezervacije")
        day = by_day.get(gap.start)
        min_stay = day.current_min_stay(gap.unit) if day else None
        if min_stay and min_stay > gap.nights:
            notes.append(f"⛔ blokira min. boravak {min_stay}")
        if gap.lost_value:
            notes.append(f"~{gap.lost_value:.0f} €")
        if notes:
            lines.append("    " + " · ".join(notes))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_notes(report: OccupancyReport) -> str:
    if not report.notes:
        return ""
    return "\n".join(f"ℹ️ {note}" for note in report.notes)


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Telegram caps a message at 4096 characters - split on line boundaries."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        if length + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def format_full_report(report: OccupancyReport, include_calendar: bool = False) -> list[str]:
    """The whole rule-based report, split into Telegram-sized messages."""
    sections = [format_summary(report), format_actions(report), format_gaps(report)]
    if include_calendar:
        sections.append(format_calendar(report))
    notes = format_notes(report)
    if notes:
        sections.append(notes)
    sections.append(f"⏱ {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return split_message("\n\n".join(s for s in sections if s))
