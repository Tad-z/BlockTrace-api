import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from bson import ObjectId

# Plug these into your project structure

from utils.auth import get_current_user
from db import get_db

router = APIRouter()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

PRO_PRICE_ID = os.getenv("PRO_PLAN_PRICE_ID")
SUCCESS_URL = os.getenv("FRONTEND_SUCCESS_URL")
CANCEL_URL = os.getenv("FRONTEND_CANCEL_URL")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")


# ---------- Helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)

def set_user_subscription_by_customer_id(
    request: Request,
    customer_id: str,
    *,
    tier: str,
    subscription_id: Optional[str],
    subscription_status: Optional[str],
    current_period_start: Optional[int] = None,
    current_period_end: Optional[int] = None,
    started_at: Optional[int] = None,
    status_change_reason: Optional[str] = None,
    extra_sets: Optional[dict] = None,
):
    """Centralized DB updater. Idempotent and consistent."""
    update = {
        "subscription_tier": tier,                               # "pro" | "free"
        "stripe_subscription_id": subscription_id,               # may be None upon cancel
        "subscription_status": subscription_status,              # mirrors Stripe status
        "subscription_current_period_start": ts_to_dt(current_period_start),
        "subscription_current_period_end": ts_to_dt(current_period_end),
        "subscription_started_at": ts_to_dt(started_at) or (utcnow() if tier == "pro" else None),
        "status_change_reason": status_change_reason,
        "updated_at": utcnow(),
    }
    if extra_sets:
        update.update(extra_sets)

    db = get_db(request.app)

    db["users"].update_one(
        {"stripe_customer_id": customer_id},
        {"$set": update},
        upsert=False,  # we expect user to already exist
    )

def tier_from_status(stripe_status: str) -> str:
    """
    Map Stripe subscription status to app tier.
    We keep users 'pro' for non-terminal states (trialing, active, past_due, incomplete),
    and downgrade only on terminal states (canceled, unpaid, incomplete_expired).
    """
    terminal_free = {"canceled", "unpaid", "incomplete_expired"}
    return "free" if stripe_status in terminal_free else "pro"


# ---------- Models

class CheckoutResponse(BaseModel):
    checkout_url: str

class PortalResponse(BaseModel):
    portal_url: str

class CancelResponse(BaseModel):
    status: str
    access_until: Optional[int] = None  # epoch seconds from Stripe


# ---------- Create Checkout Session (subscription)

@router.post("/create-checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(request: Request, current_user=Depends(get_current_user)):
    """
    Starts Stripe Checkout in subscription mode for Pro plan.
    Bills immediately and sets up monthly renewals.
    """
    db = get_db(request.app)
    if not PRO_PRICE_ID or not SUCCESS_URL or not CANCEL_URL:
        raise HTTPException(500, "Stripe env vars not configured correctly.")

    user_id = str(current_user["_id"])

    # Create/ensure Stripe customer
    if not current_user.get("stripe_customer_id"):
        customer = stripe.Customer.create(
            email=current_user["email"],
            metadata={"user_id": user_id},
            # name=current_user.get("name"),
        )
        db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"stripe_customer_id": customer.id, "updated_at": utcnow()}},
        )
        customer_id = customer.id
    else:
        customer_id = current_user["stripe_customer_id"]

    # If already pro and active-ish, prevent duplicate checkout
    if current_user.get("subscription_tier") == "pro":
        raise HTTPException(400, "User already has an active subscription.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            allow_promotion_codes=True,
            payment_method_types=["card"],
            line_items=[{"price": PRO_PRICE_ID, "quantity": 1}],
            success_url=SUCCESS_URL,
            cancel_url=CANCEL_URL,
            metadata={
                "user_id": user_id,       # <— helps during webhook reconciliation
                "plan": "pro",
            },
        )
        return {"checkout_url": session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {e.user_message or str(e)}")


# ---------- Billing Portal (manage payment method, cancel, invoices)

@router.post("/create-billing-portal-session", response_model=PortalResponse)
async def create_billing_portal_session(current_user=Depends(get_current_user)):
    customer_id = current_user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No Stripe customer on file.")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=CANCEL_URL or "https://yourapp.com/dashboard",
        )
        return {"portal_url": portal.url}
    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {e.user_message or str(e)}")


# ---------- Cancel at period end

@router.post("/cancel-subscription", response_model=CancelResponse)
async def cancel_subscription(request: Request, current_user=Depends(get_current_user)):
    sub_id = current_user.get("stripe_subscription_id")
    db = get_db(request.app)
    if not sub_id:
        raise HTTPException(400, "No active subscription to cancel.")

    try:
        sub = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        # Keep tier 'pro' until end of period; record intent
        db["users"].update_one(
            {"_id": ObjectId(str(current_user["_id"]))},
            {"$set": {
                "subscription_cancel_at_period_end": True,
                "subscription_status": sub.status,
                "subscription_current_period_end": ts_to_dt(sub.current_period_end),
                "updated_at": utcnow(),
            }},
        )
        return {"status": "cancel_scheduled", "access_until": sub.current_period_end}
    except stripe.error.StripeError as e:
        raise HTTPException(400, f"Stripe error: {e.user_message or str(e)}")


# ---------- Subscription Status (helper)

