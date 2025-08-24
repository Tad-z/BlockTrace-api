import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os
from models.wallet_query_logs import WalletQueryLogs
from fastapi import  Request, HTTPException
from db import get_db

# Subscription tier limits
SUBSCRIPTION_LIMITS = {
    "free": {
        "time_range_days": 7,
        "daily_address_limit": 5,
        "max_transactions": 50,
        "graph_types": ["force_directed"],
        "graph_depth": 1,
        "export_enabled": False
    },
    "pro": {
        "time_range_days": 180,  # 6 months
        "daily_address_limit": 50,
        "max_transactions": 500,
        "graph_types": ["force_directed", "flow", "timeline", "sankey"],
        "graph_depth": 3,
        "export_enabled": True
    }
}

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

# Cache to store already fetched prices
_token_price_cache: Dict[str, float] = {}

def get_subscription_limits(tier: str) -> Dict:
    """Get limits based on subscription tier"""
    return SUBSCRIPTION_LIMITS.get(tier.lower(), SUBSCRIPTION_LIMITS["free"])

def calculate_time_range(tier: str) -> Optional[datetime]:
    """Calculate the earliest timestamp allowed based on tier"""
    limits = get_subscription_limits(tier)
    days_back = limits["time_range_days"]
    
    earliest_time = datetime.now() - timedelta(days=days_back)
    return earliest_time

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
    return lamports / 1e9

def get_solana_signatures(address: str, limit: int = 100, before: str = None, until: str = None):
    """Get transaction signatures for an address with optional pagination"""
    params = [address, {"limit": limit}]
    
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
        
    meta = tx_data.get("meta", {})
    message = tx_data.get("transaction", {}).get("message", {})
    instructions = message.get("instructions", [])
    
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
        for address, change in balance_changes.items():
            if address != user_address and abs(change) > 0:
                if (user_balance_change > 0 and change < 0) or (user_balance_change < 0 and change > 0):
                    direction = "incoming" if user_balance_change > 0 else "outgoing"
                    source = address if direction == "incoming" else user_address
                    destination = user_address if direction == "incoming" else address
                    
                    transfers.append({
                        "source": source,
                        "destination": destination,
                        "amount": abs(change) / 1e9,
                        "direction": direction,
                        "token": "SOL",
                        "token_address": None
                    })
                    break
    
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
            
            source_owner = info.get("authority") or info.get("source")
            dest_owner = info.get("destination")
            
            if parsed.get("type") == "transferChecked":
                token_amount = info.get("tokenAmount", {})
                amount = float(token_amount.get("uiAmount", 0))
                token_address = info.get("mint")
                decimals = token_amount.get("decimals", 0)
            else:
                amount = int(info.get("amount", 0))
                decimals = info.get("decimals", 0)
                amount = amount / (10 ** decimals) if decimals > 0 else amount
                token_address = info.get("mint", "UNKNOWN")
            
            if source_owner == user_address or dest_owner == user_address:
                direction = "outgoing" if source_owner == user_address else "incoming"
            
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

def filter_transactions_by_time(transactions: List[Dict], earliest_time: datetime) -> List[Dict]:
    """Filter transactions based on time range allowed by tier"""
    filtered = []
    earliest_timestamp = earliest_time.timestamp()
    print("timestamp", earliest_timestamp)
    
    for tx in transactions:
        if tx.get("timestamp"):
            tx_time = datetime.fromisoformat(tx["timestamp"].replace('Z', '+00:00'))
            print("tx time", tx_time.timestamp())
            if tx_time.timestamp() >= earliest_timestamp:
                filtered.append(tx)
    
    return filtered

def get_solana_transactions_for_graph(address: str, tier: str) -> List[Dict]:
    """
    Get transaction data formatted for graph visualization with tier-based limits
    """
    try:
        limits = get_subscription_limits(tier)
        max_transactions = limits["max_transactions"]
        earliest_time = calculate_time_range(tier)
        
        # Get more signatures than limit to account for filtering
        fetch_limit = min(max_transactions * 2, 1000)  # Get extra in case some are filtered out
        signatures = get_solana_signatures(address, fetch_limit)
        
        transactions = []
        seen_transfers = set()
        processed_count = 0
        
        for sig_obj in signatures:
            # Stop if we've reached the transaction limit after filtering
            if len(transactions) >= max_transactions:
                break
                
            signature = sig_obj.get("signature")
            block_time = sig_obj.get("blockTime")
            
            # Skip if no block time or outside time range
            if not block_time:
                continue
                
            tx_datetime = datetime.fromtimestamp(block_time)
            if tx_datetime < earliest_time:
                continue  # Skip transactions outside time range
            
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
                "timestamp": tx_datetime.isoformat(),
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

                    # Calculate USD equivalent
                    usd_equivalent = 0
                    if transfer["token"] == "SOL":
                        usd_equivalent = transfer["amount"] * get_token_price_usd("SOL")
                    else:
                        token_info = SPL_TOKENS.get(transfer["token_address"])
                        if token_info:
                            symbol = token_info["symbol"]
                            usd_equivalent = transfer["amount"] * get_token_price_usd(symbol)

                    transaction_data = {
                        **base_tx_info,
                        **transfer,
                        "usd_equivalent": usd_equivalent
                    }
                    
                    transactions.append(transaction_data)
                    
                    # Check if we've hit the limit
                    if len(transactions) >= max_transactions:
                        break
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
            
            processed_count += 1

        return transactions[:max_transactions]  # Ensure we don't exceed limit

    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

