# routes/auth.py
from fastapi import Request, APIRouter, Header, HTTPException
from db import get_db
from utils.supabase_auth import verify_supabase_token
from datetime import datetime

router = APIRouter()

@router.post("/api/auth/oauth")
async def handle_oauth_login(request: Request, authorization: str = Header(...)):
    db = get_db(request.app) 
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = authorization.split(" ")[1]
    try:
        payload = verify_supabase_token(token)
        supabase_id = payload["sub"]
        email = payload.get("email")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    
    # Check or create user in MongoDB
    user = await db.users.find_one({"supabase_id": supabase_id})
    if user:
        # Update email if changed
        if email and user.get("email") != email:
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"email": email, "updated_at": datetime.utcnow()}}
            )
    else:
        user = {
            "supabase_id": supabase_id,
            "email": email,
            "wallet_addresses": [],
            "subscription_tier": "free",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        result = await db.users.insert_one(user)
        user["_id"] = result.inserted_id

    return {
        "user_id": str(user["_id"]),
        "email": user["email"],
        "message": "OAuth login handled successfully"
    }
