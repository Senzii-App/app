# Senzii Scheduling App & MCP Server

## Stack

- **Web framework**: FastAPI
- **Database**: asyncpg (PostgreSQL, same schema as Rust)
- **Sessions**: Cookie-based, PostgreSQL-backed (same `sessions` table)
- **Email**: Resend API
- **Geocoding**: Nominatim (OpenStreetMap)
- **MCP Server**: Python MCP SDK

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in DATABASE_URL, SESSION_SECRET, etc.
uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload
```

MCP server:
```bash
python -m mcp_server.main
```

## Architecture

```
app/
  main.py          — FastAPI app, all routers wired
  config.py        — Environment configuration
  db/              — Database layer (asyncpg pool, migrations, queries)
  models/          — Pydantic schemas
  middleware/      — Auth session extraction, rate limiting
  routes/          — FastAPI routers (1:1 with Rust routes)
  services/        — Email (Resend), Geocoding (Nominatim)
mcp_server/       — MCP server (Python MCP SDK)
site/             — HTML/CSS/JS static site (moved from app/static)
```

## License

Open-source. See LICENSE.