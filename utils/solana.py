import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os
from models.wallet_query_logs import WalletQueryLogs
from fastapi import Request, HTTPException
from db import get_db
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
import time
from functools import lru_cache

# Subscription tier limits
SUBSCRIPTION_LIMITS = {
    "free": {
        "time_range_days": 7,
        "daily_address_limit": 5,
        "max_transactions": 50,
        # "graph_types": ["force_directed"],
        "graph_depth": 1,
        "export_enabled": False
    },
    "pro": {
        "time_range_days": 180,  # 6 months
        "daily_address_limit": 50,
        "max_transactions": 500,
        # "graph_types": ["force_directed", "flow", "timeline", "sankey"],
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

# Cache to store already fetched prices with TTL
_token_price_cache: Dict[str, Dict] = {}
PRICE_CACHE_TTL = 300  # 5 minutes

def get_subscription_limits(tier: str) -> Dict:
    """Get limits based on subscription tier"""
    return SUBSCRIPTION_LIMITS.get(tier.lower(), SUBSCRIPTION_LIMITS["free"])

def calculate_time_range(tier: str) -> Optional[datetime]:
    """Calculate the earliest timestamp allowed based on tier"""
    limits = get_subscription_limits(tier)
    days_back = limits["time_range_days"]
    
    earliest_time = datetime.now() - timedelta(days=days_back)
    return earliest_time

async def get_solana_balance_async(session: aiohttp.ClientSession, address: str) -> float:
    """Get current SOL balance for an address asynchronously"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address]
    }
    
    try:
        async with session.post(ALCHEMY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
            result = await response.json()
            
            if "error" in result:
                raise Exception(f"API Error: {result['error']}")
                
            lamports = result.get("result", {}).get("value", 0)
            return lamports / 1e9
    except Exception as e:
        print(f"Error getting balance for {address}: {e}")
        return 0

async def get_solana_balance(address: str) -> float:
    """Get current SOL balance for an address"""
    async with aiohttp.ClientSession() as session:
        return await get_solana_balance_async(session, address)

async def get_solana_signatures_async(session: aiohttp.ClientSession, address: str, limit: int = 100, before: str = None, until: str = None):
    """Get transaction signatures for an address with optional pagination - async version"""
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
    
    try:
        async with session.post(ALCHEMY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
            result = await response.json()
            
            if "error" in result:
                raise Exception(f"API Error: {result['error']}")
                
            return result.get("result", [])
    except Exception as e:
        print(f"Error getting signatures: {e}")
        return []

async def get_solana_transaction_details_async(session: aiohttp.ClientSession, signature: str):
    """Get detailed transaction information - async version with better error handling"""
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
    
    try:
        async with session.post(ALCHEMY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
            result = await response.json()
            
            if "error" in result:
                error_code = result.get("error", {}).get("code")
                if error_code == 429:
                    # Raise specific exception for rate limiting
                    raise Exception(f"429: Rate limited")
                else:
                    print(f"API Error fetching transaction {signature[:16]}: {result['error']}")
                    return None
                
            return result.get("result")
    except asyncio.TimeoutError:
        print(f"Timeout fetching transaction {signature[:16]}")
        return None
    except Exception as e:
        if "429" in str(e):
            raise e  # Re-raise rate limit errors for retry handling
        print(f"Error fetching transaction {signature[:16]}: {e}")
        return None

async def process_transactions_batch_async(session: aiohttp.ClientSession, signatures: List[Dict], user_address: str, max_transactions: int, earliest_time: datetime):
    """Process transactions in batches asynchronously with rate limiting and retry logic"""
    transactions = []
    seen_transfers = set()
    
    # Create semaphore to limit concurrent requests - reduced to avoid rate limits
    semaphore = asyncio.Semaphore(3)  # Much lower limit for Alchemy
    
    async def process_single_transaction_with_retry(sig_obj, max_retries=3):
        async with semaphore:
            signature = sig_obj.get("signature")
            block_time = sig_obj.get("blockTime")
            
            # Skip if no block time or outside time range
            if not block_time:
                return None
                
            tx_datetime = datetime.fromtimestamp(block_time)
            if tx_datetime < earliest_time:
                return None
                
            slot = sig_obj.get("slot")
            confirmation_status = sig_obj.get("confirmationStatus", "finalized")

            # Retry logic for rate-limited requests
            for attempt in range(max_retries):
                try:
                    tx_data = await get_solana_transaction_details_async(session, signature)
                    if tx_data:
                        break
                    elif attempt < max_retries - 1:
                        # Wait longer on each retry
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        return None
                except Exception as e:
                    if "429" in str(e) and attempt < max_retries - 1:
                        # Exponential backoff for rate limits
                        wait_time = (2 ** attempt) + (attempt * 0.5)
                        print(f"Rate limited, retrying in {wait_time}s for {signature[:16]}...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        print(f"Failed to fetch transaction {signature[:16]} after {attempt + 1} attempts")
                        return None

            if not tx_data:
                return None

            transfers = parse_transaction_transfers(tx_data, user_address)
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

            processed_transfers = []
            
            if transfers:
                for transfer in transfers:
                    transfer_key = (
                        signature,
                        transfer["source"],
                        transfer["destination"],
                        transfer["amount"],
                        transfer["token"]
                    )
                    
                    # Use thread-safe approach for seen_transfers check
                    if transfer_key not in seen_transfers:
                        seen_transfers.add(transfer_key)
                        
                        # Calculate USD equivalent (using cached prices)
                        usd_equivalent = 0
                        if transfer["token"] == "SOL":
                            usd_equivalent = transfer["amount"] * get_cached_token_price("SOL")
                        else:
                            token_info = SPL_TOKENS.get(transfer["token_address"])
                            if token_info:
                                symbol = token_info["symbol"]
                                usd_equivalent = transfer["amount"] * get_cached_token_price(symbol)

                        transaction_data = {
                            **base_tx_info,
                            **transfer,
                            "usd_equivalent": usd_equivalent
                        }
                        
                        processed_transfers.append(transaction_data)
            else:
                processed_transfers.append({
                    **base_tx_info,
                    "source": user_address,
                    "destination": "System Program",
                    "amount": 0,
                    "direction": "interaction",
                    "token": "SOL",
                    "token_address": None,
                    "usd_equivalent": 0
                })
            
            return processed_transfers
    
    # Process transactions in smaller batches with longer delays
    batch_size = 5  # Much smaller batches to avoid rate limits
    processed_count = 0
    
    for i in range(0, len(signatures), batch_size):
        if len(transactions) >= max_transactions:
            break
            
        batch = signatures[i:i + batch_size]
        
        # Process batch concurrently
        tasks = [process_single_transaction_with_retry(sig_obj) for sig_obj in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                print(f"Error processing transaction: {result}")
                continue
                
            if result:
                for tx in result:
                    if tx:
                        transactions.append(tx)
                        if len(transactions) >= max_transactions:
                            return transactions[:max_transactions]
        
        processed_count += len(batch)
        print(f"Processed batch {i//batch_size + 1}, total transactions so far: {len(transactions)}")
        
        # Longer delay between batches to respect rate limits
        if i + batch_size < len(signatures):
            await asyncio.sleep(0.5)  # Increased delay
        
        # Stop if we have enough transactions
        if len(transactions) >= max_transactions:
            break
    
    print(f"Final transaction count: {len(transactions)} out of {max_transactions} requested")
    return transactions[:max_transactions]

@lru_cache(maxsize=100)
def get_cached_token_price(symbol: str) -> float:
    """
    Get cached token price with TTL
    """
    current_time = time.time()
    
    if symbol in _token_price_cache:
        cache_entry = _token_price_cache[symbol]
        if current_time - cache_entry['timestamp'] < PRICE_CACHE_TTL:
            return cache_entry['price']
    
    # Fetch new price
    price = get_token_price_usd_sync(symbol)
    _token_price_cache[symbol] = {
        'price': price,
        'timestamp': current_time
    }
    
    return price

def get_token_price_usd_sync(symbol: str) -> float:
    """
    Synchronous version of price fetching for caching with default fallback
    """
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

        # fallback prices if 429 or API failure
        default_prices = {
            "SOL": 204.95,
            "USDC": 0.999814,
            "USDT": 1.0,
            "BONK": 0.00002276,
            "JUP": 0.00105764,
            "RAY": 3.57,
            "MNDE": 0.131998,
            "stSOL": 248.29,
            "WSOL": 206.67,
            "PENGU": 0.02901802,
        }

        coingecko_id = symbol_map.get(symbol)
        if not coingecko_id:
            return 0

        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"
        response = requests.get(url, timeout=5)

        if response.status_code == 429:
            print(f"Rate limit hit for {symbol}, using default price")
            return default_prices.get(symbol, 0)

        response.raise_for_status()
        data = response.json()
        return data.get(coingecko_id, {}).get("usd", default_prices.get(symbol, 0))

    except Exception as e:
        print(f"Price fetch failed for {symbol}: {e}")
        # fall back to default
        return default_prices.get(symbol, 0)

def prefetch_prices(symbols: list[str]):
    """
    Bulk fetch prices for multiple symbols and update cache
    """
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

    coingecko_ids = [symbol_map[s] for s in symbols if s in symbol_map]

    if not coingecko_ids:
        return

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(coingecko_ids)}&vs_currencies=usd"

    try:
        response = requests.get(url, timeout=6)
        response.raise_for_status()
        data = response.json()

        now = time.time()
        for s in symbols:
            cg_id = symbol_map.get(s)
            if cg_id and cg_id in data:
                _token_price_cache[s] = {
                    'price': data[cg_id]['usd'],
                    'timestamp': now
                }
    except Exception as e:
        print("Prefetch failed:", e)


def parse_transaction_transfers(tx_data, user_address: str):
    """Parse transaction data to extract all transfer information - optimized version"""
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
    
    # Parse instruction-level transfers (optimized)
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

async def get_solana_transactions_for_graph_async(address: str, tier: str) -> List[Dict]:
    """
    Async version of transaction fetching with optimizations
    """
    try:
        limits = get_subscription_limits(tier)
        max_transactions = limits["max_transactions"]
        earliest_time = calculate_time_range(tier)
        
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20),
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            
            # Get signatures first
            fetch_limit = min(max_transactions * 2, 1000)
            signatures = await get_solana_signatures_async(session, address, fetch_limit)
            
            # Filter signatures by time early to avoid unnecessary API calls
            filtered_signatures = []
            for sig_obj in signatures:
                block_time = sig_obj.get("blockTime")
                if block_time:
                    tx_datetime = datetime.fromtimestamp(block_time)
                    if tx_datetime >= earliest_time:
                        filtered_signatures.append(sig_obj)
                        
                # Limit early to avoid processing too many
                if len(filtered_signatures) >= max_transactions:
                    break
            
            # Process transactions asynchronously
            transactions = await process_transactions_batch_async(
                session, filtered_signatures, address, max_transactions, earliest_time
            )
            
            return transactions

    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []

async def get_solana_transactions_for_graph(address: str, tier: str) -> List[Dict]:
    """
    Async version of transaction fetching
    """
    return await get_solana_transactions_for_graph_async(address, tier)

async def get_wallet_graph_data(address: str, tier: str = "free"):
    """
    Main function to get all graph data for a wallet address with tier-based limits - now fully async
    """
    try:
        limits = get_subscription_limits(tier)
        
        # Pre-fetch all token prices in parallel to populate cache
        symbols_to_fetch = ["SOL"] + [token["symbol"] for token in SPL_TOKENS.values()]
        
        with ThreadPoolExecutor(max_workers=1) as executor:  # just 1 job now
            future = executor.submit(prefetch_prices, symbols_to_fetch)
            try:
                future.result(timeout=10)
            except Exception as e:
                print(f"Price prefetch error: {e}")
        
        # Get balance and transactions concurrently
        balance_task = get_solana_balance(address)
        transactions_task = get_solana_transactions_for_graph(address, tier)
        
        # Wait for both to complete
        balance, transactions = await asyncio.gather(balance_task, transactions_task)
        
        # Build nodes and edges for graph (optimized)
        nodes = {
            address: {
                "id": address,
                "label": f"{address[:8]}...{address[-8:]}",
                "type": "main_wallet",
                "balance": balance,
                "full_address": address
            }
        }
        edges = []
        
        # Process transactions to build graph (optimized with set for node tracking)
        seen_addresses = {address}
        
        for tx in transactions:
            source = tx["source"]
            destination = tx["destination"]
            
            # Add source node if not exists
            if source not in seen_addresses:
                nodes[source] = {
                    "id": source,
                    "label": f"{source[:8]}...{source[-8:]}" if len(source) > 16 else source,
                    "type": "external_wallet",
                    "full_address": source
                }
                seen_addresses.add(source)
            
            # Add destination node if not exists
            if destination not in seen_addresses:
                nodes[destination] = {
                    "id": destination,
                    "label": f"{destination[:8]}...{destination[-8:]}" if len(destination) > 16 else destination,
                    "type": "external_wallet",
                    "full_address": destination
                }
                seen_addresses.add(destination)
            
            # Create edge (represents transaction)
            edges.append({
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
            })
        
        # Calculate summary statistics
        sol_transactions = [tx for tx in transactions if tx["token"] == "SOL"]
        
        # Prepare final response with tier information
        response = {
            "wallet_address": address,
            "balance": balance,
            "chain": "Solana",
            "subscription_tier": tier.lower(),
            "tier_limits": limits,
            "total_transactions": len(transactions),
            "graph_data": {
                "nodes": list(nodes.values()),
                "edges": edges
            },
            "summary": {
                "total_incoming": sum(1 for tx in transactions if tx["direction"] == "incoming"),
                "total_outgoing": sum(1 for tx in transactions if tx["direction"] == "outgoing"),
                "total_solana_volume": sum(tx["amount"] for tx in sol_transactions),
                "unique_addresses": len(nodes) - 1,
                "date_range": {
                    "earliest": min((tx["timestamp"] for tx in transactions if tx["timestamp"]), default=None),
                    "latest": max((tx["timestamp"] for tx in transactions if tx["timestamp"]), default=None),
                    "allowed_days_back": limits["time_range_days"]
                },
                "tokens_found": list(set(tx["token"] for tx in transactions)),
                "limitations_applied": {
                    "time_limited": len(transactions) > 0,
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
    Legacy function kept for compatibility - now uses cached version
    """
    return get_cached_token_price(symbol)

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

    # Return the graph data (now fully async)
    response = await get_wallet_graph_data(wallet_address, tier)

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

async def analyze_solana_wallet_endpoint2(wallet_address: str):
    if not wallet_address:
        return {"error": "Wallet address is required"}
    return await get_wallet_graph_data(wallet_address)