@router.get("/subscription-status")
async def subscription_status(current_user=Depends(get_current_user)):
    sub_id = current_user.get("stripe_subscription_id")
    if not sub_id:
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": current_user.get("subscription_status", "no_subscription"),
        }
    try:
        sub = stripe.Subscription.retrieve(sub_id)
        return {
            "tier": tier_from_status(sub.status),
            "status": sub.status,
            "current_period_start": sub.current_period_start,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
        }
    except stripe.error.StripeError as e:
        # Fallback to DB state if Stripe call fails
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": current_user.get("subscription_status", "unknown"),
            "error": e.user_message or str(e),
        }


# ---------- Stripe Webhook (single source of truth)

@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")
    except ValueError:
        raise HTTPException(400, "Invalid payload")

    et = event["type"]
    data = event["data"]["object"]

    # 1) Initial success: Checkout completed → upgrade to PRO
    if et == "checkout.session.completed":
        session = data
        customer_id = session["customer"]
        subscription_id = session.get("subscription")

        # Retrieve subscription to get accurate status & period
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            set_user_subscription_by_customer_id(
                customer_id,
                tier=tier_from_status(sub.status),
                subscription_id=sub.id,
                subscription_status=sub.status,
                current_period_start=sub.current_period_start,
                current_period_end=sub.current_period_end,
                started_at=sub.start_date,
                status_change_reason="checkout_completed",
                extra_sets={
                    "stripe_customer_id": customer_id,
                    "subscription_cancel_at_period_end": sub.cancel_at_period_end,
                },
            )

    # 2) Recurring success: keep PRO, refresh period boundaries
    elif et == "invoice.payment_succeeded":
        invoice = data
        customer_id = invoice["customer"]
        subscription_id = invoice.get("subscription")

        # Refresh from subscription for canonical status/period
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            set_user_subscription_by_customer_id(
                customer_id,
                tier=tier_from_status(sub.status),
                subscription_id=sub.id,
                subscription_status=sub.status,
                current_period_start=sub.current_period_start,
                current_period_end=sub.current_period_end,
                status_change_reason="invoice_paid",
                extra_sets={"last_payment_date": utcnow()},
            )

    # 3) Payment failed: DO NOT downgrade yet (grace period). Mark past_due.
    elif et == "invoice.payment_failed":
        invoice = data
        customer_id = invoice["customer"]
        subscription_id = invoice.get("subscription")

        # If subscription exists, mark status but keep tier = pro unless terminal
        if subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            set_user_subscription_by_customer_id(
                customer_id,
                tier=tier_from_status(sub.status),  # past_due -> still pro
                subscription_id=sub.id,
                subscription_status=sub.status,
                current_period_start=sub.current_period_start,
                current_period_end=sub.current_period_end,
                status_change_reason="invoice_failed",
                extra_sets={"payment_failed_date": utcnow()},
            )

    # 4) Subscription updated (status changes). Downgrade only on terminal.
    elif et == "customer.subscription.updated":
        sub = data
        customer_id = sub["customer"]
        new_tier = tier_from_status(sub["status"])

        set_user_subscription_by_customer_id(
            customer_id,
            tier=new_tier,
            subscription_id=sub["id"] if new_tier == "pro" else None,
            subscription_status=sub["status"],
            current_period_start=sub.get("current_period_start"),
            current_period_end=sub.get("current_period_end"),
            started_at=sub.get("start_date"),
            status_change_reason="subscription_updated",
            extra_sets={"subscription_cancel_at_period_end": sub.get("cancel_at_period_end", False)},
        )

    # 5) Subscription deleted (canceled/expired) → downgrade to FREE
    elif et == "customer.subscription.deleted":
        sub = data
        customer_id = sub["customer"]

        set_user_subscription_by_customer_id(
            customer_id,
            tier="free",
            subscription_id=None,
            subscription_status=sub["status"],  # should be 'canceled'
            current_period_start=sub.get("current_period_start"),
            current_period_end=sub.get("current_period_end"),
            started_at=sub.get("start_date"),
            status_change_reason="subscription_deleted",
        )

    return {"status": "ok"}

# 5️⃣ Get invoice history
@router.get("/invoice-history")
def get_invoice_history(current_user=Depends(get_current_user)):
    """Fetch user's invoice history from Stripe"""
    try:
        customer_id = current_user.get("stripe_customer_id")
        if not customer_id:
            return {"invoices": []}
        
        # Fetch invoices for this customer
        invoices = stripe.Invoice.list(
            customer=customer_id,
            limit=100  # Adjust limit as needed
        )
        
        # Format invoice data for frontend
        invoice_history = []
        for invoice in invoices.data:
            invoice_history.append({
                "id": invoice.id,
                "amount_due": invoice.amount_due / 100,  # Convert cents to dollars
                "amount_paid": invoice.amount_paid / 100,
                "currency": invoice.currency.upper(),
                "status": invoice.status,  # "paid", "open", "void", "uncollectible"
                "created": invoice.created,  # Unix timestamp
                "period_start": invoice.period_start,
                "period_end": invoice.period_end,
                "invoice_pdf": invoice.invoice_pdf,  # PDF download URL
                "hosted_invoice_url": invoice.hosted_invoice_url,  # Web view URL
                "number": invoice.number,  # Invoice number (e.g., "INV-001")
                "paid": invoice.paid,
                "attempted": invoice.attempted,
                "billing_reason": invoice.billing_reason,  # "subscription_cycle", "subscription_create", etc.
            })
        
        return {
            "invoices": invoice_history,
            "total_count": len(invoice_history)
        }
        
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}")
