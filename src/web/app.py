"""Self check-in form, served from the bot process.

Two ways in:

  /checkin/<token>   a link tied to one reservation, sent to the guest
  /checkin           no token - the guest types surname + arrival date, so the
                     link is static and can go in a Rentlio message, the
                     WhatsApp Business greeting, or a QR code on the door

Both land on the same page: photograph the document, check what was read,
confirm. Nothing is written to Rentlio from here - a confirmed submission
waits in SQLite until the owner approves it in Telegram.

The image lives in memory for the length of the OCR call and is never written
to disk.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiohttp import web

from src.config import config
from src.services.checkin import name_key
from src.services.rentlio_api import is_live_reservation

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent

# A reservation lookup without a token is a guessable endpoint, so it gets a
# per-IP budget. Generous enough for a guest fumbling the date, useless for
# walking the reservation list.
LOOKUP_MAX_ATTEMPTS = 8
LOOKUP_WINDOW_SECONDS = 600

# Enough for a family; past this something is wrong with the request.
MAX_GUESTS_PER_SUBMISSION = 12

NotifyFn = Callable[[int], Awaitable[None]]


def _template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _client_ip(request: web.Request) -> str:
    """Real client address from behind the Cloudflare tunnel."""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def _mask_name(name: str) -> str:
    """"Ivan Horvat" -> "Ivan H." - enough to confirm the right page.

    A leaked link should not hand out a full name.
    """
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _format_date(timestamp) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%d.%m.%Y")
    except (TypeError, ValueError, OSError):
        return ""


def _surname_matches(typed: str, reservation_name: str) -> bool:
    """Does the typed surname appear among the reservation's name words?

    Compared without diacritics and case, because a guest typing "Masanovic"
    on a phone keyboard means the same person as "Mašanović" in Rentlio.
    """
    typed_words = set(name_key(typed).split())
    if not typed_words:
        return False
    return typed_words.issubset(set(name_key(reservation_name).split()))


class CheckinWeb:
    """Wires the form to the API, the store and the owner's Telegram."""

    def __init__(self, api, store, ocr_service, notify: Optional[NotifyFn] = None):
        self.api = api
        self.store = store
        self.ocr = ocr_service
        self.notify = notify
        self._lookup_hits: dict[str, list[float]] = {}

    # ---------- helpers ----------

    def _rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._lookup_hits.get(ip, []) if now - t < LOOKUP_WINDOW_SECONDS]
        # Drop idle buckets so a long-running process does not accumulate IPs.
        if len(self._lookup_hits) > 512:
            self._lookup_hits = {
                k: v for k, v in self._lookup_hits.items()
                if any(now - t < LOOKUP_WINDOW_SECONDS for t in v)
            }
        if len(hits) >= LOOKUP_MAX_ATTEMPTS:
            self._lookup_hits[ip] = hits
            return True
        hits.append(now)
        self._lookup_hits[ip] = hits
        return False

    async def _reservation_summary(self, reservation_id: str) -> dict:
        """What the form header shows. Never fatal - the form works without it."""
        try:
            details = await self.api.get_reservation_details(reservation_id)
        except Exception as e:
            logger.warning(f"Could not load reservation {reservation_id}: {e}")
            return {}
        holder = details.get("holder") or {}
        return {
            "unitName": details.get("unitName") or "",
            "arrival": _format_date(details.get("arrivalDate")),
            "departure": _format_date(details.get("departureDate")),
            "guestName": _mask_name(holder.get("name") or details.get("guestName") or ""),
        }

    # ---------- routes ----------

    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def lookup_page(self, request: web.Request) -> web.Response:
        html = (
            _template("lookup.html")
            .replace("__ERROR__", "")
            .replace("__SURNAME__", "")
            .replace("__ARRIVAL__", "")
        )
        return web.Response(text=html, content_type="text/html")

    async def lookup_submit(self, request: web.Request) -> web.Response:
        form = await request.post()
        surname = str(form.get("surname", "")).strip()
        arrival = str(form.get("arrival", "")).strip()

        def fail(message: str, status: int = 200) -> web.Response:
            html = (
                _template("lookup.html")
                .replace("__ERROR__", f'<div class="msg">{_escape(message)}</div>')
                .replace("__SURNAME__", _escape(surname))
                .replace("__ARRIVAL__", _escape(arrival))
            )
            return web.Response(text=html, content_type="text/html", status=status)

        if self._rate_limited(_client_ip(request)):
            return fail("Previše pokušaja. Pokušajte za 10 minuta ili nam pošaljite poruku.", 429)

        if not surname or not arrival:
            return fail("Unesite prezime i datum dolaska.")

        try:
            datetime.strptime(arrival, "%Y-%m-%d")
        except ValueError:
            return fail("Datum dolaska nije ispravan.")

        try:
            reservations = await self.api.get_reservations(
                date_from=arrival, date_to=arrival, limit=100
            )
        except Exception as e:
            logger.error(f"Lookup failed: {e}")
            return fail("Trenutno ne mogu provjeriti rezervacije. Pokušajte kasnije.")

        arrival_ts_day = arrival
        matches = []
        for res in reservations:
            if not is_live_reservation(res):
                continue
            # The API returns anything overlapping the range, so confirm this
            # reservation actually starts on the typed day.
            if _format_iso(res.get("arrivalDate")) != arrival_ts_day:
                continue
            if _surname_matches(surname, res.get("guestName", "")):
                matches.append(res)

        if not matches:
            return fail("Nisam našao rezervaciju s tim podacima. Provjerite prezime i datum.")
        if len(matches) > 1:
            logger.warning(
                f"Lookup matched {len(matches)} reservations for {surname!r} on {arrival}"
            )
            return fail("Pronašao sam više rezervacija. Pošaljite nam poruku na WhatsApp.")

        reservation_id = str(matches[0].get("id"))
        token = await self.store.create_token(
            reservation_id, config.CHECKIN_TOKEN_TTL_DAYS
        )
        logger.info(f"Lookup issued token for reservation {reservation_id}")
        raise web.HTTPFound(f"/checkin/{token}")

    async def form_page(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        row = await self.store.valid_token(token)
        if not row:
            return web.Response(
                text=_template("lookup.html")
                .replace(
                    "__ERROR__",
                    '<div class="msg">Link je istekao ili nije važeći. '
                    "Pronađite rezervaciju ispod ili nam pošaljite poruku.</div>",
                )
                .replace("__SURNAME__", "")
                .replace("__ARRIVAL__", ""),
                content_type="text/html",
                status=404,
            )

        await self.store.mark_opened(token)
        summary = await self._reservation_summary(row["reservation_id"])
        html = (
            _template("form.html")
            .replace("__TOKEN__", token)
            .replace("__RESERVATION_JSON__", json.dumps(summary, ensure_ascii=False))
        )
        return web.Response(text=html, content_type="text/html")

    async def scan(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        row = await self.store.valid_token(token)
        if not row:
            return web.json_response({"ok": False, "error": "Link nije važeći."}, status=404)

        if request.content_length and request.content_length > config.MAX_UPLOAD_BYTES:
            return web.json_response(
                {"ok": False, "error": "Slika je prevelika. Pokušajte ponovno."}, status=413
            )

        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response({"ok": False, "error": "Neispravan zahtjev."}, status=400)

        image_bytes = b""
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name != "image":
                continue
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                image_bytes += chunk
                if len(image_bytes) > config.MAX_UPLOAD_BYTES:
                    return web.json_response(
                        {"ok": False, "error": "Slika je prevelika."}, status=413
                    )
            break

        if not image_bytes:
            return web.json_response({"ok": False, "error": "Slika nije primljena."}, status=400)

        logger.info(
            f"OCR request for reservation {row['reservation_id']} "
            f"({len(image_bytes)} bytes)"
        )
        try:
            extracted = await self.ocr.extract_from_bytes(image_bytes)
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return web.json_response(
                {"ok": False, "error": "Čitanje nije uspjelo. Pokušajte ponovno."}, status=502
            )
        finally:
            # Nothing else holds a reference; drop ours as soon as OCR is done.
            image_bytes = b""

        if not extracted.is_valid():
            return web.json_response({
                "ok": False,
                "error": "Nisam prepoznao podatke na slici. Slikajte stranu sa strojno "
                         "čitljivim redovima, ili upišite podatke ručno.",
            })

        return web.json_response({"ok": True, "guest": extracted.to_dict()})

    async def submit(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        row = await self.store.valid_token(token)
        if not row:
            return web.json_response({"ok": False, "error": "Link nije važeći."}, status=404)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Neispravan zahtjev."}, status=400)

        raw_guests = payload.get("guests")
        if not isinstance(raw_guests, list) or not raw_guests:
            return web.json_response(
                {"ok": False, "error": "Nema podataka za poslati."}, status=400
            )
        if len(raw_guests) > MAX_GUESTS_PER_SUBMISSION:
            return web.json_response(
                {"ok": False, "error": "Previše osoba u jednoj prijavi."}, status=400
            )

        cleaned = []
        for index, raw in enumerate(raw_guests, start=1):
            if not isinstance(raw, dict):
                return web.json_response(
                    {"ok": False, "error": "Neispravan zahtjev."}, status=400
                )
            problem = _validate_guest(raw, index)
            if problem:
                return web.json_response({"ok": False, "error": problem}, status=400)
            cleaned.append(_normalise_guest(raw))

        submission_id = await self.store.create_submission(
            reservation_id=row["reservation_id"],
            guests=cleaned,
            token=token,
            source="form",
        )
        await self.store.mark_submitted(token)
        logger.info(
            f"Submission {submission_id} stored for reservation "
            f"{row['reservation_id']} with {len(cleaned)} guest(s)"
        )

        if self.notify:
            try:
                await self.notify(submission_id)
            except Exception as e:
                # The guest did their part; a Telegram hiccup must not look
                # like a failed submission. It is in the DB either way.
                logger.error(f"Could not notify owner about {submission_id}: {e}")

        return web.json_response({"ok": True, "submissionId": submission_id})


REQUIRED_FORM_FIELDS = (
    ("fullName", "ime i prezime"),
    ("dateOfBirth", "datum rođenja"),
    ("documentNumber", "broj dokumenta"),
)


def _validate_guest(raw: dict, index: int) -> Optional[str]:
    """Reject a guest the owner would have to finish by hand anyway."""
    for key, label in REQUIRED_FORM_FIELDS:
        if not str(raw.get(key, "")).strip():
            return f"Za {index}. osobu nedostaje: {label}."

    dob = str(raw.get("dateOfBirth", "")).strip()
    if not _parse_any_date(dob):
        return f"Datum rođenja za {index}. osobu nije ispravan (DD.MM.GGGG)."

    gender = str(raw.get("gender", "")).strip().upper()
    if gender and gender not in ("M", "F"):
        return f"Spol za {index}. osobu nije ispravan."

    doc_type = str(raw.get("documentType", "")).strip()
    if doc_type and doc_type not in ("ID_CARD", "PASSPORT"):
        return f"Vrsta dokumenta za {index}. osobu nije ispravna."

    return None


def _normalise_guest(raw: dict) -> dict:
    """Keep only the fields we understand, trimmed, with a normalised date."""
    out = {}
    for key in (
        "fullName", "firstName", "lastName", "dateOfBirth", "documentNumber",
        "documentType", "nationality", "gender", "placeOfResidence", "address",
        "expiryDate", "oib",
    ):
        value = raw.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            out[key] = str(value)[:120]

    parsed = _parse_any_date(out.get("dateOfBirth", ""))
    if parsed:
        out["dateOfBirth"] = parsed.strftime("%d.%m.%Y")
    if out.get("gender"):
        out["gender"] = out["gender"].upper()
    # A corrected full name wins; stale halves would overwrite it downstream.
    if out.get("fullName"):
        out.pop("firstName", None)
        out.pop("lastName", None)
    return out


def _parse_any_date(value: str) -> Optional[datetime]:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y."):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _format_iso(timestamp) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def checkin_url(token: str) -> str:
    """Public link for a token, or an empty string if no base URL is set."""
    if not config.PUBLIC_BASE_URL:
        return ""
    return f"{config.PUBLIC_BASE_URL}/checkin/{token}"


def build_app(api, store, ocr_service, notify: Optional[NotifyFn] = None) -> web.Application:
    handler = CheckinWeb(api, store, ocr_service, notify)
    app = web.Application(client_max_size=config.MAX_UPLOAD_BYTES + 1024 * 1024)
    app.add_routes([
        web.get("/healthz", handler.healthz),
        web.get("/checkin", handler.lookup_page),
        web.post("/checkin", handler.lookup_submit),
        web.get("/checkin/{token}", handler.form_page),
        web.post("/checkin/{token}/scan", handler.scan),
        web.post("/checkin/{token}/submit", handler.submit),
        web.get("/", lambda r: web.HTTPFound("/checkin")),
    ])
    return app


async def start_web(app: web.Application) -> web.AppRunner:
    """Start the form on the bot's own event loop."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    logger.info(f"Check-in form listening on {config.WEB_HOST}:{config.WEB_PORT}")
    return runner
