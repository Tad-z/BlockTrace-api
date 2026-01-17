# routes/auth.py
from fastapi import Request, APIRouter, Header, HTTPException, Depends
from db import get_db
from datetime import datetime
from utils.auth import get_current_user

router = APIRouter()
@router.get("/me")
async def get_current_user_details(current_user = Depends(get_current_user)):
    """
    Get the current authenticated user's details.
    """
    return {
        "id": str(current_user["_id"]),
        "blocktrace_id": current_user.get("blocktrace_id"),
        "email": current_user["email"],
        "supabase_id": current_user["supabase_id"],
        "wallet_addresses": current_user.get("wallet_addresses", []),
        "subscription_tier": current_user.get("subscription_tier", "free"),
        "subscription_status": current_user.get("subscription_status", None),
        "stripe_customer_id": current_user.get("stripe_customer_id", None),
        "stripe_subscription_id": current_user.get("stripe_subscription_id", None),
        "subscription_started_at": current_user.get("subscription_started_at", None),
        "subscription_current_period_start": current_user.get("subscription_current_period_start", None),
        "subscription_current_period_end": current_user.get("subscription_current_period_end", None),
        "subscription_cancel_at_period_end": current_user.get("subscription_cancel_at_period_end", False),
        "status_change_reason": current_user.get("status_change_reason", None),
        "last_payment_date": current_user.get("last_payment_date", None),
        "payment_failed_date": current_user.get("payment_failed_date", None),
        "created_at": current_user.get("created_at", datetime.utcnow()),
        "updated_at": current_user.get("updated_at", datetime.utcnow())
    }

