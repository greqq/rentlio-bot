"""
OCR Service using Google Cloud Vision API

Extracts guest information from ID photos.
Images are processed in memory and never stored.

Supports:
- Croatian ID cards (osobna iskaznica) - front and back
- MRZ (Machine Readable Zone) parsing for reliable extraction
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List
from google.cloud import vision

from src.config import config

logger = logging.getLogger(__name__)

# Country code to name mapping
COUNTRY_CODES = {
    'HRV': 'Hrvatska',
    'CRO': 'Hrvatska', 
    'DEU': 'Njemačka',
    'GER': 'Njemačka',
    'AUT': 'Austrija',
    'ITA': 'Italija',
    'SVN': 'Slovenija',
    'SRB': 'Srbija',
    'BIH': 'Bosna i Hercegovina',
    'HUN': 'Mađarska',
    'CZE': 'Češka',
    'POL': 'Poljska',
    'SVK': 'Slovačka',
    'GBR': 'Ujedinjeno Kraljevstvo',
    'FRA': 'Francuska',
}


@dataclass
class ExtractedGuestData:
    """Guest data extracted from ID"""
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    date_of_birth: Optional[str] = None  # Format: DD.MM.YYYY
    document_number: Optional[str] = None
    document_type: Optional[str] = None  # "ID_CARD", "PASSPORT"
    nationality: Optional[str] = None
    gender: Optional[str] = None  # M or F
    place_of_residence: Optional[str] = None
    address: Optional[str] = None  # Street address
    expiry_date: Optional[str] = None
    oib: Optional[str] = None  # Croatian OIB
    raw_text: str = ""
    confidence: float = 0.0
    extraction_method: str = ""  # How data was extracted
    
    def is_valid(self) -> bool:
        """Check if we extracted minimum required data"""
        has_name = bool(self.full_name or (self.first_name and self.last_name))
        has_doc = bool(self.document_number)
        return has_name and has_doc
    
    def to_dict(self) -> dict:
        """Convert to dictionary for form filling"""
        full_name = self.full_name
        if not full_name and (self.first_name or self.last_name):
            full_name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        
        return {
            "firstName": self.first_name or "",
            "lastName": self.last_name or "",
            "fullName": full_name or "",
            "dateOfBirth": self.date_of_birth or "",
            "documentNumber": self.document_number or "",
            "documentType": self.document_type or "",
            "nationality": self.nationality or "",
            "gender": self.gender or "",
            "placeOfResidence": self.place_of_residence or "",
            "address": self.address or "",
            "expiryDate": self.expiry_date or "",
            "oib": self.oib or "",
        }
    
    def format_telegram(self) -> str:
        """Format for Telegram message"""
        lines = ["📋 **Izvučeni podaci:**\n"]
        
        name = self.full_name
        if not name and (self.first_name or self.last_name):
            name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        
        if name:
            lines.append(f"👤 Ime: **{name}**")
        
        if self.date_of_birth:
            lines.append(f"🎂 Datum rođenja: {self.date_of_birth}")
        
        if self.document_number:
            doc_type_label = ""
            if self.document_type == "ID_CARD":
                doc_type_label = " (Osobna)"
            elif self.document_type == "PASSPORT":
                doc_type_label = " (Putovnica)"
            lines.append(f"🪪 Broj dokumenta: {self.document_number}{doc_type_label}")
        
        if self.gender:
            gender_text = "Žensko" if self.gender == 'F' else "Muško"
            lines.append(f"⚧ Spol: {gender_text}")
        
        if self.nationality:
            lines.append(f"🌍 Državljanstvo: {self.nationality}")
            
        if self.place_of_residence:
            lines.append(f"🏠 Prebivalište: {self.place_of_residence}")
            
        return "\n".join(lines)


class OCRService:
    """Google Cloud Vision OCR Service"""
    
    def __init__(self):
        self.client = vision.ImageAnnotatorClient()
    
    async def extract_from_bytes(self, image_bytes: bytes) -> ExtractedGuestData:
        """
        Extract guest data from image bytes
        
        Args:
            image_bytes: Raw image data
            
        Returns:
            ExtractedGuestData object
        """
        try:
            # Create image object
            image = vision.Image(content=image_bytes)
            
            # Perform text detection
            response = self.client.text_detection(image=image)
            
            if response.error.message:
                logger.error(f"Vision API error: {response.error.message}")
                return ExtractedGuestData(raw_text=f"Error: {response.error.message}")
            
            # Get full text
            texts = response.text_annotations
            if not texts:
                return ExtractedGuestData(raw_text="No text found in image")
            
            full_text = texts[0].description
            logger.info(f"OCR extracted {len(full_text)} characters")
            logger.debug(f"Raw text:\n{full_text}")
            
            # Parse the text
            guest_data = self._parse_id_text(full_text)
            guest_data.raw_text = full_text
            
            return guest_data
            
        except Exception as e:
            logger.error(f"OCR error: {e}")
            return ExtractedGuestData(raw_text=f"Error: {str(e)}")
    
    def _parse_id_text(self, text: str) -> ExtractedGuestData:
        """
        Parse extracted text to find guest information
        
        Priority:
        1. MRZ (Machine Readable Zone) - most reliable
        2. Croatian ID specific labels
        3. Generic patterns
        """
        data = ExtractedGuestData()
        
        # First try MRZ parsing (most reliable)
        mrz_data = self._parse_mrz(text)
        if mrz_data.is_valid():
            logger.info("Extracted data from MRZ")
            mrz_data.extraction_method = "MRZ"
            # Also try to get residence from visual text (not in MRZ)
            city, address = self._extract_residence(text)
            if city:
                mrz_data.place_of_residence = city
            if address:
                mrz_data.address = address
            return mrz_data
        
        # Try Croatian ID specific parsing
        croatian_data = self._parse_croatian_id(text)
        if croatian_data.is_valid():
            logger.info("Extracted data from Croatian ID labels")
            croatian_data.extraction_method = "Croatian ID"
            return croatian_data
        
        # Fallback to generic parsing
        generic_data = self._parse_generic(text)
        generic_data.extraction_method = "Generic"
        return generic_data
    
    def _parse_mrz(self, text: str) -> ExtractedGuestData:
        """
        Parse MRZ (Machine Readable Zone) from ID card
        
        Croatian ID MRZ format (3 lines):
        Line 1: IOHRV + document_number + check + OIB + filler (has digits)
        Line 2: DOB(YYMMDD) + check + sex + expiry(YYMMDD) + check + nationality + filler
        Line 3: SURNAME<<FIRSTNAME<<<... (letters only, no digits!)
        """
        data = ExtractedGuestData()
        lines = text.split('\n')
        
        # Find MRZ lines (contain lots of < characters or specific patterns)
        mrz_lines = []
        for line in lines:
            clean = line.strip().replace(' ', '')
            # MRZ lines typically have < characters
            if '<' in clean and len(clean) >= 20:
                mrz_lines.append(clean)
            # Also check for MRZ-like patterns without < (OCR might miss them)
            elif re.match(r'^[A-Z0-9]{20,}$', clean) and any(c.isdigit() for c in clean):
                mrz_lines.append(clean)
        
        if len(mrz_lines) < 2:
            return data
        
        logger.debug(f"Found MRZ lines: {mrz_lines}")
        
        # Find the NAME line - it's the one with << that has NO digits (only letters and <)
        name_line = None
        for line in mrz_lines:
            if '<<' in line:
                # Name line should have no digits (or very few at the end as check digit)
                # Count digits - name line typically has 0-1 digit at most
                digit_count = sum(1 for c in line if c.isdigit())
                if digit_count <= 1:
                    name_line = line
                    break
        
        if name_line:
            # Format: SURNAME<<FIRSTNAME<<<<<...
            # The line might start with random chars, find the name pattern
            match = re.search(r'([A-Z]{2,})<<([A-Z]+)', name_line)
            if match:
                data.last_name = match.group(1).title()
                data.first_name = match.group(2).replace('<', ' ').strip().title()
                data.full_name = f"{data.first_name} {data.last_name}"
        
        # Parse document info lines
        for line in mrz_lines:
            # Croatian ID line 1: IOHRV + 9 digit doc number + check + OIB(11)
            match = re.search(r'I[OACD]?HRV(\d{9})', line)
            if match:
                data.document_number = match.group(1)
                data.document_type = "ID_CARD"
                data.nationality = 'Hrvatska'
                # Extract OIB (11 digits after doc number + check digit)
                oib_match = re.search(r'I[OACD]?HRV\d{10}(\d{11})', line)
                if oib_match:
                    data.oib = oib_match.group(1)
                continue
            
            # Passport line 1: P<HRV or PHRV
            if re.search(r'P[<A-Z]?HRV', line):
                data.document_type = "PASSPORT"
                data.nationality = 'Hrvatska'
                # Extract passport number (after country code)
                pass_match = re.search(r'P[<A-Z]?HRV([A-Z0-9]{7,9})', line)
                if pass_match:
                    data.document_number = pass_match.group(1)
                continue
            
            # Line 2: YYMMDD (DOB) + check + sex + YYMMDD (expiry)
            match = re.search(r'(\d{6})(\d)([MF<])(\d{6})', line)
            if match:
                dob_raw = match.group(1)  # YYMMDD
                data.gender = match.group(3) if match.group(3) != '<' else None
                expiry_raw = match.group(4)  # YYMMDD
                
                # Convert YYMMDD to DD.MM.YYYY
                data.date_of_birth = self._mrz_date_to_normal(dob_raw)
                data.expiry_date = self._mrz_date_to_normal(expiry_raw)
                
                # Check for nationality code after
                nat_match = re.search(r'[MF<]\d{6}\d([A-Z]{3})', line)
                if nat_match:
                    code = nat_match.group(1)
                    data.nationality = COUNTRY_CODES.get(code, code)
        
        return data
    
    def _mrz_date_to_normal(self, mrz_date: str) -> str:
        """Convert YYMMDD to DD.MM.YYYY"""
        if len(mrz_date) != 6:
            return ""
        try:
            yy = int(mrz_date[0:2])
            mm = mrz_date[2:4]
            dd = mrz_date[4:6]
            # Determine century (assume 00-30 is 2000s, 31-99 is 1900s)
            year = 2000 + yy if yy <= 30 else 1900 + yy
            return f"{dd}.{mm}.{year}"
        except:
            return ""
    
    def _parse_croatian_id(self, text: str) -> ExtractedGuestData:
        """Parse Croatian ID card using labeled fields"""
        data = ExtractedGuestData()
        text_upper = text.upper()
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        
        # Helper to find value after a label
        def find_after_label(patterns: List[str], text_lines: List[str]) -> Optional[str]:
            for i, line in enumerate(text_lines):
                line_upper = line.upper()
                for pattern in patterns:
                    if pattern in line_upper:
                        # Value might be on same line or next line
                        # Try same line first (after the label)
                        remaining = line_upper.split(pattern)[-1].strip()
                        if remaining and not remaining.startswith('/'):
                            # Clean up any trailing labels
                            value = re.split(r'[A-ZČĆŠĐŽ]{2,}/', remaining)[0].strip()
                            if value:
                                return value
                        # Try next line
                        if i + 1 < len(text_lines):
                            next_line = text_lines[i + 1].strip()
                            # Skip if next line is another label
                            if not re.match(r'^[A-ZČĆŠĐŽ]+/', next_line.upper()):
                                return next_line
            return None
        
        # Extract surname (PREZIME)
        surname = find_after_label(['PREZIME/SURNAME', 'PREZIME', 'SURNAME'], lines)
        if surname:
            # Clean up - take only the name part
            surname = re.sub(r'\d+', '', surname).strip()
            if surname and len(surname) > 1:
                data.last_name = surname.title()
        
        # Extract first name (IME)  
        first_name = find_after_label(['IME/NAME', 'NAME'], lines)
        if first_name:
            first_name = re.sub(r'\d+', '', first_name).strip()
            if first_name and len(first_name) > 1:
                data.first_name = first_name.title()
        
        # Build full name
        if data.first_name and data.last_name:
            data.full_name = f"{data.first_name} {data.last_name}"
        
        # Extract DOB - look for DD MM YYYY pattern near DOB label
        dob_section = None
        for i, line in enumerate(lines):
            if 'ROĐENJA' in line.upper() or 'BIRTH' in line.upper():
                # Get this and next few lines
                dob_section = ' '.join(lines[i:i+3])
                break
        
        if dob_section:
            # Pattern: DD MM YYYY or DD.MM.YYYY
            match = re.search(r'(\d{1,2})\s*[.\s]\s*(\d{1,2})\s*[.\s]\s*(\d{4})', dob_section)
            if match:
                data.date_of_birth = f"{match.group(1).zfill(2)}.{match.group(2).zfill(2)}.{match.group(3)}"
        
        # Extract document number (9 digits for Croatian ID)
        for line in lines:
            if 'BROJ' in line.upper() and 'ISKAZNIC' in line.upper():
                # Next line should have the number
                idx = lines.index(line)
                if idx + 1 < len(lines):
                    num_match = re.search(r'(\d{9})', lines[idx + 1])
                    if num_match:
                        data.document_number = num_match.group(1)
                        break
        
        # If not found by label, look for 9-digit number that's not OIB
        if not data.document_number:
            for line in lines:
                if 'OIB' not in line.upper() and 'MBG' not in line.upper():
                    match = re.search(r'\b(\d{9})\b', line)
                    if match:
                        data.document_number = match.group(1)
                        break
        
        # Extract gender
        for line in lines:
            line_upper = line.upper()
            if 'SPOL' in line_upper or 'SEX' in line_upper:
                if 'Ž' in line or 'Z/F' in line_upper or '/F' in line_upper:
                    data.gender = 'F'
                elif 'M/' in line_upper or '/M' in line_upper:
                    data.gender = 'M'
                break
        
        # Extract nationality
        if 'HRV' in text_upper or 'HRVATSKA' in text_upper or 'CROATIA' in text_upper:
            data.nationality = 'Hrvatska'
        
        # Extract residence
        city, address = self._extract_residence(text)
        if city:
            data.place_of_residence = city
        if address:
            data.address = address
        
        return data
    
    def _extract_residence(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """
        Extract place of residence (city) and address from text
        
        Returns:
            (city, address) tuple
        """
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        city = None
        address = None
        
        for i, line in enumerate(lines):
            line_upper = line.upper()
            if 'PREBIVALIŠTE' not in line_upper and 'RESIDENCE' not in line_upper:
                continue
            
            # Check if city is on the same line (after the label)
            # e.g. "PREBIVALIŠTE/RESIDENCE LADIMIREVCI, VALPOVO"
            after_label = line_upper
            for label in ['PREBIVALIŠTE/RESIDENCE', 'PREBIVALIŠTE', 'RESIDENCE']:
                if label in after_label:
                    after_label = after_label.split(label, 1)[-1].strip()
                    break
            
            if after_label and len(after_label) > 2 and not after_label.startswith('/'):
                # City is on the same line
                parts = after_label.split(',')
                city = parts[0].strip().title()
                # Next line might be the address
                if i + 1 < len(lines):
                    addr_line = lines[i + 1].strip()
                    if not any(lbl in addr_line.upper() for lbl in ['IZDALA', 'ISSUED', 'DATUM', 'OIB', 'MBG', 'PREBIVALIŠTE']):
                        address = addr_line.title()
            elif i + 1 < len(lines):
                # City is on the next line
                next_line = lines[i + 1].strip()
                if any(lbl in next_line.upper() for lbl in ['IZDALA', 'ISSUED', 'DATUM', 'OIB', 'MBG']):
                    continue
                if next_line:
                    # Format: "LADIMIREVCI, VALPOVO" - take the full city string
                    city = next_line.title()
                    # Check for address on the line after
                    if i + 2 < len(lines):
                        addr_line = lines[i + 2].strip()
                        if not any(lbl in addr_line.upper() for lbl in ['IZDALA', 'ISSUED', 'DATUM', 'OIB', 'MBG']):
                            # If it looks like a street address (has number), save it
                            if re.search(r'\d', addr_line):
                                address = addr_line.title()
            
            if city:
                break
        
        return city, address
    
    def _parse_generic(self, text: str) -> ExtractedGuestData:
        """Generic parsing fallback"""
        data = ExtractedGuestData()
        text_upper = text.upper()
        
        # Try to find any 9-digit number as document
        match = re.search(r'\b(\d{9})\b', text)
        if match:
            data.document_number = match.group(1)
        
        # Try to find date pattern
        match = re.search(r'(\d{1,2})[.\s/](\d{1,2})[.\s/](\d{4})', text)
        if match:
            data.date_of_birth = f"{match.group(1).zfill(2)}.{match.group(2).zfill(2)}.{match.group(3)}"
        
        # Try to find capitalized name-like words
        name_match = re.search(r'\b([A-ZČĆŠĐŽ]{2,})\s+([A-ZČĆŠĐŽ]{2,})\b', text_upper)
        if name_match:
            # Filter out common non-name words
            skip_words = {'REPUBLIKA', 'HRVATSKA', 'CROATIA', 'OSOBNA', 'ISKAZNICA', 'IDENTITY', 
                         'CARD', 'PREZIME', 'SURNAME', 'IME', 'NAME', 'DATUM', 'DATE', 'SPOL',
                         'SEX', 'BROJ', 'NUMBER', 'PREBIVALIŠTE', 'RESIDENCE'}
            word1, word2 = name_match.group(1), name_match.group(2)
            if word1 not in skip_words and word2 not in skip_words:
                data.first_name = word1.title()
                data.last_name = word2.title()
                data.full_name = f"{data.first_name} {data.last_name}"
        
        return data


# Singleton instance
ocr_service = OCRService()
