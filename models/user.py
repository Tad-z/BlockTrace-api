from typing import Optional
from fastapi import File, UploadFile, Form
from pydantic import BaseModel, Field


# User model
class UserModel(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    blocktrace_id: Optional[str] = None
    email: str
    supabase_id: str
    wallet_addresses: list[dict] = Field(default_factory=list)
    subscription_tier: str = "free"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # class Config:
    #     allow_population_by_field_name = True
    #     json_encoders = {
    #         str: lambda v: str(v) if v else None,
    #     }
# {
#     "_id": ObjectId,
#     "email": str,  # from Google OAuth
#     "google_id": str,  # from Google OAuth
#     "wallet_addresses": [
#         {
#             "address": str,
#             "chain": str,  # "ethereum", "solana"
#             "verified": bool,
#             "added_at": datetime,
#             "is_primary": bool
#         }
#     ],
#     "subscription_tier": str,
#     "created_at": datetime,
#     "updated_at": datetime
# }

# Authentication challenges (temporary)
class AuthChallengeModel(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    challenge: str
    wallet_address: str
    chain: str
    expires_at: Optional[str] = None
    used: bool = False

#     class Config:
#         json_encoders = {
#             str: lambda v: str(v) if v else None,
#         }
# {
#     "_id": ObjectId,
#     "challenge": str,  # random message to sign
#     "wallet_address": str,
#     "chain": str,
#     "expires_at": datetime,
#     "used": bool
# }