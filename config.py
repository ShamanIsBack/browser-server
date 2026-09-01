import os

from dotenv import load_dotenv

load_dotenv()

API_KEY: str = os.environ.get("API_KEY", "")
HOST: str = os.environ.get("HOST", "127.0.0.1")
PORT: int = int(os.environ.get("PORT", "8765"))
# Boolean flags use a uniform positive-inclusion pattern.
HEADLESS: bool = os.environ.get("HEADLESS", "true").lower() in ("true", "1", "yes")
DEBUG: bool = os.environ.get("DEBUG", "false").lower() in ("true", "1", "yes")
