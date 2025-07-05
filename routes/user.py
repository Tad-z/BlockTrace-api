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
        "email": current_user["email"],
        "supabase_id": current_user["supabase_id"],
        "wallet_addresses": current_user.get("wallet_addresses", []),
        "subscription_tier": current_user.get("subscription_tier", "free"),
        "created_at": current_user.get("created_at", datetime.utcnow()),
        "updated_at": current_user.get("updated_at", datetime.utcnow())
    }

