from fastapi import APIRouter, Request, HTTPException, Depends
from utils.auth import get_current_user
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from models.wallet import (
    WalletChallengeRequest,
    WalletChallengeResponse,
    WalletVerifyRequest,
    WalletVerifyResponse,
)
from utils.signatures import verify_ethereum_signature, verify_solana_signature
from db import get_db
import uuid
import base58
import re

router = APIRouter()


# Validation utilities
def is_valid_ethereum_address(address: str) -> bool:
    return bool(re.match(r"^0x[a-fA-F0-9]{40}$", address))


def is_valid_solana_address(address: str) -> bool:
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False


# Challenge endpoint
@router.post("/challenge", response_model=WalletChallengeResponse)
async def create_wallet_challenge(body: WalletChallengeRequest, request: Request):
    chain = body.chain
    address = (
        body.wallet_address.lower() if chain == "ethereum" else body.wallet_address
    )

    if chain not in ["ethereum", "solana"]:
        raise HTTPException(status_code=400, detail="Unsupported chain")

    if chain == "ethereum" and not is_valid_ethereum_address(address):
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    elif chain == "solana" and not is_valid_solana_address(address):
        raise HTTPException(status_code=400, detail="Invalid Solana address")

    db = get_db(request.app)
    nonce = str(uuid.uuid4())
    timestamp = int(datetime.utcnow().timestamp())
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
    print(f"Creating challenge for {address} on {chain} with nonce {nonce}")
    await db.challenges.insert_one(
        {
            "wallet_address": address,
            "chain": chain,
            "challenge": challenge_message,
            "nonce": nonce,
            "used": False,
            "created_at": datetime.utcnow(),
            "expires_at": expires_at,
        }
    )

    return WalletChallengeResponse(challenge=challenge_message, expires_in=600)


# Verify endpoint
@router.post("/verify", response_model=WalletVerifyResponse)
async def verify_wallet_signature(
    body: WalletVerifyRequest, request: Request, current_user=Depends(get_current_user)
):
    db = get_db(request.app)
    print("body:", body)
    address = (
        body.wallet_address.lower() if body.chain == "ethereum" else body.wallet_address
    )
    chain = body.chain

    # Check if wallet is already linked to another user
    existing_wallet = await db.users.find_one(
        {
            "wallet_addresses.address": address,
            "wallet_addresses.chain": chain,
            "_id": {"$ne": current_user["_id"]},
        }
    )

    if existing_wallet:
        raise HTTPException(
            status_code=400, detail="Wallet already linked to another account"
        )

    # Prevent duplicate on current user
    has_wallet = await db.users.find_one(
        {
            "_id": current_user["_id"],
            "wallet_addresses": {"$elemMatch": {"address": address, "chain": chain}},
        }
    )

    if has_wallet:
        raise HTTPException(
            status_code=400, detail="Wallet already linked to your account"
        )

    # Get challenge
    challenge_doc = await db.challenges.find_one(
        {
            "wallet_address": address,
            "chain": chain,
            "used": False,
            "expires_at": {"$gt": datetime.utcnow()},
        }
    )

    if not challenge_doc:
        raise HTTPException(status_code=400, detail="No valid challenge found")

    # Verify signature
    if chain == "ethereum":
        valid = verify_ethereum_signature(
            address, challenge_doc["challenge"], body.signature
        )
    elif chain == "solana":
        valid = verify_solana_signature(
            address, challenge_doc["challenge"], body.signature
        )
    else:
        raise HTTPException(status_code=400, detail="Unsupported chain")

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid signature")

    await db.challenges.update_one(
        {"_id": challenge_doc["_id"]}, {"$set": {"used": True}}
    )

    is_primary = len(current_user.get("wallet_addresses", [])) == 0
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {
            "$push": {
                "wallet_addresses": {
                    "address": address,
                    "chain": chain,
                    "verified": True,
                    "added_at": datetime.utcnow(),
                    "is_primary": is_primary,
                }
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )

    return WalletVerifyResponse(success=True, message="Wallet linked successfully")


# List wallets
@router.get("/list")
async def list_user_wallets(current_user=Depends(get_current_user)):
    return {"wallets": current_user.get("wallet_addresses", [])}


# Remove wallet
class WalletRemoveRequest(BaseModel):
    wallet_address: str = Field(..., description="Wallet address to remove")
    chain: str = Field(..., description="Blockchain chain (e.g., ethereum, solana)")


@router.delete("/remove")
async def remove_wallet(
    body: WalletRemoveRequest, request: Request, current_user=Depends(get_current_user)
):
    db = get_db(request.app)
    address = (
        body.wallet_address.lower() if body.chain == "ethereum" else body.wallet_address
    )

    # Remove wallet
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$pull": {"wallet_addresses": {"address": address, "chain": body.chain}}},
    )

    # Fetch updated user
    user = await db.users.find_one({"_id": current_user["_id"]})
    wallets = user.get("wallet_addresses", [])

    # If primary wallet was removed and others remain, promote the first
    removed_was_primary = any(
        (w["address"].lower() if w["chain"] == "ethereum" else w["address"]) == address
        and w["chain"] == body.chain
        and w.get("is_primary", False)
        for w in current_user.get("wallet_addresses", [])
    )

    if removed_was_primary and wallets:
        first_wallet = wallets[0]
        await db.users.update_one(
            {
                "_id": current_user["_id"],
                "wallet_addresses.address": first_wallet["address"],
                "wallet_addresses.chain": first_wallet["chain"],
            },
            {"$set": {"wallet_addresses.$.is_primary": True}},
        )

    return {"success": True, "message": "Wallet removed successfully"}
