"""Config for the Senzii MCP server."""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MCP_PORT = int(os.getenv("MCP_PORT", "3001"))
SESSION_SECRET = os.getenv("SESSION_SECRET", "senzii-oauth-secret")
BASE_URL = os.getenv("BASE_URL", "https://senzii.com")