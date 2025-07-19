import os
import requests
from dotenv import load_dotenv
from typing import Dict
from routes.wallet import is_valid_solana_address
from fastapi import Request, Depends, Header, HTTPException
from datetime import datetime, timedelta

load_dotenv()

ALCHEMY_URL = os.getenv("ALCHEMY_SOLANA_URL")

SPL_TOKENS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {"symbol": "USDC", "name": "USD Coin"},
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": {"symbol": "USDT", "name": "Tether USD"},
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": {"symbol": "BONK", "name": "Bonk"},
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": {"symbol": "JUP", "name": "Jupiter"},
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {"symbol": "RAY", "name": "Raydium"},
    "MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey": {"symbol": "MNDE", "name": "Marinade"},
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj": {"symbol": "stSOL", "name": "Lido Staked SOL"},
    "So11111111111111111111111111111111111111112": {"symbol": "WSOL", "name": "Wrapped SOL"},
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv": {"symbol": "PENGU", "name": "Pudgy Penguins"}
}


def fetch_solana_wallet_data(address):
    address = address.strip()
    valid = is_valid_solana_address(address)

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid Solana address")
    
    balance = get_solana_balance(address)
    transactions = get_solana_transactions(address)

    return {
        "wallet": address,
        "balanceSOL": balance,
        "recentTransactions": transactions
    }

# def get_solana_balance(address: str) -> float:
#     payload = {
#         "jsonrpc": "2.0",
#         "id": 1,
#         "method": "getBalance",
#         "params": [address]
#     }

#     response = requests.post(ALCHEMY_URL, json=payload)
#     getResult = response.json()
#     result = getResult.get("result", {})
#     lamports = result.get("value", 0)
#     return lamports / 1e9  

def get_solana_transactions(address: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address]
    }

    response = requests.post(ALCHEMY_URL, json=payload)
    getResult = response.json()
    result = getResult.get("result", [])
    return result

def get_solana_balance(address: str) -> float:
    """Get current SOL balance for an address"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    
    response = requests.post(ALCHEMY_URL, json=payload)
    result = response.json()
    
    if "error" in result:
        raise Exception(f"API Error: {result['error']}")
        
    lamports = result.get("result", {}).get("value", 0)
    return lamports / 1e9  # convert lamports to SOL

def get_solana_signatures(address: str, limit: int = 100, before: str = None, until: str = None):
    """Get transaction signatures for an address with optional pagination"""
    params = [address, {"limit": limit}]
    
    # Add pagination and filtering options for time-based queries
    if before:
        params[1]["before"] = before
    if until:
        params[1]["until"] = until
        
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": params
    }
    
    response = requests.post(ALCHEMY_URL, json=payload)
    result = response.json()
    
    if "error" in result:
        raise Exception(f"API Error: {result['error']}")
        
    return result.get("result", [])

def get_solana_transaction_details(signature: str):
    """Get detailed transaction information"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature, 
            {
                "encoding": "jsonParsed",
                "commitment": "finalized",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }
    
    response = requests.post(ALCHEMY_URL, json=payload)
    result = response.json()
    
    if "error" in result:
        print(f"Error fetching transaction {signature}: {result['error']}")
        return None
        
    return result.get("result")

