# main.py
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from db import get_db
from utils.auth import get_current_user
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Literal
from datetime import datetime, timedelta
from routes.wallet import is_valid_solana_address, is_valid_ethereum_address
from utils.solana import analyze_solana_wallet_endpoint, analyze_solana_wallet_endpoint2
from utils.eth import analyze_ethereum_wallet_endpoint, analyze_ethereum_wallet_endpoint2
from utils.redis_client import redis_client
import json

router = APIRouter()

class WalletRequestModel(BaseModel):
    wallet_address: Optional[str] = Field(default=None)
    useConnectedWallet: bool
    chain: Optional[Literal["ethereum", "solana"]] = Field(default=None)

    @model_validator(mode="after")
    def check_fields(self) -> "WalletRequestModel":
        if not self.useConnectedWallet:
            if not self.wallet_address:
                raise ValueError("wallet_address is required when useConnectedWallet is false")
            if not self.chain:
                raise ValueError("chain is required when useConnectedWallet is false")
        return self



@router.post("/wallet")
async def fetch_wallet_data(
    body: WalletRequestModel,
    request: Request,
    current_user=Depends(get_current_user)
):
    try:
        # Handle connected wallet logic
        if body.useConnectedWallet:
            wallet_addresses = current_user.get("wallet_addresses", [])
            primary_wallet = next((w for w in wallet_addresses if w.get("is_primary")), None)
            if not primary_wallet:
                raise HTTPException(status_code=400, detail="No primary wallet found in connected wallets")

            address = primary_wallet.get("address", "").strip()
            chain = primary_wallet.get("chain", "").lower()
            if chain not in ["ethereum", "solana"]:
                raise HTTPException(status_code=400, detail="Invalid or missing chain in primary wallet")
        else:
            address = body.wallet_address.strip()
            chain = body.chain.lower()

        userId = current_user.get("id")
        tier = current_user.get("subscription_tier", "free")

        # Generate Redis cache key
        cache_key = f"wallet_cache:{userId}:{tier}:{chain}:{address}"

        # Check Redis cache first
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            print("✅ Returning wallet data from cache")
            return json.loads(cached_data)

        # Validate address by chain
        if chain == "ethereum":
            if not is_valid_ethereum_address(address):
                raise HTTPException(status_code=400, detail="Invalid Ethereum address")
            data = await analyze_ethereum_wallet_endpoint(request, userId, chain, address, tier)

        elif chain == "solana":
            if not is_valid_solana_address(address):
                raise HTTPException(status_code=400, detail="Invalid Solana address")
            data = await analyze_solana_wallet_endpoint(request, userId, chain, address, tier)

        else:
            raise HTTPException(status_code=400, detail="Unsupported chain")

        # ✅ Cache result for future requests
        ttl = 60 if tier == "pro" else 180  
        await redis_client.set(cache_key, json.dumps(data), ex=ttl)

        print("⚙️ Cached wallet data for:", cache_key)
        return data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/simple/solana")
async def fetch_solana_wallet_simple(address: str = Query(...)):
    address = address.strip()
    cache_key = f"wallet_cache:{address}"
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        print("✅ Returning wallet data from cache")
        return json.loads(cached_data) 
    data = await analyze_solana_wallet_endpoint2(address)
    await redis_client.set(cache_key, json.dumps(data), ex=60)
    print("⚙️ Cached wallet data for:", cache_key)
    return data

@router.get("/simple/ethereum")
async def fetch_ethereum_wallet_simple(address: str = Query(...)):
    address = address.strip()
    cache_key = f"wallet_cache:{address}"
    cached_data = await redis_client.get(cache_key)
    if cached_data:
        print("✅ Returning wallet data from cache")
        return json.loads(cached_data) 
    data = await analyze_ethereum_wallet_endpoint2(address)
    await redis_client.set(cache_key, json.dumps(data), ex=60)
    print("⚙️ Cached wallet data for:", cache_key)
    return data

    

    
