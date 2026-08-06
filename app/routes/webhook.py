"""Stripe webhook handler — POST /api/webhooks/stripe.
Direct port of src/routes/webhook.rs from the Rust project.

Receives Stripe events, verifies HMAC-SHA256 signature, and handles:
  - checkout.session.completed
  - customer.subscription.created
  - customer.subscription.updated
  - customer.subscription.deleted
"""
import os
import json
import hmac
import hashlib
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db.pool import get_pool
from app.services.email import send_magic_link_email, send_trial_notification_email

router = APIRouter(prefix="")


# ── Signature verification ────────────────────────────────────────────────────

def verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify Stripe webhook signature using HMAC-SHA256.

    Matches the Rust `verify_stripe_signature` exactly. If no secret is
    configured (development), verification is skipped.
    """
    if not secret or not sig_header:
        # In development, skip verification if no secret configured
        return True

    parts: dict[str, str] = {}
    for part in sig_header.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            parts[k] = v

    timestamp = parts.get("t")
    v1_sig = parts.get("v1")
    if timestamp is None or v1_sig is None:
        return False

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()

    # constant-time compare
    return hmac.compare_digest(v1_sig, expected)


# ── SubInfo ───────────────────────────────────────────────────────────────────

class SubInfo:
    """Subscription info fetched from Stripe (used during provisioning)."""
    def __init__(
        self,
        item_id: str | None = None,
        status: str | None = None,
        current_period_end: datetime | None = None,
        trial_end: datetime | None = None,
    ):
        self.item_id = item_id
        self.status = status
        self.current_period_end = current_period_end
        self.trial_end = trial_end


def extract_sub_info(sub: dict) -> SubInfo:
    """Extract SubInfo from a Stripe subscription JSON object."""
    item_id = None
    try:
        item_id = sub["items"]["data"][0]["id"]
    except (KeyError, IndexError, TypeError):
        pass

    status = sub.get("status")

    def _ts_to_dt(ts) -> datetime | None:
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    current_period_end = _ts_to_dt(sub.get("current_period_end"))
    trial_end = _ts_to_dt(sub.get("trial_end"))

    return SubInfo(item_id, status, current_period_end, trial_end)


async def fetch_sub_info(stripe_key: str, subscription_id: str) -> SubInfo | None:
    """Fetch subscription info (item ID, status, period end, trial end) from Stripe."""
    url = f"https://api.stripe.com/v1/subscriptions/{subscription_id}?expand[]=items"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {stripe_key}"},
            )
    except Exception as e:
        print(f"Failed to fetch subscription {subscription_id}: {e}")
        return None

    if resp.status_code >= 400:
        print(
            f"Failed to fetch subscription {subscription_id}: status {resp.status_code}"
        )
        return None

    try:
        sub = resp.json()
    except Exception:
        return None

    return extract_sub_info(sub)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """SQL-escape single quotes (matches Rust `esc` closure for simple_query)."""
    return s.replace("'", "''")


def _dt_to_sql(ts: datetime) -> str:
    """Format a datetime as the Rust chrono format %Y-%m-%dT%H:%M:%SZ (UTC)."""
    # chrono formats in UTC; ensure we render UTC
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(obj: dict, *path, default=None):
    """Safe nested-dict access mirroring Rust serde_json::Value indexing."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int):
            if 0 <= key < len(cur):
                cur = cur[key]
            else:
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


def _get_str(obj: dict, *path, default="") -> str:
    v = _get(obj, *path)
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return default


def _get_int(obj: dict, *path, default=None):
    v = _get(obj, *path)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


# ── POST /stripe — Stripe webhook handler ─────────────────────────────────────

