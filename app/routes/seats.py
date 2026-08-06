"""Seats route — staff member billing (seat management) endpoints.

Direct port of src/routes/seats.rs from the Rust/Axum project.
All SQL queries are identical to the Rust version.

Routes (mounted at /api/seats):
  GET  /              — view current reservation, staff count, pricing
  POST /add           — add staff members (increases Stripe subscription quantity)
  POST /remove        — remove staff members (decreases Stripe subscription quantity)
  POST /cancel         — cancel the entire subscription (commented out)
  POST /reactivate    — reactivate a canceled subscription (commented out)
"""
import os
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.middleware.auth import require_auth
from app.db.pool import get_pool

router = APIRouter()

PRICE_PER_SEAT_CENTS = 500  # $5.00


# ── Payloads ──────────────────────────────────────────────────────────────────


class AddSeatsPayload(BaseModel):
    seats: int


class RemoveSeatsPayload(BaseModel):
    seats: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def format_cents(cents: int) -> str:
    """Format cents as '$X.XX'."""
    return f"${cents / 100.0:.2f}"


async def get_org_billing(conn, org_id: int) -> dict | None:
    """Fetch org billing info: seats, staff_count, subscription details.

    Direct port of get_org_billing() in seats.rs.
    """
    row = await conn.fetchrow(
        """SELECT o.seats,
                  o.stripe_subscription_id,
                  o.stripe_subscription_item_id,
                  o.stripe_customer_id,
                  o.subscription_status,
                  o.current_period_end,
                  o.trial_end,
                  o.sales_agent_name,
                  o.sales_agent_email,
                  (SELECT COUNT(*)::int FROM staff WHERE organization_id = $1 AND deleted_at IS NULL) AS staff_count
           FROM organizations o WHERE o.id = $1""",
        org_id,
    )
    if row is None:
        return None
    return {
        "seats": row["seats"],
        "staff_count": row["staff_count"],
        "stripe_subscription_id": row["stripe_subscription_id"],
        "stripe_subscription_item_id": row["stripe_subscription_item_id"],
        "stripe_customer_id": row["stripe_customer_id"],
        "subscription_status": row["subscription_status"],
        "current_period_end": row["current_period_end"],
        "trial_end": row["trial_end"],
        "sales_agent_name": row["sales_agent_name"],
        "sales_agent_email": row["sales_agent_email"],
    }


async def update_stripe_quantity(sub_item_id: str, new_quantity: int) -> None:
    """Call Stripe to update subscription item quantity.

    Raises Exception on failure (mirrors the Rust Result<(), String>).
    """
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        raise Exception("Stripe not configured")

    url = f"https://api.stripe.com/v1/subscription_items/{sub_item_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {stripe_key}"},
            data={"quantity": str(new_quantity)},
        )

    if resp.status_code >= 400:
        print(f"[seats] Stripe update quantity error: {resp.status_code} {resp.text}")
        raise Exception(f"Stripe API error: {resp.status_code}")


# ── Handlers ─────────────────────────────────────────────────────────────────


