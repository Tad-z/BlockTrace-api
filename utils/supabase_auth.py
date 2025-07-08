from functools import lru_cache
import requests
from jose import jwt, jwk
from jose.exceptions import ExpiredSignatureError, JWTError
from dotenv import load_dotenv
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://olqqqjqhslvvjjrodxrc.supabase.co")
# This is the crucial part: Get your JWT_SECRET from Supabase Dashboard
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET") 

SUPABASE_AUDIENCE = "authenticated"
SUPABASE_ISSUER = f"{SUPABASE_URL}/auth/v1" # This is usually the issuer for Supabase tokens

def verify_supabase_token(token: str):
    """Verify Supabase JWT token locally using the JWT Secret."""
    if not SUPABASE_JWT_SECRET:
        raise ValueError("SUPABASE_JWT_SECRET environment variable is not set. "
                         "This is required for backend token verification.")

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET, # Use the shared secret directly
            algorithms=["HS256"], # Supabase uses HS256 for user tokens
            audience=SUPABASE_AUDIENCE,
            issuer=SUPABASE_ISSUER,
            options={
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "require_exp": True, # Ensure 'exp' claim is present
                "require_aud": True, # Ensure 'aud' claim is present
                "require_iss": True, # Ensure 'iss' claim is present
            }
        )
        print("Token verified successfully:", payload)
        return payload

    except ExpiredSignatureError:
        raise JWTError("Token expired")
    except JWTError as e:
        # Catches general JWT errors, including invalid signature or claims
        raise JWTError(f"Invalid token: {str(e)}")
    except Exception as e:
        # Catch any unexpected errors
        raise JWTError(f"An unexpected error occurred during token verification: {str(e)}")