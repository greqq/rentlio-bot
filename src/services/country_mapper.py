"""
Country Code Mapper for Rentlio API

Maps OCR-extracted country names/codes to Rentlio country IDs.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Common country name variations mapped to Rentlio-style names
# This will be matched against the Rentlio countries list
COUNTRY_ALIASES = {
    # Croatian variations
    'HRV': 'Croatia',
    'CRO': 'Croatia',
    'CROATIA': 'Croatia',
    'HRVATSKA': 'Croatia',
    'REPUBLIC OF CROATIA': 'Croatia',
    'HR': 'Croatia',
    
    # German variations
    'DEU': 'Germany',
    'GER': 'Germany',
    'GERMANY': 'Germany',
    'DEUTSCHLAND': 'Germany',
    'NJEMAČKA': 'Germany',
    'DE': 'Germany',
    'D': 'Germany',
    
    # Austrian variations
    'AUT': 'Austria',
    'AUSTRIA': 'Austria',
    'ÖSTERREICH': 'Austria',
    'AUSTRIJA': 'Austria',
    'AT': 'Austria',
    'A': 'Austria',
    
    # Italian variations
    'ITA': 'Italy',
    'ITALY': 'Italy',
    'ITALIA': 'Italy',
    'ITALIJA': 'Italy',
    'IT': 'Italy',
    'I': 'Italy',
    
    # Slovenian variations
    'SVN': 'Slovenia',
    'SLO': 'Slovenia',
    'SLOVENIA': 'Slovenia',
    'SLOVENIJA': 'Slovenia',
    'SI': 'Slovenia',
    
    # Serbian variations
    'SRB': 'Serbia',
    'SERBIA': 'Serbia',
    'SRBIJA': 'Serbia',
    'RS': 'Serbia',
    
    # Bosnian variations
    'BIH': 'Bosnia and Herzegovina',
    'BOSNIA': 'Bosnia and Herzegovina',
    'BOSNIA AND HERZEGOVINA': 'Bosnia and Herzegovina',
    'BOSNA I HERCEGOVINA': 'Bosnia and Herzegovina',
    'BA': 'Bosnia and Herzegovina',
    
    # Hungarian variations
    'HUN': 'Hungary',
    'HUNGARY': 'Hungary',
    'MAGYARORSZÁG': 'Hungary',
    'MAĐARSKA': 'Hungary',
    'HU': 'Hungary',
    
    # Czech variations
    'CZE': 'Czech Republic',
    'CZECH': 'Czech Republic',
    'CZECH REPUBLIC': 'Czech Republic',
    'CZECHIA': 'Czech Republic',
    'ČESKÁ REPUBLIKA': 'Czech Republic',
    'ČEŠKA': 'Czech Republic',
    'CZ': 'Czech Republic',
    
    # Polish variations
    'POL': 'Poland',
    'POLAND': 'Poland',
    'POLSKA': 'Poland',
    'POLJSKA': 'Poland',
    'PL': 'Poland',
    
    # Slovak variations
    'SVK': 'Slovakia',
    'SLOVAKIA': 'Slovakia',
    'SLOVENSKO': 'Slovakia',
    'SLOVAČKA': 'Slovakia',
    'SK': 'Slovakia',
    
    # UK variations
    'GBR': 'United Kingdom',
    'UK': 'United Kingdom',
    'GB': 'United Kingdom',
    'GREAT BRITAIN': 'United Kingdom',
    'UNITED KINGDOM': 'United Kingdom',
    'ENGLAND': 'United Kingdom',
    'VELIKA BRITANIJA': 'United Kingdom',
    'UJEDINJENO KRALJEVSTVO': 'United Kingdom',
    
    # French variations
    'FRA': 'France',
    'FRANCE': 'France',
    'FRANCUSKA': 'France',
    'FR': 'France',
    'F': 'France',
    
    # Dutch variations
    'NLD': 'Netherlands',
    'NL': 'Netherlands',
    'NETHERLANDS': 'Netherlands',
    'HOLLAND': 'Netherlands',
    'NIEDERLANDE': 'Netherlands',
    'NIZOZEMSKA': 'Netherlands',
    
    # Belgian variations
    'BEL': 'Belgium',
    'BELGIUM': 'Belgium',
    'BELGIQUE': 'Belgium',
    'BELGIJA': 'Belgium',
    'BE': 'Belgium',
    
    # Swiss variations
    'CHE': 'Switzerland',
    'SWITZERLAND': 'Switzerland',
    'SCHWEIZ': 'Switzerland',
    'SUISSE': 'Switzerland',
    'SVIZZERA': 'Switzerland',
    'ŠVICARSKA': 'Switzerland',
    'CH': 'Switzerland',
    
    # Spanish variations
    'ESP': 'Spain',
    'SPAIN': 'Spain',
    'ESPAÑA': 'Spain',
    'ŠPANJOLSKA': 'Spain',
    'ES': 'Spain',
    
    # Portuguese variations
    'PRT': 'Portugal',
    'PORTUGAL': 'Portugal',
    'PT': 'Portugal',
    
    # Romanian variations
    'ROU': 'Romania',
    'ROMANIA': 'Romania',
    'RUMUNJSKA': 'Romania',
    'RO': 'Romania',
    
    # Bulgarian variations
    'BGR': 'Bulgaria',
    'BULGARIA': 'Bulgaria',
    'BUGARSKA': 'Bulgaria',
    'BG': 'Bulgaria',
    
    # Greek variations
    'GRC': 'Greece',
    'GREECE': 'Greece',
    'ΕΛΛΆΔΑ': 'Greece',
    'GRČKA': 'Greece',
    'GR': 'Greece',
    
    # US variations
    'USA': 'United States',
    'US': 'United States',
    'UNITED STATES': 'United States',
    'UNITED STATES OF AMERICA': 'United States',
    'AMERIKA': 'United States',
    'SAD': 'United States',
    
    # Canadian variations
    'CAN': 'Canada',
    'CANADA': 'Canada',
    'KANADA': 'Canada',
    'CA': 'Canada',
    
    # Australian variations
    'AUS': 'Australia',
    'AUSTRALIA': 'Australia',
    'AUSTRALIJA': 'Australia',
    'AU': 'Australia',
    
    # Russian variations
    'RUS': 'Russia',
    'RUSSIA': 'Russia',
    'RUSSIAN FEDERATION': 'Russia',
    'RUSIJA': 'Russia',
    'RU': 'Russia',
    
    # Ukrainian variations
    'UKR': 'Ukraine',
    'UKRAINE': 'Ukraine',
    'UKRAJINA': 'Ukraine',
    'UA': 'Ukraine',
    
    # Turkish variations
    'TUR': 'Turkey',
    'TURKEY': 'Turkey',
    'TÜRKIYE': 'Turkey',
    'TURSKA': 'Turkey',
    'TR': 'Turkey',
    
    # Montenegrin variations
    'MNE': 'Montenegro',
    'MONTENEGRO': 'Montenegro',
    'CRNA GORA': 'Montenegro',
    'ME': 'Montenegro',
    
    # North Macedonian variations
    'MKD': 'North Macedonia',
    'NORTH MACEDONIA': 'North Macedonia',
    'MACEDONIA': 'North Macedonia',
    'MAKEDONIJA': 'North Macedonia',
    'SJEVERNA MAKEDONIJA': 'North Macedonia',
    'MK': 'North Macedonia',
    
    # Albanian variations
    'ALB': 'Albania',
    'ALBANIA': 'Albania',
    'ALBANIJA': 'Albania',
    'AL': 'Albania',
    
    # Kosovo variations
    'XKX': 'Kosovo',
    'KOSOVO': 'Kosovo',
    'XK': 'Kosovo',
    
    # Irish variations
    'IRL': 'Ireland',
    'IRELAND': 'Ireland',
    'IRSKA': 'Ireland',
    'IE': 'Ireland',
    
    # Danish variations
    'DNK': 'Denmark',
    'DENMARK': 'Denmark',
    'DANMARK': 'Denmark',
    'DANSKA': 'Denmark',
    'DK': 'Denmark',
    
    # Swedish variations
    'SWE': 'Sweden',
    'SWEDEN': 'Sweden',
    'SVERIGE': 'Sweden',
    'ŠVEDSKA': 'Sweden',
    'SE': 'Sweden',
    
    # Norwegian variations
    'NOR': 'Norway',
    'NORWAY': 'Norway',
    'NORGE': 'Norway',
    'NORVEŠKA': 'Norway',
    'NO': 'Norway',
    
    # Finnish variations
    'FIN': 'Finland',
    'FINLAND': 'Finland',
    'SUOMI': 'Finland',
    'FINSKA': 'Finland',
    'FI': 'Finland',
}


class CountryMapper:
    """Maps country names/codes to Rentlio country IDs"""
    
    def __init__(self):
        self._countries: dict[str, int] = {}  # name -> id
        self._loaded = False
    
    async def load_countries(self, api) -> None:
        """Load countries from Rentlio API"""
        if self._loaded:
            return
        
        try:
            countries = await api.get_countries()
            for country in countries:
                name = country.get('name', '').strip()
                country_id = country.get('id')
                if name and country_id:
                    # Store with normalized name (uppercase for matching)
                    self._countries[name.upper()] = country_id
                    # Also store original for exact matches
                    self._countries[name] = country_id
            
            self._loaded = True
            logger.info(f"Loaded {len(countries)} countries from Rentlio API")
        except Exception as e:
            logger.error(f"Failed to load countries: {e}")
    
    def get_country_id(self, country_input: str) -> Optional[int]:
        """
        Get Rentlio country ID from country name or code
        
        Args:
            country_input: Country name, code (e.g., 'HRV', 'Croatia', 'Hrvatska')
        
        Returns:
            Country ID or None if not found
        """
        if not country_input:
            return None
        
        # Normalize input
        normalized = country_input.strip().upper()
        
        # First check aliases
        if normalized in COUNTRY_ALIASES:
            standard_name = COUNTRY_ALIASES[normalized]
            # Look for the standard name in loaded countries
            if standard_name.upper() in self._countries:
                return self._countries[standard_name.upper()]
            if standard_name in self._countries:
                return self._countries[standard_name]
        
        # Direct lookup (normalized)
        if normalized in self._countries:
            return self._countries[normalized]
        
        # Try original input
        if country_input in self._countries:
            return self._countries[country_input]
        
        # Fuzzy match - check if input is contained in any country name
        for name, country_id in self._countries.items():
            if normalized in name.upper() or name.upper() in normalized:
                return country_id
        
        logger.warning(f"Country not found: {country_input}")
        return None
    
    def get_all_countries(self) -> dict[str, int]:
        """Get all loaded countries"""
        return self._countries.copy()


# Singleton instance
country_mapper = CountryMapper()