def parse_transaction_transfers(tx_data, user_address: str):
    """Parse transaction data to extract all transfer information"""
    transfers = []
    
    if not tx_data or not tx_data.get("transaction"):
        return transfers
        
    # Get transaction metadata
    meta = tx_data.get("meta", {})
    message = tx_data.get("transaction", {}).get("message", {})
    instructions = message.get("instructions", [])
    
    # Parse pre and post balances to detect SOL transfers
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    account_keys = message.get("accountKeys", [])
    
    # Track balance changes for SOL transfers
    balance_changes = {}
    for i, account_key in enumerate(account_keys):
        if isinstance(account_key, dict):
            account_address = account_key.get("pubkey")
        else:
            account_address = account_key
            
        if i < len(pre_balances) and i < len(post_balances):
            balance_change = post_balances[i] - pre_balances[i]
            if abs(balance_change) > 0:
                balance_changes[account_address] = balance_change
    
    # Find SOL transfers based on balance changes
    user_balance_change = balance_changes.get(user_address, 0)
    if abs(user_balance_change) > 0:
        # Find counterparties with opposite balance changes
        for address, change in balance_changes.items():
            if address != user_address and abs(change) > 0:
                # Check if this could be a transfer counterparty
                if (user_balance_change > 0 and change < 0) or (user_balance_change < 0 and change > 0):
                    direction = "incoming" if user_balance_change > 0 else "outgoing"
                    source = address if direction == "incoming" else user_address
                    destination = user_address if direction == "incoming" else address
                    
                    transfers.append({
                        "source": source,
                        "destination": destination,
                        "amount": abs(change) / 1e9,  # Convert lamports to SOL
                        "direction": direction,
                        "token": "SOL",
                        "token_address": None
                    })
                    break  # Only take the first valid counterparty to avoid duplicates
    
    # Parse instruction-level transfers
    for instruction in instructions:
        parsed = instruction.get("parsed", {})
        program = instruction.get("program")
        
        # Handle system program transfers (SOL)
        if parsed.get("type") == "transfer" and program == "system":
            info = parsed.get("info", {})
            source = info.get("source")
            destination = info.get("destination")
            amount = int(info.get("lamports", 0)) / 1e9
            
            if source == user_address or destination == user_address:
                direction = "outgoing" if source == user_address else "incoming"
                transfers.append({
                    "source": source,
                    "destination": destination,
                    "amount": amount,
                    "direction": direction,
                    "token": "SOL",
                    "token_address": None
                })
        
        # Handle SPL token transfers
        elif program == "spl-token" and parsed.get("type") in ["transfer", "transferChecked"]:
            info = parsed.get("info", {})
            
            # Get source and destination from token accounts
            source_owner = info.get("authority") or info.get("source")
            dest_owner = info.get("destination")
            
            # For transferChecked, we have more detailed info
            if parsed.get("type") == "transferChecked":
                token_amount = info.get("tokenAmount", {})
                amount = float(token_amount.get("uiAmount", 0))
                token_address = info.get("mint")
                decimals = token_amount.get("decimals", 0)
            else:
                # For regular transfer, we need to calculate amount
                amount = int(info.get("amount", 0))
                decimals = info.get("decimals", 0)
                amount = amount / (10 ** decimals) if decimals > 0 else amount
                token_address = info.get("mint", "UNKNOWN")
            
            # Determine if user is involved
            if source_owner == user_address or dest_owner == user_address:
                direction = "outgoing" if source_owner == user_address else "incoming"
            
                # Lookup symbol from SPL_TOKENS or fallback
                token_info = SPL_TOKENS.get(token_address)
                token_symbol = token_info["symbol"] if token_info else "SPL Token"

                
                transfers.append({
                    "source": source_owner,
                    "destination": dest_owner,
                    "amount": amount,
                    "direction": direction,
                    "token": token_symbol,
                    "token_address": token_address
                })
    
    return transfers

def get_solana_transactions_for_graph(address: str, limit: int = 5):
    """
    Get transaction data formatted for graph visualization
    Returns all the data needed for frontend graph rendering
    """
    try:
        signatures = get_solana_signatures(address, limit)
        transactions = []
        seen_transfers = set()

        for sig_obj in signatures:
            signature = sig_obj.get("signature")
            block_time = sig_obj.get("blockTime")
            slot = sig_obj.get("slot")
            confirmation_status = sig_obj.get("confirmationStatus", "finalized")

            tx_data = get_solana_transaction_details(signature)
            if not tx_data:
                continue

            transfers = parse_transaction_transfers(tx_data, address)
            meta = tx_data.get("meta", {})
            fee = meta.get("fee", 0) / 1e9
            status = "success" if meta.get("err") is None else "failed"

            base_tx_info = {
                "hash": signature,
                "timestamp": datetime.fromtimestamp(block_time).isoformat() if block_time else None,
                "chain": "Solana",
                "fee": fee,
                "status": status,
                "slot": slot,
                "confirmation_status": confirmation_status
            }

            if transfers:
                for transfer in transfers:
                    transfer_key = (
                        signature,
                        transfer["source"],
                        transfer["destination"],
                        transfer["amount"],
                        transfer["token"]
                    )
                    if transfer_key in seen_transfers:
                        continue
                    seen_transfers.add(transfer_key)

                    usd_equivalent = 0
                    if transfer["token"] == "SOL":
                        usd_equivalent = transfer["amount"] * get_token_price_usd("SOL")
                    else:
                        token_info = SPL_TOKENS.get(transfer["token_address"])
                        if token_info:
                            symbol = token_info["symbol"]
                            usd_equivalent = transfer["amount"] * get_token_price_usd(symbol)

                    transactions.append({
                        **base_tx_info,
                        **transfer,
                        "usd_equivalent": usd_equivalent
                    })
            else:
                transactions.append({
                    **base_tx_info,
                    "source": address,
                    "destination": "System Program",
                    "amount": 0,
                    "direction": "interaction",
                    "token": "SOL",
                    "token_address": None,
                    "usd_equivalent": 0
                })

        return transactions

    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []


