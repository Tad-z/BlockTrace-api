# main.py
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from db import get_db
from utils.auth import get_current_user
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timedelta
from routes.wallet import is_valid_solana_address, is_valid_ethereum_address
from utils.solana import analyze_wallet_endpoint, fetch_solana_wallet_data

router = APIRouter()

class WalletRequestModel(BaseModel):
    wallet_address: Optional[str] = Field(None)
    useConnectedWallet: bool = Field(...)
    chain: Literal["ethereum", "solana"] = Field(...)

@router.post("/wallet")
async def fetch_wallet_data(
    body: WalletRequestModel, 
    request: Request, 
    current_user=Depends(get_current_user)
):
    chain = body.chain

    # Resolve address
    if body.useConnectedWallet:
        wallet_addresses = current_user.get("wallet_addresses", [])
        primary_wallet = next((w for w in wallet_addresses if w.get("is_primary")), None)
        if not primary_wallet:
            raise HTTPException(status_code=400, detail="No primary wallet found in connected wallets")
        address = primary_wallet.get("address", "").strip()
    else:
        if not body.wallet_address:
            raise HTTPException(status_code=400, detail="wallet_address is required when useConnectedWallet is false")
        address = body.wallet_address.strip()

    # Validate chain + address
    if chain == "ethereum":
        if not is_valid_ethereum_address(address):
            raise HTTPException(status_code=400, detail="Invalid Ethereum address")
        # Placeholder for Ethereum logic (Alchemy or Etherscan)
        return {"message": "Ethereum support coming soon"}

    elif chain == "solana":
        if not is_valid_solana_address(address):
            raise HTTPException(status_code=400, detail="Invalid Solana address")
        data = analyze_wallet_endpoint(address)
        return data

    else:
        raise HTTPException(status_code=400, detail="Unsupported chain")
    
@router.get("/simple")
def fetch_wallet_data(address: str = Query(...)):
    address = address.strip()  # ✅ remove leading/trailing whitespace

    data = analyze_wallet_endpoint(address)
    return data
    

    
