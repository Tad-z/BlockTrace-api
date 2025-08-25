# main.py
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from db import get_db
from utils.auth import get_current_user
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Literal
from datetime import datetime, timedelta
from routes.wallet import is_valid_solana_address, is_valid_ethereum_address
from utils.solana import analyze_solana_wallet_endpoint, analyze_solana_wallet_endpoint2

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
    print(current_user)
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
        chain = body.chain

    userId = current_user.get("id")
    tier = current_user.get("subscription_tier", "free")

    # Validate chain + address
    if chain == "ethereum":
        if not is_valid_ethereum_address(address):
            raise HTTPException(status_code=400, detail="Invalid Ethereum address")
        return {"message": "Ethereum support coming soon"}

    elif chain == "solana":
        if not is_valid_solana_address(address):
            raise HTTPException(status_code=400, detail="Invalid Solana address")
        data = await analyze_solana_wallet_endpoint(request, userId, chain, address, tier)  # ✅ await
        return data


    else:
        raise HTTPException(status_code=400, detail="Unsupported chain")
    
@router.get("/simple")
def fetch_wallet_data(address: str = Query(...)):
    address = address.strip()
    data = analyze_solana_wallet_endpoint2(address)
    return data

    

    
