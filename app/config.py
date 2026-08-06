"""Configuration — reads from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "3000"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-to-a-random-string")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
MAIL_FROM = os.getenv("MAIL_FROM", "Senzii <noreply@senzii.com>")
BASE_URL = os.getenv("BASE_URL", "https://senzii.com")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "")
SESSION_IDLE_TIMEOUT_SECS = int(os.getenv("SESSION_IDLE_TIMEOUT_SECS", "900"))
SESSION_ABSOLUTE_TIMEOUT_SECS = int(os.getenv("SESSION_ABSOLUTE_TIMEOUT_SECS", "28800"))
MCP_PORT = int(os.getenv("MCP_PORT", "3001"))
ORG_ID = int(os.getenv("ORG_ID", "0")) if os.getenv("ORG_ID") else None