@router.post("/api/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    body = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    if not verify_stripe_signature(body, sig_header, webhook_secret):
        return JSONResponse(
            status_code=400, content={"error": "Invalid signature"}
        )

    try:
        event = json.loads(body)
    except (ValueError, TypeError):
        return JSONResponse(
            status_code=400, content={"error": "Invalid JSON"}
        )

    event_type = event.get("type", "") or ""

    print(f"Stripe webhook received: {event_type}")

    pool = await get_pool()

    if event_type == "checkout.session.completed":
        try:
            await handle_checkout_complete(pool, event["data"]["object"])
        except Exception as e:
            print(f"handleCheckoutComplete error: {e}")
    elif event_type == "customer.subscription.created":
        try:
            await handle_subscription_created(pool, event["data"]["object"])
        except Exception as e:
            print(f"handleSubscriptionCreated error: {e}")
    elif event_type == "customer.subscription.updated":
        try:
            await handle_subscription_updated(pool, event["data"]["object"])
        except Exception as e:
            print(f"handleSubscriptionUpdated error: {e}")
    elif event_type == "customer.subscription.deleted":
        try:
            await handle_subscription_deleted(pool, event["data"]["object"])
        except Exception as e:
            print(f"handleSubscriptionDeleted error: {e}")

    return JSONResponse(status_code=200, content={"received": True})


# ── Handlers ───────────────────────────────────────────────────────────────────

async def handle_checkout_complete(pool, session: dict) -> None:
    # CRITICAL: Verify this checkout is for Senzii's product, not another product
    # on the same Stripe account (e.g. FlipFind). Without this check, a FlipFind
    # subscription could provision a Senzii org.
    product = _get_str(session, "metadata", "product")
    if product != "senzii":
        print(
            f"Checkout complete: ignoring non-Senzii product (metadata.product={product})"
        )
        return

    stripe_customer_id = _get_str(session, "customer")
    stripe_subscription_id = _get_str(session, "subscription")
    email = (
        _get_str(session, "customer_email")
        or _get_str(session, "metadata", "email")
    ).lower().strip()
    seats = _get_int(session, "metadata", "seats", default=1)
    org_name = _get_str(session, "metadata", "orgName", default="My Organization")
    name = _get_str(session, "metadata", "name")
    phone = _get_str(session, "metadata", "phone")

    if not stripe_customer_id or not email:
        print("Checkout complete: missing customer or email — skipping")
        return

    # Fetch subscription info (item ID, status, period end, trial end) from Stripe
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    sub_info = None
    if stripe_subscription_id and stripe_key:
        sub_info = await fetch_sub_info(stripe_key, stripe_subscription_id)

    await provision_org(
        pool,
        stripe_customer_id,
        stripe_subscription_id,
        email,
        seats,
        org_name,
        sub_info,
        name,
        phone,
    )


async def handle_subscription_created(pool, sub: dict) -> None:
    # Guard: only process Senzii subscriptions (shared Stripe account)
    product = _get_str(sub, "metadata", "product")
    if product != "senzii":
        print(
            f"Subscription created: ignoring non-Senzii product (metadata.product={product})"
        )
        return

    stripe_customer_id = _get_str(sub, "customer")
    stripe_subscription_id = _get_str(sub, "id")
    email = _get_str(sub, "metadata", "email").lower().strip()

    seats = _get_int(sub, "metadata", "seats")
    if seats is None:
        seats = _get_int(sub, "items", "data", 0, "quantity", default=1)
    if seats is None:
        seats = 1

    org_name = _get_str(sub, "metadata", "orgName", default="My Organization")
    name = _get_str(sub, "metadata", "name")
    phone = _get_str(sub, "metadata", "phone")

    if not stripe_customer_id or not email:
        print("Subscription created: no email in metadata — skipping")
        return

    sub_info = extract_sub_info(sub)

    await provision_org(
        pool,
        stripe_customer_id,
        stripe_subscription_id,
        email,
        seats,
        org_name,
        sub_info,
        name,
        phone,
    )


async def handle_subscription_updated(pool, sub: dict) -> None:
    stripe_customer_id = _get_str(sub, "customer")
    stripe_subscription_id = _get_str(sub, "id")
    status = _get_str(sub, "status")
    quantity = _get_int(sub, "items", "data", 0, "quantity", default=0)
    current_period_end = _get_int(sub, "current_period_end")
    trial_end = _get_int(sub, "trial_end")
    item_id = _get(sub, "items", "data", 0, "id")  # may be None

    async with pool.acquire() as conn:
        # Find org by stripe_customer_id
        row = await conn.fetchrow(
            "SELECT id FROM organizations WHERE stripe_customer_id = $1",
            stripe_customer_id,
        )

        org_id = None
        if row:
            org_id = row["id"]
        else:
            # Maybe it's by subscription ID
            row2 = await conn.fetchrow(
                "SELECT id FROM organizations WHERE stripe_subscription_id = $1",
                stripe_subscription_id,
            )
            if row2:
                org_id = row2["id"]

        if org_id is None:
            print(
                f"Subscription updated: org not found for customer {stripe_customer_id}"
            )
            return

        # Update seats, status, period end, trial end, and subscription item ID
        period_end_ts = None
        if current_period_end is not None:
            try:
                period_end_ts = datetime.fromtimestamp(
                    int(current_period_end), tz=timezone.utc
                )
            except (ValueError, TypeError, OSError):
                period_end_ts = None

        trial_end_ts = None
        if trial_end is not None:
            try:
                trial_end_ts = datetime.fromtimestamp(
                    int(trial_end), tz=timezone.utc
                )
            except (ValueError, TypeError, OSError):
                trial_end_ts = None

        # Use simple SQL string for Neon pooler compatibility (Option params)
        sql = (
            f"UPDATE organizations SET seats = {quantity}, "
            f"subscription_status = '{_esc(status)}'"
        )
        if period_end_ts is not None:
            sql += f", current_period_end = '{_dt_to_sql(period_end_ts)}'"
        if trial_end_ts is not None:
            sql += f", trial_end = '{_dt_to_sql(trial_end_ts)}'"
        if item_id is not None:
            sql += f", stripe_subscription_item_id = '{_esc(item_id)}'"
        sql += f" WHERE id = {org_id}"

        await conn.execute(sql)

    print(
        f"Subscription updated: org {org_id} → {quantity} seats, status={status}"
    )


async def handle_subscription_deleted(pool, sub: dict) -> None:
    stripe_customer_id = _get_str(sub, "customer")
    stripe_subscription_id = _get_str(sub, "id")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM organizations WHERE stripe_customer_id = $1",
            stripe_customer_id,
        )

        org_id = None
        if row:
            org_id = row["id"]
        else:
            row2 = await conn.fetchrow(
                "SELECT id FROM organizations WHERE stripe_subscription_id = $1",
                stripe_subscription_id,
            )
            if row2:
                org_id = row2["id"]

        if org_id is None:
            return

        await conn.execute(
            "UPDATE organizations SET seats = 0, subscription_status = 'canceled', "
            "stripe_subscription_id = NULL, stripe_subscription_item_id = NULL "
            f"WHERE id = {org_id}"
        )

    print(
        f"Subscription deleted: org {org_id} → seats set to 0, canceled"
    )


