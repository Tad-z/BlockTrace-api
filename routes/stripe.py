import os
import asyncio
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
    print(update)

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
    Starts Stripe Checkout in subscription mode for the Pro plan.
    Bills immediately and sets up monthly renewals.
    """

    # --- Step 1: Verify environment configuration ---
    missing_env = [k for k, v in {
        "STRIPE_SECRET_KEY": stripe.api_key,
        "PRO_PLAN_PRICE_ID": PRO_PRICE_ID,
        "FRONTEND_SUCCESS_URL": SUCCESS_URL,
        "FRONTEND_CANCEL_URL": CANCEL_URL
    }.items() if not v]

    if missing_env:
        raise HTTPException(
            status_code=500,
            detail=f"Missing Stripe environment variables: {', '.join(missing_env)}"
        )

    db = get_db(request.app)
    user_id = str(current_user["_id"])

    # --- Step 2: Ensure a valid Stripe customer ---
    try:
        stripe_customer_id = current_user.get("stripe_customer_id")
        email = current_user["email"]

        if not stripe_customer_id:
            # No customer yet — create one
            customer = stripe.Customer.create(
                email=email,
                metadata={"user_id": user_id},
            )
            db["users"].update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {
                    "stripe_customer_id": customer.id,
                    "updated_at": utcnow()
                }}
            )
            customer_id = customer.id
        else:
            # Validate that customer still exists in Stripe
            try:
                stripe.Customer.retrieve(stripe_customer_id)
                customer_id = stripe_customer_id
            except stripe.InvalidRequestError as e:
                # If Stripe says the customer doesn't exist, recreate it
                print(f"⚠️ Stripe customer missing ({stripe_customer_id}), recreating...")

                customer = stripe.Customer.create(
                    email=email,
                    metadata={"user_id": user_id},
                )
                db["users"].update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {
                        "stripe_customer_id": customer.id,
                        "updated_at": utcnow()
                    }}
                )
                customer_id = customer.id

    except stripe.StripeError as e:
        print(f"❌ Stripe error while ensuring customer: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Stripe customer error: {e.user_message or str(e)}"
        )


    # --- Step 3: Prevent duplicate active subscriptions ---
    sub_tier = current_user.get("subscription_tier")
    cancel_at_period_end = current_user.get("subscription_cancel_at_period_end", False)

    if sub_tier == "pro" and not cancel_at_period_end:
        raise HTTPException(400, "User already has an active subscription.")

    # --- Step 4: Create Checkout Session with retry safety ---
    try:
        for attempt in range(3):  # transient retry for network hiccups
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
                        "user_id": user_id,
                        "plan": "pro",
                    },
                )
                break
            except stripe.APIConnectionError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5)  # small delay before retry

        print(session.url)
        return {"checkout_url": session.url}

    except stripe.StripeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": e.user_message or str(e), "code": getattr(e, "code", None)}
        )



# ---------- Billing Portal (manage payment method, cancel, invoices)

@router.post("/create-billing-portal-session", response_model=PortalResponse)
async def create_billing_portal_session(
    request: Request,
    current_user=Depends(get_current_user)
):
    """
    Generates a Stripe Billing Portal session for the user.
    Allows them to manage payment methods, view invoices, or cancel subscriptions.
    """

    db = get_db(request.app)
    customer_id = current_user.get("stripe_customer_id")

    # --- Step 1: Validate customer existence ---
    if not customer_id:
        print("No customer id")
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer on file. Please start a subscription first."
        )

    # --- Step 2: Verify the customer still exists on Stripe ---
    try:
        stripe.Customer.retrieve(customer_id)
    except stripe.InvalidRequestError:
        # If the Stripe customer was deleted, clean up local record
        db["users"].update_one(
            {"_id": current_user["_id"]},
            {"$unset": {"stripe_customer_id": ""}}
        )
        print("Stripe customer not found")
        raise HTTPException(
            status_code=404,
            detail="Stripe customer not found. Please start a new subscription."
        )

    # --- Step 3: Generate billing portal session ---
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            # Return URL should ideally be a dashboard or account settings page
            return_url=SUCCESS_URL or CANCEL_URL or "https://yourapp.com/dashboard",
        )

        print(portal_session.url)
        return {"portal_url": portal_session.url}

    except stripe.StripeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": e.user_message or str(e), "code": getattr(e, "code", None)}
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error creating billing portal: {str(e)}"
        )



# ---------- Cancel at period end

@router.post("/cancel-subscription", response_model=CancelResponse)
async def cancel_subscription(request: Request, current_user=Depends(get_current_user)):
    """
    Schedules cancellation of the user's active subscription at the end of the billing period.
    The user retains Pro access until the current period ends.
    """
    db = get_db(request.app)
    sub_id = current_user.get("stripe_subscription_id")

    if not sub_id:
        raise HTTPException(400, "No active subscription to cancel.")

    try:
        # --- 1️⃣ Retrieve first to verify it's still active ---
        sub = stripe.Subscription.retrieve(sub_id)
        if sub.status in ("canceled", "incomplete_expired"):
            raise HTTPException(400, "Subscription is already inactive.")

        # --- 2️⃣ Schedule cancellation at period end ---
        updated_sub = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)

        # --- 3️⃣ Update DB with cancellation metadata ---
        db["users"].update_one(
            {"_id": ObjectId(str(current_user["_id"]))},
            {"$set": {
                "subscription_cancel_at_period_end": True,
                "subscription_status": updated_sub.status,
                "subscription_current_period_end": ts_to_dt(updated_sub.current_period_end),
                "updated_at": utcnow(),
            }},
        )

        return {
            "status": "cancel_scheduled",
            "access_until": updated_sub.current_period_end,
            "message": "Your subscription will remain active until the end of the billing period."
        }

    except stripe.InvalidRequestError as e:
        # Usually happens if the subscription ID is invalid or already canceled
        raise HTTPException(404, f"Invalid or missing subscription: {e.user_message or str(e)}")

    except stripe.APIConnectionError:
        raise HTTPException(503, "Stripe connection failed, please try again later.")

    except stripe.StripeError as e:
        raise HTTPException(400, f"Stripe error: {e.user_message or str(e)}")

    except Exception as e:
        # Catch any unexpected DB or runtime issue
        raise HTTPException(500, f"Unexpected error: {str(e)}")



# ---------- Subscription Status (helper)

@router.get("/subscription-status")
async def subscription_status(current_user=Depends(get_current_user)):
    """
    Returns the user's current subscription status.
    Falls back to database state if Stripe API is unreachable.
    """
    sub_id = current_user.get("stripe_subscription_id")

    # --- 1️⃣ Handle users without Stripe subscription ---
    if not sub_id:
        print("no sub id")
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": current_user.get("subscription_status", "no_subscription"),
            "cancel_at_period_end": current_user.get("subscription_cancel_at_period_end", False),
            "source": "db",
        }

    try:
        # --- 2️⃣ Retrieve from Stripe ---
        sub = stripe.Subscription.retrieve(sub_id)

        return {
            "tier": tier_from_status(sub.status),
            "status": sub.status,
            "current_period_start": ts_to_dt(sub.get("current_period_start")),
            "current_period_end": ts_to_dt(sub.get("current_period_end")),
            "cancel_at_period_end": sub.get("cancel_at_period_end", False),
            "source": "stripe",
        }


    except stripe.InvalidRequestError as e:
        # Subscription might have been deleted or invalid
        print("Subscription might have been deleted or invalid")
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": "invalid_subscription",
            "error": e.user_message or str(e),
            "source": "fallback_db",
        }

    except stripe.APIConnectionError:
        # Network or connection issue to Stripe
        print("Network or connection issue to Stripe")
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": current_user.get("subscription_status", "unknown"),
            "error": "Unable to reach Stripe servers, showing cached data.",
            "source": "fallback_db",
        }

    except stripe.StripeError as e:
        # Any other Stripe error
        print("stripe error")
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": current_user.get("subscription_status", "unknown"),
            "error": e.user_message or str(e),
            "source": "fallback_db",
        }

    except Exception as e:
        # Catch-all fallback
        print(f"exception error: {str(e)}")
        return {
            "tier": current_user.get("subscription_tier", "free"),
            "status": "error",
            "error": str(e),
            "source": "fallback_db",
        }



# ---------- Stripe Webhook (single source of truth)

# ---------- Stripe Webhook (single source of truth)

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Handles Stripe webhook events.
    Keeps local subscription state in sync with Stripe.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    # --- 1️⃣ Verify event authenticity ---
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload.")
    except stripe.SignatureVerificationError:  # ✅ Fixed: removed .error
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")

    event_type = event["type"]
    data_object = event["data"]["object"]

    print(event_type)

    try:
        # --- 2️⃣ Checkout completed: first payment success ---
        if event_type == "checkout.session.completed":
            session = data_object
            if session.get("total_details", {}).get("amount_discount"):
                promo_info = session.get("discounts", [])
                print("User used promo:", promo_info)
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            if not customer_id:
                print("no customer id")
                raise ValueError("Missing customer ID in session.")

            # Retrieve canonical subscription info
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                set_user_subscription_by_customer_id(
                    request,  # ✅ Added request parameter
                    customer_id,
                    tier=tier_from_status(sub.status),
                    subscription_id=sub.id,
                    subscription_status=sub.status,
                    current_period_start=sub.get("current_period_start"),  # ✅ Safe access
                    current_period_end=sub.get("current_period_end"),      # ✅ Safe access
                    started_at=sub.get("start_date"),
                    status_change_reason="checkout_completed",
                    extra_sets={
                        "stripe_customer_id": customer_id,
                        "subscription_cancel_at_period_end": sub.get("cancel_at_period_end", False),
                    },
                )

        # --- 3️⃣ Recurring invoice success ---
        elif event_type == "invoice.payment_succeeded":
            invoice = data_object
            customer_id = invoice.get("customer")
            subscription_id = invoice.get("subscription")

            if not customer_id:
                raise ValueError("Missing customer ID in invoice.")

            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                set_user_subscription_by_customer_id(
                    request,  # ✅ Added request parameter
                    customer_id,
                    tier=tier_from_status(sub.status),
                    subscription_id=sub.id,
                    subscription_status=sub.status,
                    current_period_start=sub.get("current_period_start"),  # ✅ Safe access
                    current_period_end=sub.get("current_period_end"),      # ✅ Safe access
                    status_change_reason="invoice_paid",
                    extra_sets={"last_payment_date": utcnow()},
                )

        # --- 4️⃣ Payment failed: mark past_due but don't downgrade yet ---
        elif event_type == "invoice.payment_failed":
            invoice = data_object
            customer_id = invoice.get("customer")
            subscription_id = invoice.get("subscription")

            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                set_user_subscription_by_customer_id(
                    request,  # ✅ Added request parameter
                    customer_id,
                    tier=tier_from_status(sub.status),
                    subscription_id=sub.id,
                    subscription_status=sub.status,
                    current_period_start=sub.get("current_period_start"),  # ✅ Safe access
                    current_period_end=sub.get("current_period_end"),      # ✅ Safe access
                    status_change_reason="invoice_failed",
                    extra_sets={"payment_failed_date": utcnow()},
                )

        # --- 5️⃣ Subscription updated (status or cancel flag changed) ---
        elif event_type == "customer.subscription.updated":
            sub = data_object
            customer_id = sub["customer"]
            new_tier = tier_from_status(sub["status"])

            set_user_subscription_by_customer_id(
                request,  # ✅ Added request parameter
                customer_id,
                tier=new_tier,
                subscription_id=sub["id"] if new_tier == "pro" else None,
                subscription_status=sub["status"],
                current_period_start=sub.get("current_period_start"),  # ✅ Safe access
                current_period_end=sub.get("current_period_end"),      # ✅ Safe access
                started_at=sub.get("start_date"),
                status_change_reason="subscription_updated",
                extra_sets={
                    "subscription_cancel_at_period_end": sub.get("cancel_at_period_end", False),
                },
            )

        # --- 6️⃣ Subscription deleted/canceled ---
        elif event_type == "customer.subscription.deleted":
            sub = data_object
            customer_id = sub["customer"]

            set_user_subscription_by_customer_id(
                request,  # ✅ Added request parameter
                customer_id,
                tier="free",
                subscription_id=None,
                subscription_status=sub["status"],
                current_period_start=sub.get("current_period_start"),  # ✅ Safe access
                current_period_end=sub.get("current_period_end"),      # ✅ Safe access
                started_at=sub.get("start_date"),
                status_change_reason="subscription_deleted",
            )

        # --- 7️⃣ Ignore unhandled events safely ---
        else:
            print(f"Unhandled event type: {event_type}")

        return {"status": "ok"}

    except stripe.StripeError as e:  # ✅ Fixed: removed .error
        print(f"⚠️ Stripe API error on webhook: {str(e)}")
        return {"status": "stripe_error", "error": str(e)}

    except Exception as e:
        print(f"⚠️ Webhook processing error: {str(e)}")
        return {"status": "internal_error", "error": str(e)}


# 5️⃣ Get invoice history
@router.get("/invoice-history")
def get_invoice_history(current_user=Depends(get_current_user)):
    """
    Fetch user's invoice history from Stripe.
    Returns a list of past invoices and payments for transparency and support.
    """
    customer_id = current_user.get("stripe_customer_id")
    if not customer_id:
        return {"invoices": [], "total_count": 0, "source": "no_stripe_customer"}

    try:
        # --- 1️⃣ Retrieve invoices ---
        invoices = stripe.Invoice.list(customer=customer_id, limit=100)

        # --- 2️⃣ Format data for frontend ---
        invoice_history = []
        for inv in invoices.auto_paging_iter():
            invoice_history.append({
                "id": inv.id,
                "number": getattr(inv, "number", None),
                "status": getattr(inv, "status", "unknown"),  # "paid", "open", etc.
                "currency": inv.currency.upper() if hasattr(inv, "currency") else None,
                "amount_due": (inv.amount_due or 0) / 100,
                "amount_paid": (inv.amount_paid or 0) / 100,
                "amount_remaining": (inv.amount_remaining or 0) / 100,
                "billing_reason": getattr(inv, "billing_reason", None),
                "attempted": getattr(inv, "attempted", False),
                "created": ts_to_dt(inv.created),
                "period_start": ts_to_dt(getattr(inv, "period_start", None)),
                "period_end": ts_to_dt(getattr(inv, "period_end", None)),
                "hosted_invoice_url": getattr(inv, "hosted_invoice_url", None),
                "invoice_pdf": getattr(inv, "invoice_pdf", None),
                "subscription": getattr(inv, "subscription", None),
            })

        # --- 3️⃣ Sort by most recent ---
        invoice_history.sort(key=lambda x: x["created"], reverse=True)
        return {
            "invoices": invoice_history,
            "total_count": len(invoice_history),
            "source": "stripe",
        }

    except stripe.InvalidRequestError as e:
        # Happens if customer ID invalid or deleted
        print("customer ID invalid or deleted")
        raise HTTPException(status_code=404, detail=f"Invalid Stripe customer: {e.user_message or str(e)}")

    except stripe.APIConnectionError:
        # Stripe connection issue
        print("Stripe connection issue")
        raise HTTPException(status_code=503, detail="Unable to connect to Stripe. Please try again later.")

    except stripe.StripeError as e:
        print("stripe error")
        raise HTTPException(status_code=400, detail=f"Stripe error: {e.user_message or str(e)}")

    except Exception as e:
        print("exception error")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

