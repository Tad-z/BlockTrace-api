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


@router.post("/challenge", response_model=WalletChallengeResponse)
async def create_wallet_challenge(
    body: WalletChallengeRequest,
    request: Request,
    current_user=Depends(get_current_user)
):
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
    # delete existing challenges for this wallet
    await db.challenges.delete_many({
        "$or": [
            {"used": True},
            {"expires_at": {"$lt": datetime.utcnow()}}
        ]
    })


   # 🔍 Check if wallet is already linked to current user
    has_wallet = await db.users.find_one({
        "_id": current_user["_id"],
        "wallet_addresses": {
            "$elemMatch": {
                "address": address,
                "chain": chain
            }
        }
    })

    if has_wallet:
        # First, set ALL wallets to not primary
        await db.users.update_one(
            {"_id": current_user["_id"]},
            {
                "$set": {
                    "wallet_addresses.$[].is_primary": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        # Then, set the specific wallet as primary
        await db.users.update_one(
            {
                "_id": current_user["_id"],
                "wallet_addresses.address": address,
                "wallet_addresses.chain": chain
            },
            {
                "$set": {
                    "wallet_addresses.$.is_primary": True,
                    "updated_at": datetime.utcnow()
                }
            }
        )


        print(f"Wallet {address} on chain {chain} already linked. Marked as primary.")
        return WalletChallengeResponse(
            challenge=None,
            expires_in=0,
            already_linked=True,
            message="Wallet already linked to your account. Marked as primary."
        )

    # 🧾 New wallet → Create challenge
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
    challenge_doc = {
        "wallet_address": address,
        "chain": chain,
        "challenge": challenge_message,
        "nonce": nonce,
        "used": False,
        "created_at": datetime.utcnow(),
        "expires_at": expires_at,
    }
    await db.challenges.insert_one(challenge_doc)

    print(f"Created challenge for wallet {address} on chain {chain}")
    return WalletChallengeResponse(
        challenge=challenge_message,
        expires_in=600,
        already_linked=False,
        message="Challenge created"
    )



@router.post("/verify", response_model=WalletVerifyResponse)
async def verify_wallet_signature(
    body: WalletVerifyRequest,
    request: Request,
    current_user=Depends(get_current_user)
):
    db = get_db(request.app)

    address = (
        body.wallet_address.lower() if body.chain == "ethereum" else body.wallet_address
    )
    chain = body.chain

    # 🔎 Get valid challenge for this wallet
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

    # ✅ Verify signature
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

    # ✅ Mark challenge as used
    await db.challenges.update_one(
        {"_id": challenge_doc["_id"]},
        {"$set": {"used": True}}
    )

    # ❌ Set all other wallets as not primary
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {
            "$set": {"updated_at": datetime.utcnow()},
            "$set": {
                "wallet_addresses.$[elem].is_primary": False
            }
        },
        array_filters=[
            {
                "elem.address": {"$ne": address},
                "elem.chain": {"$ne": chain}
            }
        ]
    )

    # ✅ Add new wallet as primary
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {
            "$push": {
                "wallet_addresses": {
                    "address": address,
                    "chain": chain,
                    "verified": True,
                    "added_at": datetime.utcnow(),
                    "is_primary": True,
                }
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )

    print(f"Wallet {address} on chain {chain} linked successfully for user {current_user['_id']}")
    return WalletVerifyResponse(success=True, message="Wallet linked successfully.")



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