def get_wallet_graph_data(address: str, tier: str = "free"):
    """
    Main function to get all graph data for a wallet address with tier-based limits
    """
    try:
        limits = get_subscription_limits(tier)
        
        # Get balance
        balance = get_solana_balance(address)
        
        # Get transactions with tier limits
        transactions = get_solana_transactions_for_graph(address, tier)
        
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
                "weight": tx["amount"]
            }
            
            edges.append(edge)
        
        # Calculate summary statistics
        sol_transactions = [tx for tx in transactions if tx["token"] == "SOL"]
        
        # Prepare final response with tier information
        response = {
            "wallet_address": address,
            "balance": balance,
            "subscription_tier": tier.lower(),
            "tier_limits": limits,
            "total_transactions": len(transactions),
            "graph_data": {
                "nodes": list(nodes.values()),
                "edges": edges
            },
            "summary": {
                "total_incoming": len([tx for tx in transactions if tx["direction"] == "incoming"]),
                "total_outgoing": len([tx for tx in transactions if tx["direction"] == "outgoing"]),
                "total_solana_volume": sum([tx["amount"] for tx in sol_transactions]),
                "unique_addresses": len(nodes) - 1,
                "date_range": {
                    "earliest": min([tx["timestamp"] for tx in transactions if tx["timestamp"]]) if transactions else None,
                    "latest": max([tx["timestamp"] for tx in transactions if tx["timestamp"]]) if transactions else None,
                    "allowed_days_back": limits["time_range_days"]
                },
                "tokens_found": list(set([tx["token"] for tx in transactions])),
                "limitations_applied": {
                    "time_limited": len(transactions) > 0,  # Will be true if any filtering occurred
                    "transaction_limited": len(transactions) == limits["max_transactions"]
                }
            }
        }
        
        return response
        
    except Exception as e:
        return {
            "error": str(e),
            "wallet_address": address,
            "subscription_tier": tier.lower(),
            "graph_data": {"nodes": [], "edges": []},
            "summary": {}
        }

def get_token_price_usd(symbol: str) -> float:
    """
    Fetch USD price for a given token symbol using CoinGecko.
    Uses cache to avoid redundant API calls during the same run.
    """
    if symbol in _token_price_cache:
        return _token_price_cache[symbol]

    try:
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
            "PENGU": "pudgy-penguins"
        }

        coingecko_id = symbol_map.get(symbol)
        if not coingecko_id:
            return 0

        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        price = data.get(coingecko_id, {}).get("usd", 0)

        _token_price_cache[symbol] = price
        return price

    except Exception as e:
        print(f"Price fetch failed for {symbol}: {e}")
        return 0


def get_day_bounds():
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    return start, end


# Main endpoint function
async def analyze_solana_wallet_endpoint(request: Request, userId, chain: str, wallet_address: str, tier: str = "free"):
    db = get_db(request.app)

    if not wallet_address:
        return {"error": "Wallet address is required"}

    # Validate tier
    if tier.lower() not in ["free", "pro"]:
        return {"error": "Invalid tier. Must be 'free' or 'pro'"}
    
    # daily address limits
    free_daily_limit = SUBSCRIPTION_LIMITS["free"]["daily_address_limit"]
    pro_daily_limit = SUBSCRIPTION_LIMITS["pro"]["daily_address_limit"]

    # Check rate limit **before** logging
    if tier.lower() == "free":
        start, end = get_day_bounds()

        used_addresses = await db["wallet_query_logs"].distinct(
            "wallet_address",
            {
                "userId": userId,
                "tier": "free",
                "created_at": {"$gte": start.isoformat(), "$lt": end.isoformat()}
            }
        )

        if wallet_address.lower() not in used_addresses and len(used_addresses) >= free_daily_limit:
            raise HTTPException(status_code=403, detail="Free tier daily limit reached.")
        
    if tier.lower() == "pro":
        start, end = get_day_bounds()

        used_addresses = await db["wallet_query_logs"].distinct(
            "wallet_address",
            {
                "userId": userId,
                "tier": "pro",
                "created_at": {"$gte": start.isoformat(), "$lt": end.isoformat()}
            }
        )

        if wallet_address.lower() not in used_addresses and len(used_addresses) >= pro_daily_limit:
            raise HTTPException(status_code=403, detail="Pro tier daily limit reached.")
        
    
        

    # Log query after passing check
    await db["wallet_query_logs"].insert_one(WalletQueryLogs.from_query(
        user_id=userId,
        wallet_address=wallet_address,
        tier=tier,
        chain=chain
    ).dict())

    # Return the graph data
    response = get_wallet_graph_data(wallet_address, tier)

    if tier.lower() == "free":
        response["rate_limit_info"] = {
            "addresses_used_today": len(used_addresses) + (1 if wallet_address.lower() not in used_addresses else 0),
            "daily_limit": free_daily_limit,
            "remaining": max(0, 5 - len(used_addresses) - (1 if wallet_address.lower() not in used_addresses else 0))
        }

    if tier.lower() == "pro":
        response["rate_limit_info"] = {
            "addresses_used_today": len(used_addresses) + (1 if wallet_address.lower() not in used_addresses else 0),
            "daily_limit": pro_daily_limit,
            "remaining": max(0, 50 - len(used_addresses) - (1 if wallet_address.lower() not in used_addresses else 0))
        }

    return response

def analyze_solana_wallet_endpoint2(wallet_address: str):
    if not wallet_address:
        return {"error": "Wallet address is required"}
    return get_wallet_graph_data(wallet_address)
