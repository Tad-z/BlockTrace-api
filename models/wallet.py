from typing import Optional
from fastapi import File, UploadFile, Form
from pydantic import BaseModel, Field

class WalletChallengeRequest(BaseModel):
    wallet_address: str
    chain: str  # "ethereum" or "solana"

class WalletChallengeResponse(BaseModel):
    challenge: Optional[str] = None
    expires_in: Optional[int] = 0
    already_linked: Optional[bool] = False
    message: str


class WalletVerifyRequest(BaseModel):
    wallet_address: str
    chain: str
    signature: str

class WalletVerifyResponse(BaseModel):
    success: bool
    message: str