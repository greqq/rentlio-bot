"""Configuration management"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Application configuration"""
    
    # Rentlio API
    RENTLIO_API_KEY: str = os.getenv("RENTLIO_API_KEY", "")
    RENTLIO_API_URL: str = os.getenv("RENTLIO_API_URL", "https://api.rentl.io/v1")
    
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_ALLOWED_USERS: list[int] = [
        int(uid.strip()) 
        for uid in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") 
        if uid.strip()
    ]
    
    # Google Cloud
    GOOGLE_APPLICATION_CREDENTIALS: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    
    # Paths
    BASE_DIR: Path = Path(__file__).parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    TEMP_DIR: Path = BASE_DIR / "temp"

    # Self check-in form
    # The form runs in the same process as the bot, behind the Cloudflare
    # tunnel. WEB_ENABLED off leaves the bot behaving exactly as before.
    WEB_ENABLED: bool = os.getenv("WEB_ENABLED", "true").lower() in ("1", "true", "yes")
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
    # Public origin the check-in links are built from, no trailing slash.
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.sqlite3"))
    CHECKIN_TOKEN_TTL_DAYS: int = int(os.getenv("CHECKIN_TOKEN_TTL_DAYS", "30"))
    # An ID photo off a phone is ~1-5 MB; anything past this is not a document.
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))
    
    @classmethod
    def validate(cls) -> list[str]:
        """Validate required configuration"""
        errors = []
        if not cls.RENTLIO_API_KEY:
            errors.append("RENTLIO_API_KEY is required")
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        return errors

    @classmethod
    def warnings(cls) -> list[str]:
        """Non-fatal gaps worth printing at startup."""
        out = []
        if cls.WEB_ENABLED and not cls.PUBLIC_BASE_URL:
            out.append(
                "PUBLIC_BASE_URL not set - check-in links cannot be built. "
                "Set it to https://sun-apartments.co"
            )
        return out


config = Config()
