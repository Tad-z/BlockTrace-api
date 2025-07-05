from functools import lru_cache
import requests
from jose import jwt, jwk
from jose.exceptions import JWTError, ExpiredSignatureError

SUPABASE_JWKS_URL = "https://<your-project>.supabase.co/auth/v1/keys"
SUPABASE_AUDIENCE = "<your-project-id>"  # or 'authenticated' if default

@lru_cache()
def fetch_supabase_jwks():
    res = requests.get(SUPABASE_JWKS_URL)
    res.raise_for_status()
    return res.json()

def verify_supabase_token(token: str):
    jwks = fetch_supabase_jwks()
    try:
        unverified_header = jwt.get_unverified_header(token)
        # Find the key in JWKS that matches the 'kid' from the token header
        key_dict = next((k for k in jwks["keys"] if k["kid"] == unverified_header["kid"]), None)
        if not key_dict:
            raise JWTError("Public key not found")

        key = jwk.construct(key_dict, unverified_header.get("alg", "RS256"))

        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=SUPABASE_AUDIENCE,
            options={"verify_exp": True}
        )
        return payload  # Contains fields like `sub`, `email`, etc.

    except ExpiredSignatureError:
        raise JWTError("Token expired")
    except JWTError as e:
        raise JWTError(f"Invalid token: {str(e)}")
