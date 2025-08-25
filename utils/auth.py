# dependencies/auth.py
from fastapi import Request, Header, HTTPException
from db import get_db
from utils.supabase_auth import verify_supabase_token
from datetime import datetime, timezone

def utcnow() -> datetime:
    """Consistent UTC datetime for the app."""
    return datetime.now(timezone.utc)

async def get_current_user(request: Request, authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ", 1)[1]
    db = get_db(request.app)

    try:
        payload = verify_supabase_token(token)
        supabase_id = payload["sub"]
        email = payload.get("email")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Supabase token: {str(e)}")

    user = await db["users"].find_one({"supabase_id": supabase_id})

    # Create new user with full flat subscription schema
    if not user:
        now = utcnow()
        user = {
            "supabase_id": supabase_id,
            "email": email,
            "wallet_addresses": [],

            # Subscription fields
            "subscription_tier": "free",                     # "free" | "pro"
            "subscription_status": None,                     # mirrors Stripe status
            "stripe_customer_id": None,
            "stripe_subscription_id": None,

            # Timing
            "subscription_started_at": None,
            "subscription_current_period_start": None,
            "subscription_current_period_end": None,
            "subscription_cancel_at_period_end": False,

            # Tracking
            "status_change_reason": None,
            "last_payment_date": None,
            "payment_failed_date": None,

            "created_at": now,
            "updated_at": now,
        }
        res = await db["users"].insert_one(user)
        user["id"] = res.inserted_id
        return user

    # 🔄 Auto-migration for legacy users
    migrate_updates = {}

    # If they never had the new fields, add them (don’t overwrite existing values)
    if "subscription_status" not in user:
        migrate_updates.update({
            "subscription_status": None,
            "stripe_customer_id": user.get("stripe_customer_id", None),
            "stripe_subscription_id": user.get("stripe_subscription_id", None),
            "subscription_started_at": None,
            "subscription_current_period_start": None,
            "subscription_current_period_end": None,
            "subscription_cancel_at_period_end": False,
            "status_change_reason": None,
            "last_payment_date": None,
            "payment_failed_date": None,
        })

    # Ensure subscription_tier exists (default to free if missing)
    if "subscription_tier" not in user:
        migrate_updates["subscription_tier"] = "free"

    if migrate_updates:
        migrate_updates["updated_at"] = utcnow()
        await db["users"].update_one({"_id": user["_id"]}, {"$set": migrate_updates})
        user.update(migrate_updates)

    return user
