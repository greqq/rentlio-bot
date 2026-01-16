"""
Form Filler Service using Playwright

Automatically fills Rentlio online check-in forms with guest data.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


@dataclass
class FormFillerResult:
    """Result of form filling operation"""
    success: bool
    message: str
    screenshot: Optional[bytes] = None  # Screenshot after filling
    error: Optional[str] = None


class FormFillerService:
    """Fills Rentlio online check-in forms using Playwright"""
    
    # URL patterns
    SHORT_URL_PATTERN = r'ci\.book\.rentl\.io/c/([a-f0-9-]+)/(\d+)'
    FULL_URL_PATTERN = r'([a-z-]+)\.book\.rentl\.io/reservation/check-in/([a-f0-9-]+)'
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.property_name = "sun-apartments"  # Default, can be extracted from URL
    
    def transform_url(self, url: str) -> str:
        """
        Transform short URL to full check-in URL
        
        ci.book.rentl.io/c/{uuid}/{code} -> sun-apartments.book.rentl.io/reservation/check-in/{uuid}
        """
        # Check if it's a short URL
        short_match = re.search(self.SHORT_URL_PATTERN, url)
        if short_match:
            uuid = short_match.group(1)
            return f"https://{self.property_name}.book.rentl.io/reservation/check-in/{uuid}"
        
        # Already a full URL
        full_match = re.search(self.FULL_URL_PATTERN, url)
        if full_match:
            return url
        
        # Unknown format, return as-is
        return url
    
    async def fill_form(self, url: str, guest_data: Dict[str, Any]) -> FormFillerResult:
        """
        Fill the online check-in form with guest data
        
        Args:
            url: Check-in URL (short or full format)
            guest_data: Dictionary with guest information
        
        Returns:
            FormFillerResult with success status and optional screenshot
        """
        # Transform URL if needed
        full_url = self.transform_url(url)
        logger.info(f"Filling form at: {full_url}")
        
        async with async_playwright() as p:
            try:
                # Launch browser (headless for server)
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    locale='hr-HR'
                )
                page = await context.new_page()
                
                # Navigate to the check-in page
                logger.info("Loading check-in page...")
                await page.goto(full_url, wait_until='networkidle', timeout=30000)
                await page.wait_for_timeout(2000)
                
                # Fill the form
                filled_fields = await self._fill_form_fields(page, guest_data)
                
                # Scroll to top for screenshot
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                
                # Take screenshot after filling
                screenshot = await page.screenshot(full_page=True)
                
                await browser.close()
                
                return FormFillerResult(
                    success=True,
                    message=f"✅ Ispunjeno {filled_fields} polja",
                    screenshot=screenshot
                )
                
            except PlaywrightTimeout as e:
                logger.error(f"Timeout loading page: {e}")
                return FormFillerResult(
                    success=False,
                    message="❌ Timeout - stranica se nije učitala",
                    error=str(e)
                )
            except Exception as e:
                logger.error(f"Form filling error: {e}")
                return FormFillerResult(
                    success=False,
                    message=f"❌ Greška: {str(e)}",
                    error=str(e)
                )
    
    async def _fill_form_fields(self, page: Page, guest_data: Dict[str, Any]) -> int:
        """Fill individual form fields. Returns count of filled fields."""
        filled = 0
        
        # Build full name
        full_name = guest_data.get('fullName', '')
        if not full_name:
            first = guest_data.get('firstName', '')
            last = guest_data.get('lastName', '')
            full_name = f"{first} {last}".strip()
        
        # 1. Fill IME I PREZIME (Name) - first input with this placeholder
        if full_name:
            try:
                name_input = page.locator('input[placeholder="Unesite ime i prezime"]').first
                if await name_input.count() > 0:
                    await name_input.clear()
                    await name_input.fill(full_name.upper())
                    filled += 1
                    logger.debug(f"Filled name: {full_name}")
            except Exception as e:
                logger.warning(f"Could not fill name: {e}")
        
        # 2. Fill DATUM ROĐENJA (Date of Birth) - format DD.MM.GGGG
        dob = guest_data.get('dateOfBirth', '')
        if dob:
            try:
                dob_input = page.locator('input[placeholder="Unesite datum (DD.MM.GGGG)"]').first
                if await dob_input.count() > 0:
                    await dob_input.clear()
                    await dob_input.fill(dob)
                    filled += 1
                    logger.debug(f"Filled DOB: {dob}")
            except Exception as e:
                logger.warning(f"Could not fill DOB: {e}")
        
        # 3. Select SPOL (Gender) - custom dropdown
        gender = guest_data.get('gender', '')
        if gender:
            try:
                # Click the dropdown placeholder
                gender_dropdown = page.get_by_text("-- odaberite spol --").first
                if await gender_dropdown.count() > 0:
                    await gender_dropdown.click()
                    await page.wait_for_timeout(300)
                    
                    # Select the option - options are "Ženski" and "Muški"
                    if gender == 'F':
                        option = page.get_by_text("Ženski", exact=True)
                    else:
                        option = page.get_by_text("Muški", exact=True)
                    
                    if await option.count() > 0:
                        await option.first.click()
                        filled += 1
                        logger.debug(f"Selected gender: {gender}")
                    else:
                        # Close dropdown by clicking elsewhere
                        await page.keyboard.press("Escape")
            except Exception as e:
                logger.warning(f"Could not select gender: {e}")
        
        # 4. Select TIP DOKUMENTA (Document Type) - default to Osobna iskaznica
        try:
            doc_type_dropdown = page.get_by_text("-- odaberite tip dokumenta --").first
            if await doc_type_dropdown.count() > 0:
                await doc_type_dropdown.click()
                await page.wait_for_timeout(300)
                
                osobna = page.get_by_text("Osobna iskaznica")
                if await osobna.count() > 0:
                    await osobna.first.click()
                    filled += 1
                    logger.debug("Selected document type: Osobna iskaznica")
                else:
                    await page.keyboard.press("Escape")
        except Exception as e:
            logger.warning(f"Could not select document type: {e}")
        
        # 5. Fill BROJ DOKUMENTA (Document Number)
        doc_number = guest_data.get('documentNumber', '')
        if doc_number:
            try:
                doc_input = page.locator('input[placeholder="Unesite broj dokumenta"]').first
                if await doc_input.count() > 0:
                    await doc_input.clear()
                    await doc_input.fill(doc_number)
                    filled += 1
                    logger.debug(f"Filled document number: {doc_number}")
            except Exception as e:
                logger.warning(f"Could not fill document number: {e}")
        
        # 6. Fill MJESTO PREBIVALIŠTA (Place of Residence)
        residence = guest_data.get('placeOfResidence', '')
        if residence:
            try:
                res_input = page.locator('input[placeholder="Unesite mjesto prebivališta"]').first
                if await res_input.count() > 0:
                    await res_input.clear()
                    await res_input.fill(residence.upper())
                    filled += 1
                    logger.debug(f"Filled residence: {residence}")
            except Exception as e:
                logger.warning(f"Could not fill residence: {e}")
        
        # 7. Select DRŽAVA ROĐENJA (Country of Birth) - searchable dropdown
        nationality = guest_data.get('nationality', '')
        if nationality:
            try:
                country_input = page.locator('input[placeholder="-- odaberite državu rođenja --"]').first
                if await country_input.count() > 0 and await country_input.is_visible():
                    # Get search term and full display name
                    search_term, display_name = self._get_country_search_term(nationality)
                    await country_input.click()
                    await page.wait_for_timeout(300)
                    await country_input.fill(search_term)
                    await page.wait_for_timeout(800)
                    
                    # Click on the dropdown option (not just Enter)
                    country_option = page.get_by_text(display_name)
                    if await country_option.count() > 0:
                        await country_option.first.click()
                        filled += 1
                        logger.debug(f"Selected country: {display_name}")
                    else:
                        # Fallback: use arrow down + enter
                        await page.keyboard.press("ArrowDown")
                        await page.wait_for_timeout(200)
                        await page.keyboard.press("Enter")
                        filled += 1
                        logger.debug(f"Selected country via keyboard: {nationality}")
                else:
                    logger.warning("Country input not found or not visible")
            except Exception as e:
                logger.warning(f"Could not select country: {e}")
        
        # 8. Click "Spremi goste" button to submit
        submit_success = False
        try:
            # Scroll to bottom to find the submit button
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            
            # Use exact text match - reliable and doesn't depend on auto-generated classes
            submit_button = page.get_by_text("Spremi goste", exact=True)
            
            if await submit_button.count() > 0:
                await submit_button.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                await submit_button.click(force=True)
                logger.info("Clicked Spremi goste button")
                # Wait for form to submit
                await page.wait_for_timeout(2000)
                
                # Check for validation error
                error_msg = page.locator('text="Obrazac ima pogreške, molimo provjerite obrazac."')
                if await error_msg.count() > 0:
                    logger.warning("Form validation error - some fields may be invalid")
                else:
                    submit_success = True
                    logger.info("Form submitted successfully!")
            else:
                logger.warning("Spremi goste button not found")
        except Exception as e:
            logger.warning(f"Could not click submit button: {e}")
        
        logger.info(f"Filled {filled} form fields")
        return filled
    
    def _get_country_search_term(self, nationality: str) -> tuple[str, str]:
        """Convert nationality to (search_term, display_name) for dropdown selection"""
        # Maps nationality to (partial search term, full display name in dropdown)
        mapping = {
            'hrvatska': ('Cro', 'Croatia (Hrvatska)'),
            'croatian': ('Cro', 'Croatia (Hrvatska)'),
            'hrv': ('Cro', 'Croatia (Hrvatska)'),
            'njemačka': ('Germ', 'Germany (Njemačka)'),
            'german': ('Germ', 'Germany (Njemačka)'),
            'austrija': ('Aust', 'Austria (Austrija)'),
            'austrian': ('Aust', 'Austria (Austrija)'),
            'slovenija': ('Slov', 'Slovenia (Slovenija)'),
            'slovenian': ('Slov', 'Slovenia (Slovenija)'),
            'srbija': ('Serb', 'Serbia (Srbija)'),
            'serbian': ('Serb', 'Serbia (Srbija)'),
            'italija': ('Ital', 'Italy (Italija)'),
            'italian': ('Ital', 'Italy (Italija)'),
            'mađarska': ('Hung', 'Hungary (Mađarska)'),
            'hungarian': ('Hung', 'Hungary (Mađarska)'),
            'bosna': ('Bosn', 'Bosnia and Herzegovina'),
            'bosnian': ('Bosn', 'Bosnia and Herzegovina'),
        }
        return mapping.get(nationality.lower(), (nationality[:4], nationality))


# Singleton instance
form_filler = FormFillerService()
