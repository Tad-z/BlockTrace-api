# routes/wallet_auth.py
from fastapi import APIRouter, Request, HTTPException, Depends
from utils.auth import get_current_user
from pydantic import BaseModel
from datetime import datetime, timedelta
from models.wallet import WalletChallengeRequest, WalletChallengeResponse, WalletVerifyRequest, WalletVerifyResponse
from utils.signatures import verify_ethereum_signature, verify_solana_signature
from db import get_db
import uuid

router = APIRouter()



@router.post("/auth/wallet/challenge", response_model=WalletChallengeResponse)
async def create_wallet_challenge(body: WalletChallengeRequest, request: Request):
    address = body.wallet_address.lower()
    chain = body.chain

    # Validate
    if chain not in ["ethereum", "solana"]:
        raise HTTPException(status_code=400, detail="Unsupported chain")

    # Create challenge string
    db = get_db(request.app)
    nonce = str(uuid.uuid4())
    timestamp = int(datetime.now().timestamp())
    challenge_message = f"""
BlockTrace Authentication

Please sign this message to verify your wallet ownership.

Wallet: {address}
Chain: {chain}
Nonce: {nonce}
Timestamp: {timestamp}

This request will not trigger any blockchain transaction or cost any gas fees.
"""
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    # Save to DB
    await db.challenges.insert_one({
        "wallet_address": address,
        "chain": chain,
        "challenge": challenge_message,
        "nonce": nonce,
        "used": False,
        "created_at": datetime.utcnow(),
        "expires_at": expires_at
    })

    return WalletChallengeResponse(
        challenge=challenge_message,
        expires_in=600
    )




@router.post("/auth/wallet/verify", response_model=WalletVerifyResponse)
async def verify_wallet_signature(
    body: WalletVerifyRequest,
    request: Request,
    current_user = Depends(get_current_user)
):
    db = get_db(request.app)
    address = body.wallet_address.lower()
    chain = body.chain

    # Get challenge
    challenge_doc = await db.challenges.find_one({
        "wallet_address": address,
        "chain": chain,
        "used": False,
        "expires_at": {"$gt": datetime.utcnow()}
    })

    if not challenge_doc:
        raise HTTPException(status_code=400, detail="No valid challenge found")

    # Verify signature
    if chain == "ethereum":
        valid = verify_ethereum_signature(address, challenge_doc["challenge"], request.signature)
    elif chain == "solana":
        valid = verify_solana_signature(address, challenge_doc["challenge"], request.signature)
    else:
        raise HTTPException(status_code=400, detail="Unsupported chain")

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Mark challenge as used
    await db.challenges.update_one(
        {"_id": challenge_doc["_id"]},
        {"$set": {"used": True}}
    )

    # Add wallet to current user
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$addToSet": {
            "wallet_addresses": {
                "address": address,
                "chain": chain,
                "verified": True,
                "added_at": datetime.utcnow(),
                "is_primary": False
            }
        }}
    )

    return WalletVerifyResponse(success=True, message="Wallet linked successfully")
