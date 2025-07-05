# utils/supabase_auth.py
import requests
from jose import jwt
from jose.exceptions import JWTError, ExpiredSignatureError
from functools import lru_cache

SUPABASE_PROJECT_ID = "<your-project-id>"
SUPABASE_JWKS_URL = f"https://{SUPABASE_PROJECT_ID}.supabase.co/auth/v1/keys"
SUPABASE_AUDIENCE = SUPABASE_PROJECT_ID  # Usually same as project ID

@lru_cache()
def fetch_supabase_jwks():
    res = requests.get(SUPABASE_JWKS_URL)
    res.raise_for_status()
    return res.json()

def verify_supabase_token(token: str):
    jwks = fetch_supabase_jwks()
    try:
        unverified_header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == unverified_header["kid"]), None)
        if not key:
            raise JWTError("Public key not found")

        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=SUPABASE_AUDIENCE,
            options={"verify_exp": True}
        )
        return payload  # Contains sub, email, etc.
    except ExpiredSignatureError:
        raise ValueError("Token expired")
    except JWTError as e:
        raise ValueError(f"Invalid token: {str(e)}")