# ── Org provisioning ───────────────────────────────────────────────────────────

async def provision_org(
    pool,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    email: str,
    seats: int,
    org_name: str,
    sub_info: SubInfo | None,
    name: str,
    phone: str,
) -> None:
    # Rust uses a single pooled client across all queries + create_admin_user;
    # we hold one asyncpg connection for the same scope.
    async with pool.acquire() as conn:
        # Check if org already exists for this Stripe customer
        existing = await conn.fetch(
            "SELECT id FROM organizations WHERE stripe_customer_id = $1",
            stripe_customer_id,
        )

        if existing:
            # Org exists — just update seats + subscription fields
            status = "active"
            if sub_info and sub_info.status:
                status = sub_info.status

            sql = (
                f"UPDATE organizations SET seats = {seats}, "
                f"stripe_subscription_id = '{_esc(stripe_subscription_id)}', "
                f"subscription_status = '{_esc(status)}'"
            )
            if sub_info:
                if sub_info.item_id:
                    sql += f", stripe_subscription_item_id = '{_esc(sub_info.item_id)}'"
                if sub_info.current_period_end:
                    sql += f", current_period_end = '{_dt_to_sql(sub_info.current_period_end)}'"
                if sub_info.trial_end:
                    sql += f", trial_end = '{_dt_to_sql(sub_info.trial_end)}'"
            org_id = existing[0]["id"]
            sql += f" WHERE id = {org_id}"
            await conn.execute(sql)
            print(f"Org {org_id} already exists — updated seats to {seats}")
            return

        # Check if org already exists by name (e.g. created manually before checkout)
        existing_by_name = await conn.fetch(
            "SELECT id FROM organizations WHERE LOWER(name) = LOWER($1)",
            org_name,
        )

        if existing_by_name:
            # Org exists by name but has no stripe_customer_id — link it
            org_id = existing_by_name[0]["id"]
            status = "active"
            if sub_info and sub_info.status:
                status = sub_info.status

            sql = (
                f"UPDATE organizations SET stripe_customer_id = '{_esc(stripe_customer_id)}', "
                f"stripe_subscription_id = '{_esc(stripe_subscription_id)}', "
                f"subscription_status = '{_esc(status)}', seats = {seats}"
            )
            if sub_info:
                if sub_info.item_id:
                    sql += f", stripe_subscription_item_id = '{_esc(sub_info.item_id)}'"
                if sub_info.current_period_end:
                    sql += f", current_period_end = '{_dt_to_sql(sub_info.current_period_end)}'"
                if sub_info.trial_end:
                    sql += f", trial_end = '{_dt_to_sql(sub_info.trial_end)}'"
            sql += f" WHERE id = {org_id}"
            await conn.execute(sql)
            print(
                f"Org {org_id} found by name — linked to Stripe customer {stripe_customer_id}"
            )

            # Still need to create the admin user if it doesn't exist
            existing_user = await conn.fetch(
                "SELECT id FROM users WHERE email = $1", email
            )
            if existing_user:
                print("User already exists — skipping")
                return

            # Create admin user for existing org
            await create_admin_user(
                conn, email, org_id, name, phone, seats, org_name,
                stripe_subscription_id, sub_info,
            )
            return

        # Check if user already exists
        existing_user = await conn.fetch(
            "SELECT id FROM users WHERE email = $1", email
        )
        if existing_user:
            print("User already exists — skipping")
            return

        # Create org — use simple SQL (Neon pooler doesn't reliably return rows from RETURNING)
        org_insert = (
            f"INSERT INTO organizations (name, stripe_customer_id) "
            f"VALUES ('{_esc(org_name)}', '{_esc(stripe_customer_id)}')"
        )
        await conn.execute(org_insert)

        # Fetch org by stripe_customer_id
        org_row = await conn.fetchrow(
            "SELECT id FROM organizations WHERE stripe_customer_id = $1",
            stripe_customer_id,
        )
        org_id = org_row["id"]

        # Create admin user (same connection, matching the Rust flow)
        await create_admin_user(
            conn, email, org_id, name, phone, seats, org_name,
            stripe_subscription_id, sub_info,
        )