@router.get("/")
async def get_seats(request: Request):
    """GET / — get current seat reservation info."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        billing = await get_org_billing(conn, org_id)
        if billing is None:
            return JSONResponse(
                {"error": "Failed to fetch billing info"}, status_code=500
            )

        seats = billing["seats"] or 0
        staff_count = billing["staff_count"] or 0
        monthly_cents = seats * PRICE_PER_SEAT_CENTS
        available = seats - staff_count
        is_trial = billing["subscription_status"] == "trialing"

        # During a trial, current_period_end may not be set yet by Stripe webhooks.
        # Fall back to trial_end as the next billing date.
        current_period_end = billing["current_period_end"]
        effective_period_end = (
            billing["trial_end"] if current_period_end is None else current_period_end
        )

        return JSONResponse(
            {
                "seats": seats,
                "staff_count": staff_count,
                "available": available,
                "price_per_seat_cents": PRICE_PER_SEAT_CENTS,
                "price_per_seat_display": format_cents(PRICE_PER_SEAT_CENTS),
                "monthly_total_cents": monthly_cents,
                "monthly_total_display": format_cents(monthly_cents),
                "current_period_end": effective_period_end,
                "trial_end": billing["trial_end"],
                "is_trial": is_trial,
                "subscription_status": billing["subscription_status"],
                "stripe_configured": seats > 0,
                "stripe_customer_id": billing["stripe_customer_id"],
                "sales_agent_name": billing["sales_agent_name"],
                "sales_agent_email": billing["sales_agent_email"],
            }
        )


@router.post("/add")
async def add_seats(payload: AddSeatsPayload, request: Request):
    """POST /add — add staff members (increase reservation)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if payload.seats < 1:
        return JSONResponse(
            {"error": "Must add at least 1 staff member."}, status_code=400
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        billing = await get_org_billing(conn, org_id)
        if billing is None:
            return JSONResponse(
                {"error": "Failed to fetch billing info"}, status_code=500
            )

        current_seats = billing["seats"] or 0
        sub_item_id = billing["stripe_subscription_item_id"] or ""

        # If org has no Stripe subscription (seats=0, pre-Stripe), they can't add seats here
        if current_seats == 0 or not sub_item_id:
            return JSONResponse(
                {
                    "error": "no_subscription",
                    "message": "No active subscription found. Visit senzii.com to start a subscription.",
                },
                status_code=409,
            )

        new_quantity = current_seats + payload.seats

        # Update DB first (Option A: DB is source of truth, rollback if Stripe fails)
        try:
            await conn.execute(
                f"UPDATE organizations SET seats = {new_quantity} WHERE id = {org_id}"
            )
        except Exception as e:
            print(f"[seats] DB update seats error: {e}")
            return JSONResponse(
                {"error": "Failed to update staff member count"}, status_code=500
            )

        # Sync Stripe — if this fails, roll back DB to original value
        try:
            await update_stripe_quantity(sub_item_id, new_quantity)
        except Exception as e:
            print(
                f"[seats] Stripe sync failed after DB update (add_seats): "
                f"org={org_id} seats={new_quantity} err={e}"
            )
            try:
                await conn.execute(
                    f"UPDATE organizations SET seats = {current_seats} WHERE id = {org_id}"
                )
            except Exception as re:
                print(
                    f"[seats] DB rollback failed (add_seats): "
                    f"org={org_id} original_seats={current_seats} err={re}"
                )
            return JSONResponse(
                {
                    "error": "stripe_error",
                    "message": f"Failed to update subscription: {e}",
                },
                status_code=502,
            )

        staff_count = billing["staff_count"] or 0
        monthly_cents = new_quantity * PRICE_PER_SEAT_CENTS

        return JSONResponse(
            {
                "seats": new_quantity,
                "staff_count": staff_count,
                "available": new_quantity - staff_count,
                "price_per_seat_cents": PRICE_PER_SEAT_CENTS,
                "price_per_seat_display": format_cents(PRICE_PER_SEAT_CENTS),
                "monthly_total_cents": monthly_cents,
                "monthly_total_display": format_cents(monthly_cents),
                "message": f"Added {payload.seats} staff member(s). You now have {new_quantity} reserved.",
            }
        )


@router.post("/remove")
async def remove_seats(payload: RemoveSeatsPayload, request: Request):
    """POST /remove — remove staff members (decrease reservation)."""
    try:
        user_id, role, email, org_id = require_auth(request)
    except Exception:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if payload.seats < 1:
        return JSONResponse(
            {"error": "Must remove at least 1 staff member."}, status_code=400
        )

    pool = await get_pool()
    async with pool.acquire() as conn:
        billing = await get_org_billing(conn, org_id)
        if billing is None:
            return JSONResponse(
                {"error": "Failed to fetch billing info"}, status_code=500
            )

        current_seats = billing["seats"] or 0
        staff_count = billing["staff_count"] or 0
        sub_item_id = billing["stripe_subscription_item_id"] or ""

        if current_seats == 0 or not sub_item_id:
            return JSONResponse(
                {"error": "no_subscription", "message": "No active subscription found."},
                status_code=409,
            )

        new_quantity = current_seats - payload.seats

        # Cannot go below current staff count
        if new_quantity < staff_count:
            return JSONResponse(
                {
                    "error": "below_staff_count",
                    "message": (
                        f"Cannot remove {payload.seats} staff member(s). "
                        f"You have {staff_count} staff members in use and {current_seats} reserved. "
                        f"Remove staff members first or reduce the amount."
                    ),
                },
                status_code=409,
            )

        if new_quantity < 0:
            return JSONResponse(
                {"error": "Cannot remove more staff members than you have reserved."},
                status_code=400,
            )

        # Update DB first (Option A: DB is source of truth, rollback if Stripe fails)
        try:
            await conn.execute(
                f"UPDATE organizations SET seats = {new_quantity} WHERE id = {org_id}"
            )
        except Exception as e:
            print(f"[seats] DB update seats error: {e}")
            return JSONResponse(
                {"error": "Failed to update staff member count"}, status_code=500
            )

        # Sync Stripe — if this fails, roll back DB to original value
        try:
            await update_stripe_quantity(sub_item_id, new_quantity)
        except Exception as e:
            print(
                f"[seats] Stripe sync failed after DB update (remove_seats): "
                f"org={org_id} seats={new_quantity} err={e}"
            )
            try:
                await conn.execute(
                    f"UPDATE organizations SET seats = {current_seats} WHERE id = {org_id}"
                )
            except Exception as re:
                print(
                    f"[seats] DB rollback failed (remove_seats): "
                    f"org={org_id} original_seats={current_seats} err={re}"
                )
            return JSONResponse(
                {
                    "error": "stripe_error",
                    "message": f"Failed to update subscription: {e}",
                },
                status_code=502,
            )

        monthly_cents = new_quantity * PRICE_PER_SEAT_CENTS

        return JSONResponse(
            {
                "seats": new_quantity,
                "staff_count": staff_count,
                "available": new_quantity - staff_count,
                "price_per_seat_cents": PRICE_PER_SEAT_CENTS,
                "price_per_seat_display": format_cents(PRICE_PER_SEAT_CENTS),
                "monthly_total_cents": monthly_cents,
                "monthly_total_display": format_cents(monthly_cents),
                "message": f"Removed {payload.seats} staff member(s). You now have {new_quantity} reserved.",
            }
        )


# ── POST /cancel — cancel the entire subscription (all seats) ────────────────
# Self-serve cancel/reactivate disabled — customers contact sales agent.
#
# async def cancel_subscription(request: Request):
#     ...
# router.post("/cancel")(cancel_subscription)


# ── POST /reactivate — reactivate a canceled subscription ────────────────────
# Self-serve cancel/reactivate disabled — customers contact sales agent.
#
# async def reactivate_subscription(request: Request):
#     ...
# router.post("/reactivate")(reactivate_subscription)