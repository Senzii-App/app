"""Config for the Senzii MCP server."""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MCP_PORT = int(os.getenv("MCP_PORT", "3001"))

# SESSION_SECRET signs the OAuth/session JWTs that carry org_id and role.
# A known or guessable value lets anyone forge a Bearer token for any
# organization (and any role, including "super"), so there is no safe default:
# refuse to start unless the operator sets a real one.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET or SESSION_SECRET in ("senzii-oauth-secret", "change-me-to-a-random-string"):
    raise RuntimeError(
        "SESSION_SECRET must be set to a strong, unique value. "
        "It signs auth tokens, so an empty or default value lets anyone forge "
        "a token for any organization. Set SESSION_SECRET in the environment."
    )

BASE_URL = os.getenv("BASE_URL", "https://senzii.com")