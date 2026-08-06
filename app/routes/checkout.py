"""Stripe Checkout Session creation — POST /api/checkout.
Direct port of src/routes/checkout.rs from the Rust project.

Creates a Stripe Checkout Session for staff-member billing.
Price: $5/staff member/month, 30-day free trial.
"""
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db.pool import get_pool

router = APIRouter(prefix="")


# ── Constants ─────────────────────────────────────────────────────────────────

PRICE_PER_SEAT_CENTS = 500  # $5.00
TRIAL_DAYS = 30


# ── Request model ──────────────────────────────────────────────────────────────

class CheckoutPayload(BaseModel):
    orgName: str
    email: str
    seats: int | None = None  # number of staff members to start with
    name: str | None = None
    phone: str | None = None


# ── POST /checkout — create a Stripe Checkout Session ─────────────────────────

@router.post("/api/checkout")
async def post_checkout(payload: CheckoutPayload):
    pool = await get_pool()  # ensures pool exists (matches Rust State<_pool>)

    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Coming Soon", "reason": "stripe_unconfigured"},
        )

    price_id = os.getenv("STRIPE_PRICE_ID", "")
    if not price_id:
        return JSONResponse(
            status_code=503,
            content={"error": "Coming Soon", "reason": "stripe_price_not_configured"},
        )

    if not payload.orgName or not payload.email:
        return JSONResponse(
            status_code=400,
            content={"error": "org_name and email are required."},
        )

    seats = payload.seats if payload.seats is not None else 1
    if seats < 1:
        return JSONResponse(
            status_code=400,
            content={"error": "seats must be at least 1."},
        )

    clean_email = payload.email.strip().lower()
    base_url = os.getenv("BASE_URL", "https://senzii.com")
    name = (payload.name or "").strip()
    phone = (payload.phone or "").strip()

    params = {
        "mode": "subscription",
        "payment_method_types[0]": "card",
        "customer_email": clean_email,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": str(seats),
        "subscription_data[trial_period_days]": str(TRIAL_DAYS),
        "metadata[product]": "senzii",
        "metadata[orgName]": payload.orgName,
        "metadata[email]": clean_email,
        "metadata[seats]": str(seats),
        "metadata[name]": name,
        "metadata[phone]": phone,
        "success_url": f"{base_url}/auth/onboarding?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/#pricing",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers={"Authorization": f"Bearer {stripe_key}"},
                data=params,
            )
    except Exception as e:
        print(f"Stripe checkout session error: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to create checkout session."},
        )

    if 200 <= resp.status_code < 300:
        try:
            session_data = resp.json()
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"error": "Failed to parse Stripe response."},
            )
        url = session_data.get("url")
        if url:
            return JSONResponse(status_code=200, content={"url": url})
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to create checkout session."},
        )

    # Stripe returned an error status
    body = resp.text
    print(f"Stripe API error: {resp.status_code} {body}")
    return JSONResponse(
        status_code=500,
        content={"error": "Failed to create checkout session."},
    )