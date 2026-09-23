"""
Occupancy & pricing analysis for the next 30 / 60 days.

The analysis answers the question a small host actually asks: "which nights
in the next month or two are not selling, and what should I change - the
price, or the minimum stay?"

It works in two layers:

1. A deterministic engine (this module) that reads reservations from Rentlio,
   builds a night-by-night calendar per apartment, compares it against the
   same period in previous seasons, and derives concrete actions.
2. An optional LLM layer (``ai_advisor``) that turns the numbers into a short
   Croatian briefing. The numbers below are the source of truth; the model
   only narrates them.

Everything here is pure Python on top of already-fetched reservations, so it
can be unit-tested and run from a script without Telegram.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Rentlio does not document a booking-creation field, and different accounts
# expose different ones. Whichever of these is present lets us measure the
# real booking pace ("how much was sold 20 days before arrival last year");
# without it we fall back to a generic pickup curve.
BOOKING_DATE_FIELDS = (
    "createdAt", "created", "createdDate", "createdOn", "dateCreated",
    "bookingDate", "reservationDate", "insertedAt", "insertDate",
)

# Fraction of the final occupancy that is typically already booked N days
# before arrival. Used only when the history carries no booking dates.
DEFAULT_PICKUP_CURVE: dict[int, float] = {
    3: 0.93,
    7: 0.88,
    14: 0.78,
    21: 0.70,
    30: 0.60,
    45: 0.48,
    60: 0.38,
}

# Statuses that mean the stay is not happening are filtered by the caller
# (see rentlio_api.is_live_reservation), so anything reaching this module
# counts as a real, occupied night.


@dataclass
class PricingConfig:
    """Tunable thresholds for the recommendation engine."""

    # +/- days around the same calendar date used to smooth historical demand.
    # With two apartments a single night is either 0%, 50% or 100% booked, so
    # a raw year-over-year comparison is pure noise.
    history_window_days: int = 3

    # How far behind the expected pace a date must be before we suggest a cut.
    behind_threshold: float = 0.18
    # How far ahead before we suggest holding / raising.
    ahead_threshold: float = 0.20

    # Discount suggestion by days-to-arrival, applied when a date is behind.
    discount_by_lead: tuple[tuple[int, int], ...] = (
        (7, 20),    # <= 7 days out: aggressive, the night is about to expire
        (14, 15),
        (30, 10),
        (10**6, 5),
    )

    # A gap this short between two bookings will not sell at the normal
    # minimum stay - the min stay has to come down to the gap length.
    orphan_gap_nights: int = 2

    # Historical occupancy above this counts as strong demand.
    strong_demand: float = 0.75
    weak_demand: float = 0.45

    # Minimum stay suggested for strong periods far enough out.
    min_stay_strong: int = 3
    min_stay_normal: int = 2

    # Inside this many days the min stay should stop blocking bookings.
    last_minute_days: int = 7


@dataclass
class Stay:
    """One reservation reduced to what the analysis needs."""

    reservation_id: str
    unit: str
    arrival: date
    departure: date
    total_price: float
    channel: str
    guest_name: str
    booked_on: Optional[date] = None

    @property
    def nights(self) -> int:
        return max((self.departure - self.arrival).days, 0)

    @property
    def price_per_night(self) -> Optional[float]:
        if self.nights <= 0 or self.total_price <= 0:
            return None
        return self.total_price / self.nights

    def occupied_nights(self) -> Iterable[date]:
        """Nights slept in - the departure day is free for the next guest."""
        day = self.arrival
        while day < self.departure:
            yield day
            day += timedelta(days=1)


@dataclass
class DayStat:
    """Everything known about one future night."""

    day: date
    total_units: int
    booked_units: int
    free_unit_names: list[str]
    lead_days: int
    booked_adr: Optional[float] = None       # what the booked nights sold for
    hist_occupancy: Optional[float] = None   # 0-1, same period in past seasons
    hist_adr: Optional[float] = None         # average past price per night
    expected_occupancy: Optional[float] = None  # hist final occ x pickup curve

    @property
    def occupancy(self) -> float:
        if self.total_units <= 0:
            return 0.0
        return self.booked_units / self.total_units

    @property
    def free_units(self) -> int:
        return self.total_units - self.booked_units

    @property
    def pace_delta(self) -> Optional[float]:
        """Actual minus expected occupancy. Negative = selling too slowly."""
        if self.expected_occupancy is None:
            return None
        return self.occupancy - self.expected_occupancy

    @property
    def is_weekend(self) -> bool:
        return self.day.weekday() >= 4  # Friday & Saturday nights


@dataclass
class Gap:
    """A run of consecutive free nights in one apartment."""

    unit: str
    start: date
    end: date              # inclusive, last free night
    closed_before: bool    # a guest checks out on `start`
    closed_after: bool     # a guest checks in the morning after `end`
    lead_days: int
    hist_occupancy: Optional[float] = None
    hist_adr: Optional[float] = None
    truncated: bool = False  # the gap runs past the end of the horizon

    @property
    def nights(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_orphan(self) -> bool:
        return self.closed_before and self.closed_after

    @property
    def lost_value(self) -> float:
        if not self.hist_adr:
            return 0.0
        return self.hist_adr * self.nights


@dataclass
class Action:
    """One concrete thing to change in Rentlio."""

    kind: str              # discount | min_stay | hold | raise | info
    title: str
    detail: str
    start: date
    end: date
    priority: int = 2      # 1 = do it today, 3 = keep an eye on it
    unit: Optional[str] = None
    discount_pct: Optional[int] = None
    min_stay: Optional[int] = None
    value_at_risk: float = 0.0
    metrics: dict = field(default_factory=dict)


@dataclass
class OccupancyReport:
    generated_at: datetime
    start: date
    end: date
    horizon_days: int
    units: list[str]
    days: list[DayStat]
    gaps: list[Gap]
    actions: list[Action]
    history_years: list[int]
    pace_source: str          # "history" | "heuristic" | "none"
    weekday_occupancy: dict[int, float]
    notes: list[str]

    # ---------- aggregates ----------

    @property
    def total_unit_nights(self) -> int:
        return sum(d.total_units for d in self.days)

    @property
    def booked_unit_nights(self) -> int:
        return sum(d.booked_units for d in self.days)

    @property
    def occupancy(self) -> float:
        if not self.total_unit_nights:
            return 0.0
        return self.booked_unit_nights / self.total_unit_nights

    @property
    def hist_occupancy(self) -> Optional[float]:
        values = [d.hist_occupancy for d in self.days if d.hist_occupancy is not None]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def booked_revenue(self) -> float:
        return sum((d.booked_adr or 0) * d.booked_units for d in self.days)

    @property
    def free_nights_value(self) -> float:
        return sum((d.hist_adr or 0) * d.free_units for d in self.days)

    def weakest_periods(self, limit: int = 5) -> list[tuple[date, date, float]]:
        """Contiguous ranges with free capacity, worst pace first."""
        runs: list[tuple[date, date, float]] = []
        current: list[DayStat] = []
        for day in self.days:
            behind = day.pace_delta is not None and day.pace_delta < 0
            if day.free_units > 0 and behind:
                current.append(day)
            elif current:
                runs.append(_summarize_run(current))
                current = []
        if current:
            runs.append(_summarize_run(current))
        runs.sort(key=lambda r: r[2])
        return runs[:limit]

    def to_payload(self) -> dict:
        """Compact, LLM-friendly view of the analysis."""
        return {
            "razdoblje": {
                "od": self.start.isoformat(),
                "do": self.end.isoformat(),
                "dana": self.horizon_days,
            },
            "apartmani": self.units,
            "sazetak": {
                "popunjenost_sada_pct": round(self.occupancy * 100, 1),
                "popunjenost_povijesno_pct": (
                    round(self.hist_occupancy * 100, 1)
                    if self.hist_occupancy is not None else None
                ),
                "slobodnih_nocenja": self.total_unit_nights - self.booked_unit_nights,
                "ukupno_nocenja": self.total_unit_nights,
                "prihod_rezerviran_eur": round(self.booked_revenue),
                "vrijednost_slobodnih_nocenja_eur": round(self.free_nights_value),
                "izvor_tempa": self.pace_source,
                "godine_povijesti": self.history_years,
            },
            "dani": [
                {
                    "datum": d.day.isoformat(),
                    "dan": ["pon", "uto", "sri", "cet", "pet", "sub", "ned"][d.day.weekday()],
                    "zauzeto": d.booked_units,
                    "ukupno": d.total_units,
                    "slobodni": d.free_unit_names,
                    "cijena_rezervirano_eur": round(d.booked_adr) if d.booked_adr else None,
                    "povijesna_popunjenost_pct": (
                        round(d.hist_occupancy * 100) if d.hist_occupancy is not None else None
                    ),
                    "povijesna_cijena_eur": round(d.hist_adr) if d.hist_adr else None,
                    "ocekivano_pct": (
                        round(d.expected_occupancy * 100) if d.expected_occupancy is not None else None
                    ),
                    "odstupanje_pct": (
                        round(d.pace_delta * 100) if d.pace_delta is not None else None
                    ),
                }
                for d in self.days
            ],
            "rupe": [
                {
                    "apartman": g.unit,
                    "od": g.start.isoformat(),
                    "do": g.end.isoformat(),
                    "noci": g.nights,
                    "izmedu_rezervacija": g.is_orphan,
                    "zatvoreno_prije": g.closed_before,
                    "zatvoreno_poslije": g.closed_after,
                    "dana_do_pocetka": g.lead_days,
                    "povijesna_popunjenost_pct": (
                        round(g.hist_occupancy * 100) if g.hist_occupancy is not None else None
                    ),
                    "vrijednost_eur": round(g.lost_value),
                }
                for g in self.gaps
            ],
            "preporuke": [
                {
                    "vrsta": a.kind,
                    "prioritet": a.priority,
                    "naslov": a.title,
                    "obrazlozenje": a.detail,
                    "od": a.start.isoformat(),
                    "do": a.end.isoformat(),
                    "apartman": a.unit,
                    "popust_pct": a.discount_pct,
                    "min_nocenja": a.min_stay,
                    "vrijednost_eur": round(a.value_at_risk),
                }
                for a in self.actions
            ],
            "popunjenost_po_danu_tjedna_pct": {
                ["pon", "uto", "sri", "cet", "pet", "sub", "ned"][k]: round(v * 100)
                for k, v in sorted(self.weekday_occupancy.items())
            },
            "napomene": self.notes,
        }


def _summarize_run(run: list[DayStat]) -> tuple[date, date, float]:
    deltas = [d.pace_delta for d in run if d.pace_delta is not None]
    avg = sum(deltas) / len(deltas) if deltas else 0.0
    return run[0].day, run[-1].day, avg


# ========== parsing ==========

def to_date(value) -> Optional[date]:
    """Parse Rentlio's timestamps (unix seconds/ms) or ISO strings."""
    if value in (None, "", 0):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:      # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return to_date(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text[:len(fmt) + 4], fmt).date()
            except ValueError:
                continue
    return None