async def create_admin_user(
    conn,
    email: str,
    org_id: int,
    name: str,
    phone: str,
    seats: int,
    org_name: str,
    stripe_subscription_id: str,
    sub_info: SubInfo | None,
) -> None:
    """Create admin user for an org, generate magic link, send email."""
    # Create admin user — no password yet (set via magic link → set-password flow)
    user_name = email if not name else name

    # Use simple SQL for INSERT (Neon pooler bug with Option<String> params)
    user_insert = (
        f"INSERT INTO users (email, role, organization_id, name, phone) "
        f"VALUES ('{_esc(email)}', 'admin', {org_id}, '{_esc(user_name)}', '{_esc(phone)}')"
    )
    await conn.execute(user_insert)

    # Fetch user by email
    user_row = await conn.fetchrow(
        "SELECT id, email FROM users WHERE email = $1", email
    )
    user_id = user_row["id"]
    user_email = user_row["email"]

    # Update org owner
    await conn.execute(
        f"UPDATE organizations SET owner_user_id = {user_id} WHERE id = {org_id}"
    )

    # Update Stripe subscription fields + seats + subscription item ID + period/trial dates
    status = "active"
    if sub_info and sub_info.status:
        status = sub_info.status

    sql = (
        f"UPDATE organizations SET stripe_subscription_id = '{_esc(stripe_subscription_id)}', "
        f"subscription_status = '{_esc(status)}', seats = {seats}"
    )
    if sub_info:
        if sub_info.item_id:
            sql += f", stripe_subscription_item_id = '{_esc(sub_info.item_id)}'"
        if sub_info.current_period_end:
            sql += f", current_period_end = '{_dt_to_sql(sub_info.current_period_end)}'"
        if sub_info.trial_end:
            sql += f", trial_end = '{_dt_to_sql(sub_info.trial_end)}'"
    sql += f" WHERE id = {org_id}"
    await conn.execute(sql)

    # Generate magic token for admin onboarding
    token = str(uuid.uuid4())
    token_insert = (
        f"INSERT INTO admin_magic_tokens (user_id, organization_id, token, expires_at) "
        f"VALUES ({user_id}, {org_id}, '{_esc(token)}', NOW() + INTERVAL '7 days')"
    )
    await conn.execute(token_insert)

    base_url = os.getenv("BASE_URL", "https://senzii.com")
    magic_link = f"{base_url}/auth/admin-login?token={token}"

    print(f"Provisioning complete: org {org_id}, magic link sent")

    # Send magic link email via Resend
    email_result = await send_magic_link_email(
        to=email,
        magic_link=magic_link,
        org_name=org_name,
    )
    if not email_result.get("sent"):
        print(f"[email] Failed to send magic link: {email_result.get('reason')}")

    # ── Send trial registration notification to Chris (NOTIFY_EMAIL) ──────────
    notify_result = await send_trial_notification_email(
        org_name=org_name,
        admin_email=email,
        admin_name=name,
        admin_phone=phone,
        seats=seats,
    )
    if not notify_result.get("sent"):
        print(f"[email] Trial notification not sent: {notify_result.get('reason')}")