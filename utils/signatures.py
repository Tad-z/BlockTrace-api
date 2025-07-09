# utils/signatures.py
from eth_account.messages import encode_defunct
from eth_account import Account
import base58
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import base64

def verify_ethereum_signature(address: str, message: str, signature: str) -> bool:
    try:
        encoded_msg = encode_defunct(text=message)
        recovered_address = Account.recover_message(encoded_msg, signature=signature)
        return recovered_address.lower() == address.lower()
    except:
        return False



def verify_solana_signature(address: str, message: str, signature: str) -> bool:
    try:
        # Convert base58 Solana public key to bytes
        pubkey_bytes = base58.b58decode(address)
        verify_key = VerifyKey(pubkey_bytes)

        # Signature is base64 from frontend, decode it
        signature_bytes = base64.b64decode(signature)

        # Message must match exactly, as UTF-8
        verify_key.verify(message.encode("utf-8"), signature_bytes)
        return True
    except (BadSignatureError, Exception) as e:
        print("Solana signature verification failed:", e)
        return False

