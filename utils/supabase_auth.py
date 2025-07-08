from functools import lru_cache
import requests
from jose import jwt, jwk
from jose.exceptions import ExpiredSignatureError, JWTError
from dotenv import load_dotenv
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET") 

SUPABASE_AUDIENCE = "authenticated"
SUPABASE_ISSUER = f"{SUPABASE_URL}/auth/v1"
def verify_supabase_token(token: str):
    """Verify Supabase JWT token locally using the JWT Secret."""
    if not SUPABASE_JWT_SECRET:
        raise ValueError("SUPABASE_JWT_SECRET environment variable is not set. "
                         "This is required for backend token verification.")

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET, 
            algorithms=["HS256"], 
            audience=SUPABASE_AUDIENCE,
            issuer=SUPABASE_ISSUER,
            options={
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "require_exp": True, 
                "require_aud": True, 
                "require_iss": True, 
            }
        )
        print("Token verified successfully:", payload)
        return payload

    except ExpiredSignatureError:
        raise JWTError("Token expired")
    except JWTError as e:
        raise JWTError(f"Invalid token: {str(e)}")
    except Exception as e:
        raise JWTError(f"An unexpected error occurred during token verification: {str(e)}")