"""Shared check-in logic.

Everything here used to live inside bot.py, tangled with the Telegram
callback that happened to call it. The self check-in form needs the exact
same behaviour - match guests already on the reservation, POST only the new
ones, PUT the eVisitor fields - so it lives in one place and both callers
format the outcome themselves.
"""
import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.services.country_mapper import country_mapper
from src.services.ocr_service import strip_diacritics
from src.services.rentlio_api import RentlioAPI, RentlioAPIError

logger = logging.getLogger(__name__)


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


def get_document_type_id(doc_type: str, nationality: str = None) -> Optional[int]:
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


def get_tourist_tax_category(date_of_birth: str) -> int:
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


def name_key(name: str) -> str:
    """Comparable form of a guest name.

    OCR gives "DORA MASANOVIC", Rentlio may hold "Dora Mašanović", and a
    booking channel may put the surname first. Compare on sorted, accent-free,
    lowercase words so all three land on the same key.
    """
    if not name:
        return ""
    words = re.findall(r"[^\W\d_]+", strip_diacritics(name).lower(), re.UNICODE)
    return " ".join(sorted(words))


def match_existing_guest(guest, existing: list) -> Optional[dict]:
    """Find the guest already on the reservation, if this is the same person.

    Booking channels put the holder on the reservation before anyone scans a
    document, so the common case is that the person we just read from an ID is
    already there with empty fields. Adding them again is what produced
    duplicate registrations.
    """
    name = guest.full_name or f"{guest.first_name or ''} {guest.last_name or ''}"
    key = name_key(name)
    if not key:
        return None
    for candidate in existing:
        if name_key(candidate.get("name", "")) == key:
            return candidate
    return None


# Fields Rentlio must hold before a guest can be registered in eVisitor. We
# read them back from the same endpoint we wrote to, so a value that did not
# stick surfaces now and not weeks later in eVisitor.
REQUIRED_FOR_EVISITOR = (
    "documentNumber",
    "eVisitorDocumentTypeId",
    "eVisitorTouristTaxCategoryId",
    "dateOfBirth",
)


@dataclass
class CheckinOutcome:
    """What actually happened, for the caller to render."""
    names: list = field(default_factory=list)
    guest_ids: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    checkin_marked: bool = False
    checkin_error: Optional[str] = None

    @property
    def fully_successful(self) -> bool:
        return bool(self.guest_ids) and all(self.guest_ids)

    @property
    def partially_successful(self) -> bool:
        return any(self.guest_ids)


def guest_display_name(guest, fallback_index: int = 0) -> str:
    """Best available name for a guest, never empty."""
    name = guest.full_name
    if not name and (guest.first_name or guest.last_name):
        name = f"{guest.first_name or ''} {guest.last_name or ''}".strip()
    return name or f"Gost {fallback_index + 1}"


def build_guest_payloads(guests: list, existing_guests: list) -> tuple[list, list, list]:
    """Turn extracted guests into Rentlio payloads.

    Returns (api_guests, guest_doc_data, guest_ids) where guest_ids holds the
    id of the guest already on the reservation, or None for one we still have
    to create.
    """
    has_primary = any(g.get("isPrimary") == "Y" for g in existing_guests)

    api_guests = []
    guest_doc_data = []
    guest_ids = []

    for i, guest in enumerate(guests):
        name = guest_display_name(guest, i)

        country_id = None
        if guest.nationality:
            country_id = country_mapper.get_country_id(guest.nationality)

        matched = match_existing_guest(guest, existing_guests)
        if matched:
            # Keep the role the reservation already assigned - overwriting a
            # booker or primary flag would reshuffle the reservation.
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

        if guest.gender:
            gender_id = convert_gender_to_id(guest.gender)
            if gender_id:
                api_guest["genderId"] = gender_id

        if country_id:
            api_guest["countryId"] = country_id
            api_guest["citizenshipCountryId"] = country_id
            api_guest["countryOfBirthId"] = country_id
            api_guest["countryOfResidenceId"] = country_id

        if guest.place_of_residence:
            api_guest["cityOfResidence"] = guest.place_of_residence

        if getattr(guest, 'address', None):
            api_guest["address"] = guest.address

        # Keep document details in the note too - if a structured field is
        # rejected the number is still readable in Rentlio.
        note_parts = []
        if guest.document_number:
            note_parts.append(f"Doc: {guest.document_number}")
        if guest.expiry_date:
            note_parts.append(f"Exp: {guest.expiry_date}")
        if guest.oib:
            note_parts.append(f"OIB: {guest.oib}")
        if note_parts:
            api_guest["note"] = " | ".join(note_parts)

        doc_fields = {}
        if guest.document_number:
            doc_fields["documentNumber"] = str(guest.document_number)
        doc_type = getattr(guest, 'document_type', None)
        if doc_type:
            doc_type_id = get_document_type_id(doc_type, guest.nationality)
            if doc_type_id:
                doc_fields["eVisitorDocumentTypeId"] = doc_type_id
        doc_fields["arrivalArrangementId"] = 2   # Personal (1 is Agency)
        doc_fields["providedServicesTypeId"] = 1  # Accommodation
        doc_fields["eVisitorTouristTaxCategoryId"] = get_tourist_tax_category(
            guest.date_of_birth
        )
        guest_doc_data.append(doc_fields)

        logger.info(f"Guest {i+1} POST data: {api_guest}")
        logger.info(f"Guest {i+1} doc fields (for PUT): {doc_fields}")
        api_guests.append(api_guest)

    return api_guests, guest_doc_data, guest_ids


