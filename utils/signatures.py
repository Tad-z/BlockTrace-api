# utils/signatures.py
from eth_account.messages import encode_defunct
from eth_account import Account
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
        verify_key = VerifyKey(bytes.fromhex(address))
        verify_key.verify(message.encode(), base64.b64decode(signature))
        return True
    except (BadSignatureError, Exception):
        return False
