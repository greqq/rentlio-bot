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
    
    async def fill_form(self, url: str, guests_data: list | Dict[str, Any]) -> FormFillerResult:
        """
        Fill the online check-in form with guest data
        
        Args:
            url: Check-in URL (short or full format)
            guests_data: List of guest dictionaries OR single guest dict for backward compatibility
        
        Returns:
            FormFillerResult with success status and optional screenshot
        """
        # Handle single guest (backward compatibility)
        if isinstance(guests_data, dict):
            guests_data = [guests_data]
        
        # Transform URL if needed
        full_url = self.transform_url(url)
        logger.info(f"Filling form at: {full_url} for {len(guests_data)} guest(s)")
        
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
                
                # Fill form for all guests
                total_filled = 0
                for guest_index, guest_data in enumerate(guests_data):
                    logger.info(f"Filling guest {guest_index + 1}/{len(guests_data)}: {guest_data.get('fullName', 'Unknown')}")
                    filled = await self._fill_guest_section(page, guest_data, guest_index)
                    total_filled += filled
                
                # Click submit after filling all guests
                await self._click_submit(page)
                
                # Scroll to top for screenshot
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                
                # Take screenshot after filling
                screenshot = await page.screenshot(full_page=True)
                
                await browser.close()
                
                return FormFillerResult(
                    success=True,
                    message=f"✅ Ispunjeno {total_filled} polja za {len(guests_data)} gost(a)",
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
    
    async def _fill_guest_section(self, page: Page, guest_data: Dict[str, Any], guest_index: int) -> int:
        """Fill form fields for a specific guest section. Returns count of filled fields."""
        filled = 0
        
        # Build full name
        full_name = guest_data.get('fullName', '')
        if not full_name:
            first = guest_data.get('firstName', '')
            last = guest_data.get('lastName', '')
            full_name = f"{first} {last}".strip()
        
        # Determine the guest section container
        # Guest 1 is "Primarni gost", Guest 2+ is "Gost N"
        if guest_index == 0:
            section_header = "Primarni gost"
        else:
            section_header = f"Gost {guest_index + 1}"
        
        logger.debug(f"Looking for section: {section_header}")
        
        # Scroll to the guest section header first
        section_el = page.get_by_text(section_header, exact=True)
        if await section_el.count() > 0:
            await section_el.first.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
        
        # Get all inputs for each field type - use nth() to target specific guest section
        name_inputs = page.locator('input[placeholder="Unesite ime i prezime"]')
        dob_inputs = page.locator('input[placeholder="Unesite datum (DD.MM.GGGG)"]')
        doc_inputs = page.locator('input[placeholder="Unesite broj dokumenta"]')
        res_inputs = page.locator('input[placeholder="Unesite mjesto prebivališta"]')
        
        # 1. Fill IME I PREZIME (Name)
        if full_name:
            try:
                name_input = name_inputs.nth(guest_index)
                if await name_input.count() > 0:
                    await name_input.scroll_into_view_if_needed()
                    await name_input.clear()
                    await name_input.fill(full_name.upper())
                    filled += 1
                    logger.debug(f"Guest {guest_index+1}: Filled name: {full_name}")
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not fill name: {e}")
        
        # 2. Fill DATUM ROĐENJA (Date of Birth)
        dob = guest_data.get('dateOfBirth', '')
        if dob:
            try:
                dob_input = dob_inputs.nth(guest_index)
                if await dob_input.count() > 0:
                    await dob_input.clear()
                    await dob_input.fill(dob)
                    filled += 1
                    logger.debug(f"Guest {guest_index+1}: Filled DOB: {dob}")
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not fill DOB: {e}")
        
        # 3. Select SPOL (Gender) - click dropdown then click LAST visible option (in dropdown menu)
        gender = guest_data.get('gender', '')
        if gender:
            try:
                gender_dropdowns = page.get_by_text("-- odaberite spol --")
                clicked = False
                
                for i in range(await gender_dropdowns.count()):
                    el = gender_dropdowns.nth(i)
                    if await el.is_visible():
                        await el.scroll_into_view_if_needed()
                        await page.wait_for_timeout(300)
                        await el.click()
                        await page.wait_for_timeout(500)
                        
                        # Click the option - use LAST visible (dropdown menu appears below existing values)
                        if gender == 'F':
                            opt = page.get_by_text("Ženski", exact=True)
                        else:
                            opt = page.get_by_text("Muški", exact=True)
                        
                        # Find the option with highest Y coordinate (in dropdown menu)
                        best_idx = -1
                        best_y = -1
                        for j in range(await opt.count()):
                            o = opt.nth(j)
                            if await o.is_visible():
                                box = await o.bounding_box()
                                if box and box['y'] > best_y:
                                    best_y = box['y']
                                    best_idx = j
                        
                        if best_idx >= 0:
                            await opt.nth(best_idx).click()
                            clicked = True
                            filled += 1
                            logger.debug(f"Guest {guest_index+1}: Selected gender: {gender} (idx={best_idx}, y={best_y:.0f})")
                        break
                
                if not clicked:
                    logger.warning(f"Guest {guest_index+1}: Could not select gender")
                
                await page.wait_for_timeout(300)
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not select gender: {e}")
        
        # 4. Select TIP DOKUMENTA (Document Type) - click dropdown then click LAST visible option
        try:
            doc_dropdowns = page.get_by_text("-- odaberite tip dokumenta --")
            clicked = False
            
            for i in range(await doc_dropdowns.count()):
                el = doc_dropdowns.nth(i)
                if await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    await el.click()
                    await page.wait_for_timeout(500)
                    
                    # Click option with highest Y coordinate (in dropdown menu)
                    opt = page.get_by_text("Osobna iskaznica")
                    best_idx = -1
                    best_y = -1
                    for j in range(await opt.count()):
                        o = opt.nth(j)
                        if await o.is_visible():
                            box = await o.bounding_box()
                            if box and box['y'] > best_y:
                                best_y = box['y']
                                best_idx = j
                    
                    if best_idx >= 0:
                        await opt.nth(best_idx).click()
                        clicked = True
                        filled += 1
                        logger.debug(f"Guest {guest_index+1}: Selected doc type (idx={best_idx}, y={best_y:.0f})")
                    break
            
            if not clicked:
                logger.warning(f"Guest {guest_index+1}: Could not select doc type")
            
            await page.wait_for_timeout(300)
        except Exception as e:
            logger.warning(f"Guest {guest_index+1}: Could not select document type: {e}")
        
        # 5. Fill BROJ DOKUMENTA (Document Number)
        doc_number = guest_data.get('documentNumber', '')
        if doc_number:
            try:
                doc_input = doc_inputs.nth(guest_index)
                if await doc_input.count() > 0:
                    await doc_input.clear()
                    await doc_input.fill(doc_number)
                    filled += 1
                    logger.debug(f"Guest {guest_index+1}: Filled document number: {doc_number}")
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not fill document number: {e}")
        
        # 6. Fill MJESTO PREBIVALIŠTA (Place of Residence)
        residence = guest_data.get('placeOfResidence', '')
        if residence:
            try:
                res_input = res_inputs.nth(guest_index)
                if await res_input.count() > 0:
                    await res_input.clear()
                    await res_input.fill(residence.upper())
                    filled += 1
                    logger.debug(f"Guest {guest_index+1}: Filled residence: {residence}")
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not fill residence: {e}")
        
        # 7. Select DRŽAVA ROĐENJA (Country of Birth) - use nth(guest_index) directly
        # Country inputs keep their placeholder even after selection, so nth() works
        nationality = guest_data.get('nationality', '')
        if nationality:
            try:
                country_inputs = page.locator('input[placeholder="-- odaberite državu rođenja --"]')
                country_input = country_inputs.nth(guest_index)
                
                if await country_input.count() > 0 and await country_input.is_visible():
                    logger.debug(f"Guest {guest_index+1}: Using country input at index {guest_index}")
                    await country_input.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    await country_input.click()
                    await page.wait_for_timeout(300)
                    
                    search_term, display_name = self._get_country_search_term(nationality)
                    await country_input.fill(search_term)
                    await page.wait_for_timeout(800)
                    
                    # Click first visible matching option
                    options = page.get_by_text(display_name)
                    for j in range(await options.count()):
                        opt = options.nth(j)
                        if await opt.is_visible():
                            await opt.click()
                            filled += 1
                            logger.debug(f"Guest {guest_index+1}: Selected country: {display_name}")
                            break
                else:
                    logger.warning(f"Guest {guest_index+1}: Country input at index {guest_index} not found or not visible")
            except Exception as e:
                logger.warning(f"Guest {guest_index+1}: Could not select country: {e}")
        
        logger.info(f"Guest {guest_index+1}: Filled {filled} fields")
        return filled
    
    async def _click_submit(self, page: Page) -> bool:
        """Click the submit button after all guests are filled"""
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            
            submit_button = page.get_by_text("Spremi goste", exact=True)
            
            if await submit_button.count() > 0:
                await submit_button.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                await submit_button.click(force=True)
                logger.info("Clicked Spremi goste button")
                await page.wait_for_timeout(2000)
                
                error_msg = page.locator('text="Obrazac ima pogreške, molimo provjerite obrazac."')
                if await error_msg.count() > 0:
                    logger.warning("Form validation error - some fields may be invalid")
                    return False
                else:
                    logger.info("Form submitted successfully!")
                    return True
            else:
                logger.warning("Spremi goste button not found")
                return False
        except Exception as e:
            logger.warning(f"Could not click submit button: {e}")
            return False
    
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