def parse_stays(reservations: Iterable[dict]) -> list[Stay]:
    """Turn raw reservations into Stay objects, skipping unusable ones."""
    stays: list[Stay] = []
    for res in reservations:
        arrival = to_date(res.get("arrivalDate"))
        departure = to_date(res.get("departureDate"))
        if not arrival or not departure or departure <= arrival:
            continue

        booked_on = None
        for candidate in BOOKING_DATE_FIELDS:
            if candidate in res:
                booked_on = to_date(res.get(candidate))
                if booked_on:
                    break

        stays.append(Stay(
            reservation_id=str(res.get("id", "")),
            unit=(res.get("unitName") or "Nepoznato").strip(),
            arrival=arrival,
            departure=departure,
            total_price=float(res.get("totalPrice") or 0),
            channel=res.get("otaChannelName") or "Direct",
            guest_name=res.get("guestName") or "",
            booked_on=booked_on,
        ))
    return stays


def shift_years(day: date, years: int) -> date:
    """Same calendar date N years back, tolerating 29 February."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


# ========== the engine ==========

class OccupancyAnalyzer:
    """Builds an OccupancyReport out of current and historical stays."""

    def __init__(self, config: PricingConfig = None):
        self.config = config or PricingConfig()

    def analyze(
        self,
        today: date,
        horizon_days: int,
        current_stays: list[Stay],
        history: dict[int, list[Stay]],
        units: list[str],
    ) -> OccupancyReport:
        cfg = self.config
        start = today
        end = today + timedelta(days=horizon_days - 1)

        units = sorted({u for u in units if u}) or sorted({s.unit for s in current_stays})
        total_units = len(units) or 1
        notes: list[str] = []

        # --- night-by-night calendar of the horizon ---
        booked: dict[date, dict[str, Stay]] = defaultdict(dict)
        for stay in current_stays:
            for night in stay.occupied_nights():
                if start <= night <= end:
                    booked[night][stay.unit] = stay

        # --- historical demand index, per calendar date ---
        hist_index = self._build_history_index(history, total_units)
        history_years = sorted(history.keys(), reverse=True)
        if not history_years:
            notes.append("Nema povijesnih podataka - analiza se oslanja samo na trenutni kalendar.")

        pickup_curve, pace_source = self._build_pickup_curve(history)
        if pace_source == "heuristic" and history_years:
            notes.append(
                "Rentlio ne vraca datum kreiranja rezervacije, pa je ocekivani tempo "
                "procijenjen iz tipicne krivulje bookinga, a ne iz tvojih podataka."
            )
        elif pace_source == "none":
            notes.append("Bez povijesti nema usporedbe tempa - gledaju se samo rupe u kalendaru.")

        days: list[DayStat] = []
        for offset in range(horizon_days):
            day = start + timedelta(days=offset)
            occupied_units = booked.get(day, {})
            free_names = [u for u in units if u not in occupied_units]

            prices = [s.price_per_night for s in occupied_units.values() if s.price_per_night]
            hist = self._history_for(hist_index, day, history_years, cfg.history_window_days)

            expected = None
            if hist["occupancy"] is not None:
                expected = min(1.0, hist["occupancy"] * _pickup(pickup_curve, offset))

            days.append(DayStat(
                day=day,
                total_units=total_units,
                booked_units=len(occupied_units),
                free_unit_names=free_names,
                lead_days=offset,
                booked_adr=(sum(prices) / len(prices)) if prices else None,
                hist_occupancy=hist["occupancy"],
                hist_adr=hist["adr"],
                expected_occupancy=expected,
            ))

        gaps = self._find_gaps(days, units, current_stays, start, end)
        weekday_occ = self._weekday_occupancy(hist_index)
        actions = self._build_actions(days, gaps, weekday_occ)

        return OccupancyReport(
            generated_at=datetime.now(),
            start=start,
            end=end,
            horizon_days=horizon_days,
            units=units,
            days=days,
            gaps=gaps,
            actions=actions,
            history_years=history_years,
            pace_source=pace_source,
            weekday_occupancy=weekday_occ,
            notes=notes,
        )

    # ---------- history ----------

    def _build_history_index(
        self,
        history: dict[int, list[Stay]],
        total_units: int,
    ) -> dict[int, dict]:
        """Per past season: for each night, how many units were sold and at what price."""
        index: dict[int, dict] = {}
        for years_back, stays in history.items():
            per_day: dict[date, dict] = defaultdict(lambda: {"units": 0, "prices": []})
            for stay in stays:
                ppn = stay.price_per_night
                for night in stay.occupied_nights():
                    entry = per_day[night]
                    entry["units"] += 1
                    if ppn:
                        entry["prices"].append(ppn)
            # How many apartments that season actually had. Taking today's unit
            # count would understate occupancy for a season rented with fewer
            # apartments, and a season's busiest night is the honest floor.
            busiest = max((e["units"] for e in per_day.values()), default=0)
            index[years_back] = {
                "per_day": per_day,
                "units": max(busiest, 1) if busiest else max(total_units, 1),
            }
        return index

    def _history_for(
        self,
        index: dict[int, dict],
        day: date,
        years: list[int],
        window: int,
    ) -> dict:
        """Blend the same calendar window across past seasons into one demand signal."""
        occ_values: list[tuple[float, float]] = []   # (value, weight)
        adr_values: list[tuple[float, float]] = []

        for years_back in years:
            season = index.get(years_back)
            if not season or not season["per_day"]:
                continue
            per_day = season["per_day"]
            season_units = season["units"]
            reference = shift_years(day, -years_back)
            sold = 0
            nights_in_window = 0
            prices: list[float] = []
            for delta in range(-window, window + 1):
                night = reference + timedelta(days=delta)
                entry = per_day.get(night)
                nights_in_window += 1
                if entry:
                    sold += entry["units"]
                    prices.extend(entry["prices"])
            if nights_in_window == 0:
                continue
            # Recent seasons say more about this one than older ones.
            weight = 1.0 / years_back
            occupancy = sold / max(nights_in_window * season_units, 1)
            occ_values.append((min(occupancy, 1.0), weight))
            if prices:
                adr_values.append((sum(prices) / len(prices), weight))

        def blend(pairs: list[tuple[float, float]]) -> Optional[float]:
            if not pairs:
                return None
            total_weight = sum(w for _, w in pairs)
            if total_weight <= 0:
                return None
            return sum(v * w for v, w in pairs) / total_weight

        return {"occupancy": blend(occ_values), "adr": blend(adr_values)}

    def _build_pickup_curve(
        self,
        history: dict[int, list[Stay]],
    ) -> tuple[dict[int, float], str]:
        """
        How much of a night's final occupancy was already booked N days out.

        Measured from last season's booking dates when the API exposes them;
        otherwise a generic curve, flagged as such in the report.
        """
        datable = [
            stay
            for stays in history.values()
            for stay in stays
            if stay.booked_on is not None
        ]
        all_stays = [stay for stays in history.values() for stay in stays]
        if not all_stays:
            return dict(DEFAULT_PICKUP_CURVE), "none"
        if len(datable) < max(10, 0.4 * len(all_stays)):
            return dict(DEFAULT_PICKUP_CURVE), "heuristic"

        sold_by_night: dict[date, list[Stay]] = defaultdict(list)
        for stay in datable:
            for night in stay.occupied_nights():
                sold_by_night[night].append(stay)

        curve: dict[int, float] = {}
        for lead in sorted(DEFAULT_PICKUP_CURVE):
            final_total = 0
            asof_total = 0
            for night, stays in sold_by_night.items():
                cutoff = night - timedelta(days=lead)
                final_total += len(stays)
                asof_total += sum(1 for s in stays if s.booked_on and s.booked_on <= cutoff)
            if final_total:
                curve[lead] = round(asof_total / final_total, 3)
        if not curve:
            return dict(DEFAULT_PICKUP_CURVE), "heuristic"

        # Pickup can only grow as arrival approaches; smooth out sampling noise.
        previous = 0.0
        for lead in sorted(curve, reverse=True):
            previous = max(previous, curve[lead])
            curve[lead] = previous
        return curve, "history"

    def _weekday_occupancy(self, index: dict[int, dict]) -> dict[int, float]:
        """
        Historical occupancy per weekday - tells weekends from midweek.

        Empty nights have to count too, so the denominator walks every night
        between the first and last booked night of each season rather than
        only the nights that happen to appear in a reservation.
        """
        sold: dict[int, int] = defaultdict(int)
        slots: dict[int, int] = defaultdict(int)

        for season in index.values():
            per_day = season["per_day"]
            if not per_day:
                continue
            season_units = season["units"]
            for night in _daterange(min(per_day), max(per_day)):
                entry = per_day.get(night)
                slots[night.weekday()] += season_units
                if entry:
                    sold[night.weekday()] += entry["units"]

        return {
            weekday: min(sold.get(weekday, 0) / count, 1.0)
            for weekday, count in slots.items()
            if count
        }

    # ---------- gaps ----------

    def _find_gaps(
        self,
        days: list[DayStat],
        units: list[str],
        stays: list[Stay],
        start: date,
        end: date,
    ) -> list[Gap]:
        """Runs of free nights per apartment, with their neighbours noted."""
        occupied: dict[str, set] = defaultdict(set)
        for stay in stays:
            for night in stay.occupied_nights():
                occupied[stay.unit].add(night)

        by_day = {d.day: d for d in days}
        gaps: list[Gap] = []

        for unit in units:
            run_start: Optional[date] = None
            previous: Optional[date] = None
            for day_stat in days:
                day = day_stat.day
                if day not in occupied[unit]:
                    if run_start is None:
                        run_start = day
                    previous = day
                    continue
                if run_start is not None and previous is not None:
                    gaps.append(self._make_gap(unit, run_start, previous, occupied, by_day, start, end))
                    run_start = None
            if run_start is not None and previous is not None:
                gaps.append(self._make_gap(unit, run_start, previous, occupied, by_day, start, end))

        gaps.sort(key=lambda g: (g.start, g.unit))
        return gaps

    def _make_gap(
        self,
        unit: str,
        run_start: date,
        run_end: date,
        occupied: dict[str, set],
        by_day: dict[date, DayStat],
        start: date,
        end: date,
    ) -> Gap:
        day_stat = by_day.get(run_start)
        hist_occ_values = [
            by_day[d].hist_occupancy
            for d in _daterange(run_start, run_end)
            if d in by_day and by_day[d].hist_occupancy is not None
        ]
        hist_adr_values = [
            by_day[d].hist_adr
            for d in _daterange(run_start, run_end)
            if d in by_day and by_day[d].hist_adr is not None
        ]
        return Gap(
            unit=unit,
            start=run_start,
            end=run_end,
            closed_before=(run_start - timedelta(days=1)) in occupied[unit],
            closed_after=(run_end + timedelta(days=1)) in occupied[unit],
            lead_days=day_stat.lead_days if day_stat else (run_start - start).days,
            hist_occupancy=(sum(hist_occ_values) / len(hist_occ_values)) if hist_occ_values else None,
            hist_adr=(sum(hist_adr_values) / len(hist_adr_values)) if hist_adr_values else None,
            truncated=(run_end >= end),
        )

    # ---------- recommendations ----------

    def _build_actions(
        self,
        days: list[DayStat],
        gaps: list[Gap],
        weekday_occ: dict[int, float],
    ) -> list[Action]:
        cfg = self.config
        actions: list[Action] = []

        # 1. Gaps that physically cannot be booked at a 2-night minimum.
        for gap in gaps:
            if gap.truncated and not gap.closed_before:
                continue
            if gap.nights <= cfg.orphan_gap_nights and gap.is_orphan:
                discount = _discount_for_lead(gap.lead_days, cfg)
                actions.append(Action(
                    kind="min_stay",
                    title=(
                        f"{gap.unit}: rupa od {gap.nights} "
                        f"{'noc' if gap.nights == 1 else 'noci'} izmedu dvije rezervacije"
                    ),
                    detail=(
                        f"{_fmt_range(gap.start, gap.end)} je zatvoreno s obje strane. "
                        f"Uz minimalni boravak veci od {gap.nights} nitko to ne moze rezervirati - "
                        f"spusti min. boravak na {gap.nights} i daj {discount}% popusta, "
                        f"ili ponudi produljenje gostu prije/poslije."
                    ),
                    start=gap.start,
                    end=gap.end,
                    priority=1 if gap.lead_days <= 21 else 2,
                    unit=gap.unit,
                    discount_pct=discount,
                    min_stay=gap.nights,
                    value_at_risk=gap.lost_value,
                    metrics={"noci": gap.nights, "dana_do": gap.lead_days},
                ))
            elif gap.is_orphan and gap.nights <= 4 and gap.lead_days <= 30:
                actions.append(Action(
                    kind="min_stay",
                    title=f"{gap.unit}: kratka rupa {gap.nights} noci",
                    detail=(
                        f"{_fmt_range(gap.start, gap.end)} - postavi min. boravak tocno na "
                        f"{gap.nights} noci da termin uopce bude vidljiv u trazilicama."
                    ),
                    start=gap.start,
                    end=gap.end,
                    priority=2,
                    unit=gap.unit,
                    min_stay=gap.nights,
                    value_at_risk=gap.lost_value,
                    metrics={"noci": gap.nights, "dana_do": gap.lead_days},
                ))

        # 2. Stretches that are selling slower than the season normally does.
        # A two-week stretch does not get one blanket discount: nights three
        # days out and nights three weeks out need different cuts, so each run
        # is split where the lead-time tier changes.
        behind_runs = _contiguous(days, lambda d: (
            d.free_units > 0
            and d.pace_delta is not None
            and d.pace_delta <= -cfg.behind_threshold
        ))
        for run in [
            segment
            for whole in behind_runs
            for segment in _split_by(whole, lambda d: _discount_for_lead(d.lead_days, cfg))
        ]:
            first, last = run[0], run[-1]
            lead = first.lead_days
            discount = _discount_for_lead(lead, cfg)
            avg_delta = sum(d.pace_delta for d in run) / len(run)
            hist = [d.hist_occupancy for d in run if d.hist_occupancy is not None]
            hist_avg = sum(hist) / len(hist) if hist else None
            value = sum((d.hist_adr or 0) * d.free_units for d in run)
            ref_price = [d.hist_adr for d in run if d.hist_adr]
            price_hint = ""
            if ref_price:
                avg_price = sum(ref_price) / len(ref_price)
                price_hint = (
                    f" Prosjecna cijena tih datuma prijasnjih godina: {avg_price:.0f} EUR, "
                    f"s popustom ~{avg_price * (100 - discount) / 100:.0f} EUR."
                )
            free_nights = sum(d.free_units for d in run)
            avg_occupancy = sum(d.occupancy for d in run) / len(run)
            actions.append(Action(
                kind="discount",
                title=f"Spusti cijenu ~{discount}%: {_fmt_range(first.day, last.day)}",
                detail=(
                    f"Prosjecna popunjenost {avg_occupancy * 100:.0f}% "
                    f"({free_nights} slobodnih nocenja), a povijesno je u ovom terminu "
                    f"bilo ~{(hist_avg or 0) * 100:.0f}%. Zaostajanje ~"
                    f"{abs(avg_delta) * 100:.0f} postotnih bodova, "
                    f"do prve noci {lead} dana.{price_hint}"
                ),
                start=first.day,
                end=last.day,
                priority=1 if lead <= 14 else 2,
                discount_pct=discount,
                value_at_risk=value,
                metrics={
                    "zaostajanje_pct": round(abs(avg_delta) * 100),
                    "dana_do": lead,
                    "slobodnih_nocenja": sum(d.free_units for d in run),
                },
            ))

        # 3. Stretches doing better than usual - protect the rate, lengthen stays.
        for run in _contiguous(days, lambda d: (
            d.free_units > 0
            and d.pace_delta is not None
            and d.pace_delta >= cfg.ahead_threshold
            and (d.hist_occupancy or 0) >= cfg.strong_demand
        )):
            first, last = run[0], run[-1]
            if first.lead_days <= cfg.last_minute_days:
                continue
            actions.append(Action(
                kind="raise",
                title=f"Drzi ili podigni cijenu: {_fmt_range(first.day, last.day)}",
                detail=(
                    f"Ide brze nego prijasnjih godina i termin je povijesno jak "
                    f"(~{(first.hist_occupancy or 0) * 100:.0f}% popunjenosti). "
                    f"Nema razloga za popust; min. boravak {cfg.min_stay_strong} noci "
                    f"cuva te od rupa oko vikenda."
                ),
                start=first.day,
                end=last.day,
                priority=3,
                min_stay=cfg.min_stay_strong,
                metrics={"dana_do": first.lead_days},
            ))

        # 4. Last-minute nights still open - the min stay itself is the blocker.
        soon = [
            d for d in days
            if d.lead_days <= cfg.last_minute_days and d.free_units > 0
        ]
        if soon:
            value = sum((d.hist_adr or 0) * d.free_units for d in soon)
            actions.append(Action(
                kind="min_stay",
                title=f"Zadnji cas: {len(soon)} slobodnih noci u iducih {cfg.last_minute_days} dana",
                detail=(
                    "Za termine unutar tjedna spusti minimalni boravak na 1 noc i ukljuci "
                    "last-minute popust - te noci inace propadaju bez prihoda."
                ),
                start=soon[0].day,
                end=soon[-1].day,
                priority=1,
                min_stay=1,
                discount_pct=cfg.discount_by_lead[0][1],
                value_at_risk=value,
                metrics={"slobodnih_nocenja": sum(d.free_units for d in soon)},
            ))

        # 5. Weekend policy, straight from the history.
        weekend_occ = [weekday_occ.get(w) for w in (4, 5) if weekday_occ.get(w) is not None]
        midweek_occ = [weekday_occ.get(w) for w in (0, 1, 2) if weekday_occ.get(w) is not None]
        if weekend_occ and midweek_occ:
            weekend_avg = sum(weekend_occ) / len(weekend_occ)
            midweek_avg = sum(midweek_occ) / len(midweek_occ)
            if weekend_avg - midweek_avg >= 0.15 and days:
                actions.append(Action(
                    kind="min_stay",
                    title="Vikendi se prodaju bolje od sredine tjedna",
                    detail=(
                        f"Povijesno: petak/subota ~{weekend_avg * 100:.0f}%, "
                        f"pon-sri ~{midweek_avg * 100:.0f}%. Postavi min. "
                        f"{cfg.min_stay_normal}-{cfg.min_stay_strong} noci s dolaskom u petak "
                        "da vikend ne ostavlja neiskoristive rupe usred tjedna."
                    ),
                    start=days[0].day,
                    end=days[-1].day,
                    priority=3,
                    min_stay=cfg.min_stay_strong,
                    metrics={
                        "vikend_pct": round(weekend_avg * 100),
                        "sredina_tjedna_pct": round(midweek_avg * 100),
                    },
                ))

        actions.sort(key=lambda a: (a.priority, -a.value_at_risk, a.start))
        return actions


# ========== small helpers ==========

def _daterange(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _contiguous(days: list[DayStat], predicate) -> list[list[DayStat]]:
    runs: list[list[DayStat]] = []
    current: list[DayStat] = []
    for day in days:
        if predicate(day):
            current.append(day)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _split_by(days: list[DayStat], key) -> list[list[DayStat]]:
    """Cut a run wherever `key` changes, keeping the day order."""
    segments: list[list[DayStat]] = []
    current: list[DayStat] = []
    current_key = None
    for day in days:
        day_key = key(day)
        if current and day_key != current_key:
            segments.append(current)
            current = []
        current.append(day)
        current_key = day_key
    if current:
        segments.append(current)
    return segments


def _pickup(curve: dict[int, float], lead_days: int) -> float:
    """Expected share of final bookings already made `lead_days` before arrival."""
    if not curve:
        return 1.0
    for lead in sorted(curve):
        if lead_days <= lead:
            return curve[lead]
    return curve[max(curve)]


def _discount_for_lead(lead_days: int, cfg: PricingConfig) -> int:
    for threshold, discount in cfg.discount_by_lead:
        if lead_days <= threshold:
            return discount
    return cfg.discount_by_lead[-1][1]


def _fmt_range(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d.%m.")
    return f"{start.strftime('%d.%m.')} - {end.strftime('%d.%m.')}"