def get_wallet_graph_data(address: str, limit: int = 5):
    """
    Main function to get all graph data for a wallet address
    This is what your endpoint should call
    """
    try:
        # Get balance
        balance = get_solana_balance(address)
        
        # Get transactions
        transactions = get_solana_transactions_for_graph(address, limit)
        
        # Build nodes and edges for graph
        nodes = {}
        edges = []
        
        # Add the main wallet as a node
        nodes[address] = {
            "id": address,
            "label": f"{address[:8]}...{address[-8:]}",
            "type": "main_wallet",
            "balance": balance,
            "full_address": address
        }
        
        # Process transactions to build graph
        for tx in transactions:
            source = tx["source"]
            destination = tx["destination"]
            
            # Add source node if not exists
            if source not in nodes and source != address:
                nodes[source] = {
                    "id": source,
                    "label": f"{source[:8]}...{source[-8:]}" if len(source) > 16 else source,
                    "type": "external_wallet",
                    "full_address": source
                }
            
            # Add destination node if not exists
            if destination not in nodes and destination != address:
                nodes[destination] = {
                    "id": destination,
                    "label": f"{destination[:8]}...{destination[-8:]}" if len(destination) > 16 else destination,
                    "type": "external_wallet",
                    "full_address": destination
                }
            
            # Create edge (represents transaction)
            edge = {
                "source": source,
                "destination": destination,
                "transaction_hash": tx["hash"],
                "timestamp": tx["timestamp"],
                "amount": tx["amount"],
                "token": tx["token"],
                "token_address": tx.get("token_address"),
                "direction": tx["direction"],
                "usd_equivalent": tx.get("usd_equivalent", 0),
                "fee": tx["fee"],
                "status": tx["status"],
                "chain": tx["chain"],
                "weight": tx["amount"]  # For graph layout algorithms
            }
            
            edges.append(edge)
        
        # Prepare final response
        response = {
            "wallet_address": address,
            "balance": balance,
            "total_transactions": len(transactions),
            "graph_data": {
                "nodes": list(nodes.values()),
                "edges": edges
            },
            "summary": {
                "total_incoming": len([tx for tx in transactions if tx["direction"] == "incoming"]),
                "total_outgoing": len([tx for tx in transactions if tx["direction"] == "outgoing"]),
                "total_solana_volume": sum([tx["amount"] for tx in transactions if tx["token"] == "SOL"]),
                "unique_addresses": len(nodes) - 1,  # Exclude main wallet
                "date_range": {
                    "earliest": min([tx["timestamp"] for tx in transactions if tx["timestamp"]]) if transactions else None,
                    "latest": max([tx["timestamp"] for tx in transactions if tx["timestamp"]]) if transactions else None
                }
            }
        }
        
        return response
        
    except Exception as e:
        return {
            "error": str(e),
            "wallet_address": address,
            "graph_data": {"nodes": [], "edges": []},
            "summary": {}
        }


# Cache to store already fetched prices
_token_price_cache: Dict[str, float] = {}

def get_token_price_usd(symbol: str) -> float:
    """
    Fetch USD price for a given token symbol using CoinGecko.
    Uses cache to avoid redundant API calls during the same run.
    Falls back to 0 if the token is not found or API fails.
    """
    if symbol in _token_price_cache:
        return _token_price_cache[symbol]

    try:
        # CoinGecko uses lowercase ids
        symbol_map = {
            "SOL": "solana",
            "USDC": "usd-coin",
            "USDT": "tether",
            "BONK": "bonk",
            "JUP": "jupiter",
            "RAY": "raydium",
            "MNDE": "marinade",
            "stSOL": "lido-staked-sol",
            "WSOL": "solana",
            "PENGU": "penguin"
        }

        coingecko_id = symbol_map.get(symbol)
        if not coingecko_id:
            return 0

        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        price = data.get(coingecko_id, {}).get("usd", 0)

        _token_price_cache[symbol] = price  # Cache the result
        return price

    except Exception as e:
        print(f"Price fetch failed for {symbol}: {e}")
        return 0


# Example usage for your endpoint
def analyze_wallet_endpoint(wallet_address: str, transaction_limit: int = 5):
    """
    This is the main function you'd call from your FastAPI/Flask endpoint
    """
    if not wallet_address:
        return {"error": "Wallet address is required"}
    
    return get_wallet_graph_data(wallet_address, transaction_limit)