async def apply_guests_to_reservation(
    api: RentlioAPI,
    reservation_id: str,
    guests: list,
    mark_checked_in: bool = True,
) -> CheckinOutcome:
    """Write guests to a reservation and optionally flip it to checked-in.

    Raises RentlioAPIError if the POST itself fails; softer failures (the PUT,
    the verification read, the status flip) come back inside the outcome so the
    caller can report a partial success instead of losing everything.
    """
    outcome = CheckinOutcome()

    # A channel booking already carries its holder, so scanning that person's
    # ID and blindly POSTing them added the same human twice. Read what is
    # there first and update in place where it is the same person.
    existing_guests = []
    try:
        existing_guests = await api.get_reservation_guests_v2(reservation_id)
    except Exception as e:
        logger.warning(f"Could not read existing guests, treating all as new: {e}")
    logger.info(
        f"Reservation {reservation_id} already has {len(existing_guests)} guest(s)"
    )

    api_guests, guest_doc_data, guest_ids = build_guest_payloads(guests, existing_guests)
    outcome.names = [g["name"] for g in api_guests]

    # Phase 1: POST - create only the guests not already on the reservation
    new_indices = [i for i, gid in enumerate(guest_ids) if gid is None]
    if new_indices:
        result = await api.add_reservation_guests(
            reservation_id, [api_guests[i] for i in new_indices]
        )
        added = result.get('guestAdded', [])
        outcome.messages = list(result.get('messages', []))
        logger.info(f"POST result: added={added}, messages={outcome.messages}")
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
            logger.info(
                f"PUT result: updated={update_result.get('guestUpdated', [])}, "
                f"messages={update_result.get('messages', [])}"
            )
            update_msgs = update_result.get('messages', [])
            if update_msgs:
                outcome.messages.extend(update_msgs)
        except Exception as e:
            logger.error(f"PUT update failed: {e}")
            outcome.messages.append("⚠️ Dokument polja: potreban ručni unos")

    # Verify against the same endpoint we wrote to.
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
            outcome.messages.append("⚠️ Nedostaje za eVisitor — " + " | ".join(incomplete))
    except Exception as e:
        logger.warning(f"Verify GET failed: {e}")
        outcome.messages.append("⚠️ Nisam mogao provjeriti spremljene podatke")

    outcome.guest_ids = guest_ids

    # Mark checked-in once every guest is on the reservation, whether we
    # created them or updated one that was already there.
    if mark_checked_in and any(guest_ids):
        try:
            checkin_result = await api.checkin_reservation(reservation_id)
            logger.info(f"Checkin result: {checkin_result}")
            outcome.checkin_marked = True
        except RentlioAPIError as e:
            logger.warning(f"Checkin status update failed: {e.message}")
            outcome.checkin_error = e.message

    return outcome


# Fields the self check-in form sends back, mapped onto ExtractedGuestData.
_FORM_FIELDS = {
    "fullName": "full_name",
    "firstName": "first_name",
    "lastName": "last_name",
    "dateOfBirth": "date_of_birth",
    "documentNumber": "document_number",
    "documentType": "document_type",
    "nationality": "nationality",
    "gender": "gender",
    "placeOfResidence": "place_of_residence",
    "address": "address",
    "expiryDate": "expiry_date",
    "oib": "oib",
}


def guest_from_form(payload: dict):
    """Rebuild an ExtractedGuestData from what the form posted back.

    The guest may have corrected any field, so the typed value always wins
    over what OCR read. first/last name are dropped when a full name is
    present - keeping them risks writing a stale spelling the guest just
    fixed.
    """
    from src.services.ocr_service import ExtractedGuestData

    guest = ExtractedGuestData(extraction_method="self_checkin_form")
    for form_key, attr in _FORM_FIELDS.items():
        value = payload.get(form_key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            setattr(guest, attr, value)

    if guest.full_name:
        guest.first_name = None
        guest.last_name = None

    return guest
