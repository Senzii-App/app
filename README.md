# Senzii Scheduling App & MCP Server

Staff scheduling your AI agent can operate directly. Senzii is an
open-source (Apache-2.0) scheduling platform: tag-based skills matching,
shift assignment with conflict detection, certification tracking,
proximity ranking, and client staffing requests — exposed as MCP tools
for Claude, ChatGPT, or any MCP-compatible client.

**The MCP server ships inside this repo** and shares the app's PostgreSQL
schema — it is the scheduling engine's agent interface, not a generic
database connector.

## MCP tools

28 tools covering the full scheduling workflow:

| Area | Tools |
|---|---|
| Staff | `list_staff` `get_staff` `create_staff` `delete_staff` `set_staff_availability` `add_staff_certification` |
| Matching | `find_candidates` — rank staff for a shift by tags/certifications, proximity, availability |
| Shifts & assignments | `list_shifts` `create_assignment` `unassign_assignment` `list_assignments` `delete_shift` |
| Clients | `list_clients` `create_client` `send_client_magic_link` `send_staff_magic_link` |
| Staffing requests | `list_staffing_requests` `create_staffing_request` `convert_staffing_request` `cancel_staffing_request` |
| Certifications | `list_certifications` `create_certification` `update_certification` `delete_certification` |
| Work sites | `list_work_sites` `create_work_site` |
| Admin | `get_org_metrics` `run_sql` (admin-only, SELECT-only) |

Every tool is scoped to the authenticated organization — the org ID is
injected server-side into every query, so cross-org data access is
impossible. Assignments are created **pending** by design: the agent
proposes, a human confirms before the schedule goes live. That is a
deliberate safety choice for regulated industries, not a limitation.

## Stack

- **Web framework**: FastAPI
- **Database**: asyncpg (PostgreSQL)
- **Sessions**: Cookie-based, PostgreSQL-backed
- **Email**: Resend API (optional — only for notification features; the app runs fine without a key)
- **Geocoding**: Nominatim (OpenStreetMap) — free public endpoint, no API key
- **MCP Server**: Python MCP SDK (Streamable HTTP)

## Requirements

| Component | Required? | Notes |
|---|---|---|
| PostgreSQL | **Yes** | Any instance works — Neon free tier is fine |
| Geocoding | Free by default | Public OpenStreetMap/Nominatim, no key (set a User-Agent per their usage policy) |
| Email (Resend) | Optional | API key only if you want email notifications |

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # set DATABASE_URL; add RESEND_API_KEY only for email features
uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload
```

MCP server:

```bash
python -m mcp_server.main
```

Tool definitions live in `mcp_server/tools/tools.toml` — add or modify
tools by editing the TOML and their handlers under `mcp_server/tools/`.

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
mcp_server/        — MCP server (Python MCP SDK), tools defined in tools.toml
static/            — App HTML/CSS/JS (login, dashboards, portals, SEO pages)
```

The marketing/landing site is a separate repo (`Senzii-App/site`) served from
Vercel at senzii.com. `app.senzii.com` serves only the app: `GET /` is the
sign-in page.

## Deployment

Two targets, one CLI (see `infra/README.md` for full docs):

```bash
./infra/deploy.sh azure   <flags...>   # deploy to Azure (VMSS / staging VM)
./infra/deploy.sh linode  <flags...>   # deploy to Linode backends
./infra/deploy.sh help                 # list providers & flags
```

## License

Open-source. See LICENSE.