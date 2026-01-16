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
    
    @classmethod
    def validate(cls) -> list[str]:
        """Validate required configuration"""
        errors = []
        if not cls.RENTLIO_API_KEY:
            errors.append("RENTLIO_API_KEY is required")
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        return errors


config = Config()
