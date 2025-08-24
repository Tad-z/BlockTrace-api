from typing import Optional
from fastapi import File, UploadFile, Form
from pydantic import BaseModel, Field
from datetime import datetime

class WalletQueryLogs(BaseModel):
    userId: str
    wallet_address: str
    tier: str
    chain: str  
    created_at: str

    @classmethod
    def from_query(cls, user_id: str, wallet_address: str, tier:str, chain: str):
        return cls(
            userId=user_id,
            wallet_address=wallet_address.lower(),
            tier=tier,
            chain=chain.lower(),
            created_at=datetime.utcnow().isoformat()
        )