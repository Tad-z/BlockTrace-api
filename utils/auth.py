# dependencies/auth.py
from fastapi import Request, Depends, Header, HTTPException
from db import get_db
from utils.supabase_auth import verify_supabase_token
from datetime import datetime
from bson import ObjectId

async def get_current_user(request: Request, authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ")[1]

    try:
        db = get_db(request.app)
        payload = verify_supabase_token(token)
        supabase_id = payload["sub"]
        email = payload.get("email")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid Supabase token: {str(e)}")

    user = await db["users"].find_one({"supabase_id": supabase_id})
    if not user:
        # Optionally auto-create
        user = {
            "supabase_id": supabase_id,
            "email": email,
            "wallet_addresses": [],
            "subscription_tier": "free",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        result = await db["users"].insert_one(user)
        user["_id"] = result.inserted_id

    return user
