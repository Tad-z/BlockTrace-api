import requests
from datetime import datetime, timedelta, timezone
import pytz
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

# Subscription tier limits (same as Solana)
SUBSCRIPTION_LIMITS = {
    "free": {
        "time_range_days": 7,
        "daily_address_limit": 5,
        "max_transactions": 50,
        "graph_types": ["force_directed"],
        "graph_depth": 1,
        "export_enabled": False,
    },
    "pro": {
        "time_range_days": 180,  # 6 months
        "daily_address_limit": 50,
        "max_transactions": 500,
        "graph_types": ["force_directed", "flow", "timeline", "sankey"],
        "graph_depth": 3,
        "export_enabled": True,
    },
}

ALCHEMY_ETH_URL = os.getenv("ALCHEMY_ETHEREUM_URL")

# Popular ERC-20 tokens
ERC20_TOKENS = {
    "0xA0b86a33E6441C47b3ff2b52d97F11c42D7b70e5": {
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
    },
    "0xdAC17F958D2ee523a2206206994597C13D831ec7": {
        "symbol": "USDT",
        "name": "Tether USD",
        "decimals": 6,
    },
    "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599": {
        "symbol": "WBTC",
        "name": "Wrapped Bitcoin",
        "decimals": 8,
    },
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2": {
        "symbol": "WETH",
        "name": "Wrapped Ether",
        "decimals": 18,
    },
    "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984": {
        "symbol": "UNI",
        "name": "Uniswap",
        "decimals": 18,
    },
    "0x7D1AfA7B718fb893dB30A3aBc0Cfc608AaCfeBB0": {
        "symbol": "MATIC",
        "name": "Polygon",
        "decimals": 18,
    },
    "0x514910771AF9Ca656af840dff83E8264EcF986CA": {
        "symbol": "LINK",
        "name": "Chainlink",
        "decimals": 18,
    },
    "0x6B175474E89094C44Da98b954EedeAC495271d0F": {
        "symbol": "DAI",
        "name": "Dai Stablecoin",
        "decimals": 18,
    },
    "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE": {
        "symbol": "SHIB",
        "name": "Shiba Inu",
        "decimals": 18,
    },
    "0x4Fabb145d64652a948d72533023f6E7A623C7C53": {
        "symbol": "BUSD",
        "name": "Binance USD",
        "decimals": 18,
    },
}

# Cache to store already fetched prices with TTL
_token_price_cache: Dict[str, Dict] = {}
PRICE_CACHE_TTL = 300  # 5 minutes


def get_subscription_limits(tier: str) -> Dict:
    """Get limits based on subscription tier"""
    return SUBSCRIPTION_LIMITS.get(tier.lower(), SUBSCRIPTION_LIMITS["free"])


def calculate_time_range(tier: str) -> Optional[datetime]:
    """Calculate the earliest timestamp allowed based on tier - now returns timezone-aware datetime"""
    limits = get_subscription_limits(tier)
    days_back = limits["time_range_days"]

    # Make the datetime timezone-aware (UTC)
    earliest_time = datetime.now(timezone.utc) - timedelta(days=days_back)
    return earliest_time


async def get_eth_balance_async(session: aiohttp.ClientSession, address: str) -> float:
    """Get current ETH balance for an address asynchronously"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }

    try:
        async with session.post(
            ALCHEMY_ETH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            result = await response.json()

            if "error" in result:
                raise Exception(f"API Error: {result['error']}")

            balance_wei = int(result.get("result", "0x0"), 16)
            return balance_wei / 1e18
    except Exception as e:
        print(f"Error getting balance for {address}: {e}")
        return 0


async def get_eth_balance(address: str) -> float:
    """Get current ETH balance for an address"""
    async with aiohttp.ClientSession() as session:
        return await get_eth_balance_async(session, address)


async def get_eth_transactions_async(
    session: aiohttp.ClientSession,
    address: str,
    start_block: str = "0x0",
    end_block: str = "latest",
    page: int = 1,
    offset: int = 100,
):
    """Get transaction list for an address using Alchemy Enhanced APIs"""

    # Get outgoing transactions (FROM the address)
    outgoing_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [
            {
                "fromBlock": start_block,
                "toBlock": end_block,
                "fromAddress": address,  # Transactions FROM this address
                "category": ["external", "internal", "erc20", "erc721", "erc1155"],
                "withMetadata": True,
                "excludeZeroValue": False,
                "maxCount": f"0x{min(offset, 1000):x}",  # Alchemy max is 1000
            }
        ],
    }

    try:
        async with session.post(
            ALCHEMY_ETH_URL,
            json=outgoing_payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            result = await response.json()

            if "error" in result:
                print(f"API Error for outgoing transactions: {result['error']}")
                transfers_from = []
            else:
                transfers_from = result.get("result", {}).get("transfers", [])

    except Exception as e:
        print(f"Error getting outgoing transactions: {e}")
        transfers_from = []

    # Get incoming transactions (TO the address)
    incoming_payload = {
        "jsonrpc": "2.0",
        "id": 2,  # Different ID for clarity
        "method": "alchemy_getAssetTransfers",
        "params": [
            {
                "fromBlock": start_block,
                "toBlock": end_block,
                "toAddress": address,  # Transactions TO this address
                # Note: We DON'T include fromAddress at all for incoming transactions
                "category": ["external", "internal", "erc20", "erc721", "erc1155"],
                "withMetadata": True,
                "excludeZeroValue": False,
                "maxCount": f"0x{min(offset, 1000):x}",  # Alchemy max is 1000
            }
        ],
    }

    try:
        async with session.post(
            ALCHEMY_ETH_URL,
            json=incoming_payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            result = await response.json()

            if "error" in result:
                print(f"API Error for incoming transactions: {result['error']}")
                transfers_to = []
            else:
                transfers_to = result.get("result", {}).get("transfers", [])

    except Exception as e:
        print(f"Error getting incoming transactions: {e}")
        transfers_to = []

    # Combine and deduplicate by transaction hash
    all_transfers = transfers_from + transfers_to
    seen_hashes = set()
    unique_transfers = []

    for transfer in all_transfers:
        tx_hash = transfer.get("hash")
        if tx_hash and tx_hash not in seen_hashes:
            seen_hashes.add(tx_hash)
            unique_transfers.append(transfer)

    print(
        f"Found {len(transfers_from)} outgoing and {len(transfers_to)} incoming transfers, {len(unique_transfers)} unique total"
    )

    return unique_transfers


async def get_transaction_receipt_async(session: aiohttp.ClientSession, tx_hash: str):
    """Get transaction receipt for fee calculation"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }

    try:
        async with session.post(
            ALCHEMY_ETH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            result = await response.json()

            if "error" in result:
                return None

            return result.get("result")
    except Exception as e:
        print(f"Error getting receipt for {tx_hash[:16]}: {e}")
        return None


async def get_transaction_details_async(session: aiohttp.ClientSession, tx_hash: str):
    """Get full transaction details"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionByHash",
        "params": [tx_hash],
    }

    try:
        async with session.post(
            ALCHEMY_ETH_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            result = await response.json()

            if "error" in result:
                return None

            return result.get("result")
    except Exception as e:
        print(f"Error getting transaction {tx_hash[:16]}: {e}")
        return None


async def process_eth_transfers_batch_async(session: aiohttp.ClientSession, transfers: List[Dict], user_address: str, max_transactions: int, earliest_time: datetime):
    """Process Ethereum transfers in batches asynchronously with enhanced debugging"""
    transactions = []
    seen_transfers = set()
    
    print(f"Starting to process {len(transfers)} transfers")
    print(f"Earliest allowed time: {earliest_time}")
    
    # Create semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(5)
    
    async def process_single_transfer_with_retry(transfer, max_retries=3):
        async with semaphore:
            try:
                
                # Parse transfer data from Alchemy
                tx_hash = transfer.get("hash")
                block_num = transfer.get("blockNum")
                
                if not tx_hash or not block_num:
                    print(f"Missing hash or block number: hash={tx_hash}, block={block_num}")
                    return None
                
                
                # Convert block number and get timestamp
                block_num_int = int(block_num, 16) if isinstance(block_num, str) else block_num
                
                # Get transaction details for timestamp and fee
                tx_details = None
                tx_receipt = None
                
                for attempt in range(max_retries):
                    try:
                        tx_details = await get_transaction_details_async(session, tx_hash)
                        tx_receipt = await get_transaction_receipt_async(session, tx_hash)
                        print(f"Attempt {attempt + 1}: Got details={tx_details is not None}, receipt={tx_receipt is not None}")
                        break
                    except Exception as e:
                        print(f"Attempt {attempt + 1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        else:
                            print(f"Failed to get details for {tx_hash} after {max_retries} attempts")
                            return None
                
                if not tx_details:
                    print("No transaction details available, skipping")
                    return None
                
                # Parse timestamp from metadata or estimate
                metadata = transfer.get("metadata", {})
                block_timestamp = metadata.get("blockTimestamp")
                
                
                if block_timestamp:
                    # Parse ISO timestamp - handle both with and without timezone info
                    try:
                        if block_timestamp.endswith('Z'):
                            # Replace Z with +00:00 for UTC
                            tx_datetime = datetime.fromisoformat(block_timestamp.replace('Z', '+00:00'))
                        elif '+' in block_timestamp or block_timestamp.endswith(('UTC', 'GMT')):
                            # Already has timezone info
                            tx_datetime = datetime.fromisoformat(block_timestamp)
                        else:
                            # Assume UTC if no timezone info
                            tx_datetime = datetime.fromisoformat(block_timestamp).replace(tzinfo=timezone.utc)
                    except ValueError as e:
                        print(f"Error parsing timestamp {block_timestamp}: {e}")
                        # Fall back to estimation
                        current_block = 18500000  # Approximate current block
                        blocks_ago = current_block - block_num_int
                        estimated_time = datetime.now(timezone.utc) - timedelta(seconds=blocks_ago * 12)
                        tx_datetime = estimated_time
                else:
                    # Estimate timestamp based on current time and block number
                    # Rough estimate: 12 seconds per block
                    current_block = 21000000  # Updated estimate for 2024
                    blocks_ago = current_block - block_num_int
                    estimated_time = datetime.now(timezone.utc) - timedelta(seconds=blocks_ago * 12)
                    tx_datetime = estimated_time
                
                # Ensure earliest_time is timezone-aware for comparison
                if earliest_time.tzinfo is None:
                    earliest_time_aware = earliest_time.replace(tzinfo=timezone.utc)
                else:
                    earliest_time_aware = earliest_time
                
                
                # Check time range - now both datetimes are timezone-aware
                if tx_datetime < earliest_time_aware:
                    print(f"Transaction too old, skipping (tx: {tx_datetime} < earliest: {earliest_time_aware})")
                    return None
                
                print("Transaction is within time range, continuing...")
                
                # Calculate fee
                fee = 0
                if tx_receipt:
                    gas_used = int(tx_receipt.get("gasUsed", "0x0"), 16)
                    gas_price = int(tx_details.get("gasPrice", "0x0"), 16)
                    fee = (gas_used * gas_price) / 1e18
                
                # Parse transfer details
                from_address = transfer.get("from", "").lower()
                to_address = transfer.get("to", "").lower()
                user_address_lower = user_address.lower()

                
                # Determine direction
                if from_address == user_address_lower:
                    direction = "outgoing"
                elif to_address == user_address_lower:
                    direction = "incoming"
                else:
                    direction = "interaction"  # Shouldn't happen with proper filtering
                
                
                # Parse amount and token info
                value = transfer.get("value", 0)
                category = transfer.get("category", "external")
                
                
                if category in ["external", "internal"]:
                    # ETH transfer
                    token_symbol = "ETH"
                    token_address = None
                    amount = float(value) if value else 0
                else:
                    # ERC-20 transfer
                    raw_contract = transfer.get("rawContract", {})
                    token_address = raw_contract.get("address", "").lower()
                    decimals = raw_contract.get("decimal", 18)
                    
                    
                    # Get token info
                    token_info = ERC20_TOKENS.get(token_address)
                    if token_info:
                        token_symbol = token_info["symbol"]
                        decimals = token_info["decimals"]
                    else:
                        token_symbol = f"TOKEN_{token_address[:8]}"
                    
                    # Parse amount
                    raw_value = raw_contract.get("value", "0x0")
                    if isinstance(raw_value, str):
                        amount_raw = int(raw_value, 16) if raw_value.startswith("0x") else int(raw_value)
                    else:
                        amount_raw = raw_value
                    
                    amount = amount_raw / (10 ** decimals)
                
                # Calculate USD equivalent
                usd_equivalent = 0
                if token_symbol == "ETH":
                    usd_equivalent = amount * get_cached_token_price("ETH")
                else:
                    usd_equivalent = amount * get_cached_token_price(token_symbol)
                
                
                # Create transfer key for deduplication
                transfer_key = (
                    tx_hash,
                    from_address,
                    to_address,
                    amount,
                    token_symbol
                )
                
                if transfer_key not in seen_transfers:
                    seen_transfers.add(transfer_key)
                    
                    transaction_data = {
                        "hash": tx_hash,
                        "timestamp": tx_datetime.isoformat(),
                        "chain": "Ethereum",
                        "source": from_address,
                        "destination": to_address,
                        "amount": amount,
                        "direction": direction,
                        "token": token_symbol,
                        "token_address": token_address,
                        "fee": fee,
                        "status": "success" if tx_receipt and tx_receipt.get("status") == "0x1" else "failed",
                        "block_number": block_num_int,
                        "usd_equivalent": usd_equivalent,
                        "category": category
                    }
                    
                    print(f"✓ Successfully processed transaction: {tx_hash[:16]}...")
                    return transaction_data
                else:
                    print(f"Duplicate transaction found, skipping: {tx_hash[:16]}...")
                    return None
                    
            except Exception as e:
                print(f"Error processing transfer: {e}")
                import traceback
                traceback.print_exc()
                return None
    
    # Process transfers in batches
    batch_size = 10
    processed_count = 0
    
    for i in range(0, len(transfers), batch_size):
        if len(transactions) >= max_transactions:
            break
            
        batch = transfers[i:i + batch_size]
        print(f"\n--- Processing batch {i//batch_size + 1} with {len(batch)} transfers ---")
        
        # Process batch concurrently
        tasks = [process_single_transfer_with_retry(transfer) for transfer in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                print(f"Error processing transfer: {result}")
                continue
                
            if result:
                transactions.append(result)
                if len(transactions) >= max_transactions:
                    return transactions[:max_transactions]
        
        processed_count += len(batch)
        print(f"Processed batch {i//batch_size + 1}, total transactions so far: {len(transactions)}")
        
        # Delay between batches to respect rate limits
        if i + batch_size < len(transfers):
            await asyncio.sleep(0.3)
        
        if len(transactions) >= max_transactions:
            break
    
    print(f"Final transaction count: {len(transactions)} out of {max_transactions} requested")
    return transactions[:max_transactions]


@lru_cache(maxsize=100)
def get_cached_token_price(symbol: str) -> float:
    """Get cached token price with TTL"""
    current_time = time.time()

    if symbol in _token_price_cache:
        cache_entry = _token_price_cache[symbol]
        if current_time - cache_entry["timestamp"] < PRICE_CACHE_TTL:
            return cache_entry["price"]

    # Fetch new price
    price = get_token_price_usd_sync(symbol)
    _token_price_cache[symbol] = {"price": price, "timestamp": current_time}

    return price


def get_token_price_usd_sync(symbol: str) -> float:
    """Synchronous version of price fetching for caching with default fallback"""
    try:
        symbol_map = {
            "ETH": "ethereum",
            "USDC": "usd-coin",
            "USDT": "tether",
            "WBTC": "wrapped-bitcoin",
            "WETH": "weth",
            "UNI": "uniswap",
            "MATIC": "matic-network",
            "LINK": "chainlink",
            "DAI": "dai",
            "SHIB": "shiba-inu",
            "BUSD": "binance-usd",
        }

        # Fallback prices if 429 or API failure
        default_prices = {
            "ETH": 4300.73,
            "USDC": 0.999814,
            "USDT": 1.0,
            "WBTC": 111237,
            "WETH": 4304.64,
            "UNI": 9.37,
            "MATIC":  0.279483,
            "LINK": 22.26,
            "DAI": 1.0,
            "SHIB":  0.00001237,
            "BUSD": 1.0,
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
        return default_prices.get(symbol, 0)


def prefetch_prices(symbols: list[str]):
    """Bulk fetch prices for multiple symbols and update cache"""
    symbol_map = {
        "ETH": "ethereum",
        "USDC": "usd-coin",
        "USDT": "tether",
        "WBTC": "wrapped-bitcoin",
        "WETH": "weth",
        "UNI": "uniswap",
        "MATIC": "matic-network",
        "LINK": "chainlink",
        "DAI": "dai",
        "SHIB": "shiba-inu",
        "BUSD": "binance-usd",
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
                _token_price_cache[s] = {"price": data[cg_id]["usd"], "timestamp": now}
    except Exception as e:
        print("Prefetch failed:", e)


async def get_eth_transactions_for_graph_async(address: str, tier: str) -> List[Dict]:
    """Async version of transaction fetching with optimizations"""
    try:
        limits = get_subscription_limits(tier)
        max_transactions = limits["max_transactions"]
        earliest_time = calculate_time_range(tier)

        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as session:

            # Get transfers using Alchemy Enhanced APIs
            transfers = await get_eth_transactions_async(
                session, address, offset=min(max_transactions * 2, 1000)
            )

            # Process transfers asynchronously
            transactions = await process_eth_transfers_batch_async(
                session, transfers, address, max_transactions, earliest_time
            )

            return transactions

    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return []


async def get_eth_transactions_for_graph(address: str, tier: str) -> List[Dict]:
    """Async version of transaction fetching"""
    return await get_eth_transactions_for_graph_async(address, tier)


async def get_wallet_graph_data(address: str, tier: str = "pro"):
    """Main function to get all graph data for a wallet address with tier-based limits - now fully async"""
    try:
        limits = get_subscription_limits(tier)

        # Pre-fetch all token prices in parallel to populate cache
        symbols_to_fetch = ["ETH"] + [
            token["symbol"] for token in ERC20_TOKENS.values()
        ]

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(prefetch_prices, symbols_to_fetch)
            try:
                future.result(timeout=10)
            except Exception as e:
                print(f"Price prefetch error: {e}")

        # Get balance and transactions concurrently
        balance_task = get_eth_balance(address)
        transactions_task = get_eth_transactions_for_graph(address, tier)

        # Wait for both to complete
        balance, transactions = await asyncio.gather(balance_task, transactions_task)

        # Build nodes and edges for graph
        nodes = {
            address.lower(): {
                "id": address.lower(),
                "label": f"{address[:8]}...{address[-8:]}",
                "type": "main_wallet",
                "balance": balance,
                "full_address": address,
            }
        }
        edges = []

        # Process transactions to build graph
        seen_addresses = {address.lower()}

        for tx in transactions:
            source = tx["source"]
            destination = tx["destination"]

            # Add source node if not exists
            if source not in seen_addresses:
                nodes[source] = {
                    "id": source,
                    "label": (
                        f"{source[:8]}...{source[-8:]}" if len(source) > 16 else source
                    ),
                    "type": "external_wallet",
                    "full_address": source,
                }
                seen_addresses.add(source)

            # Add destination node if not exists
            if destination not in seen_addresses:
                nodes[destination] = {
                    "id": destination,
                    "label": (
                        f"{destination[:8]}...{destination[-8:]}"
                        if len(destination) > 16
                        else destination
                    ),
                    "type": "external_wallet",
                    "full_address": destination,
                }
                seen_addresses.add(destination)

            # Create edge (represents transaction)
            edges.append(
                {
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
                    "block_number": tx.get("block_number"),
                    "category": tx.get("category"),
                    "weight": tx["amount"],
                }
            )

        # Calculate summary statistics
        eth_transactions = [tx for tx in transactions if tx["token"] == "ETH"]

        # Prepare final response with tier information
        response = {
            "wallet_address": address,
            "balance": balance,
            "chain": "Ethereum",
            "subscription_tier": tier.lower(),
            "tier_limits": limits,
            "total_transactions": len(transactions),
            "graph_data": {"nodes": list(nodes.values()), "edges": edges},
            "summary": {
                "total_incoming": sum(
                    1 for tx in transactions if tx["direction"] == "incoming"
                ),
                "total_outgoing": sum(
                    1 for tx in transactions if tx["direction"] == "outgoing"
                ),
                "total_eth_volume": sum(tx["amount"] for tx in eth_transactions),
                "unique_addresses": len(nodes) - 1,
                "date_range": {
                    "earliest": min(
                        (tx["timestamp"] for tx in transactions if tx["timestamp"]),
                        default=None,
                    ),
                    "latest": max(
                        (tx["timestamp"] for tx in transactions if tx["timestamp"]),
                        default=None,
                    ),
                    "allowed_days_back": limits["time_range_days"],
                },
                "tokens_found": list(set(tx["token"] for tx in transactions)),
                "limitations_applied": {
                    "time_limited": len(transactions) > 0,
                    "transaction_limited": len(transactions)
                    == limits["max_transactions"],
                },
            },
        }

        return response

    except Exception as e:
        return {
            "error": str(e),
            "wallet_address": address,
            "subscription_tier": tier.lower(),
            "graph_data": {"nodes": [], "edges": []},
            "summary": {},
        }


def get_token_price_usd(symbol: str) -> float:
    """Legacy function kept for compatibility - now uses cached version"""
    return get_cached_token_price(symbol)


def get_day_bounds():
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)
    return start, end


# Main endpoint function
async def analyze_ethereum_wallet_endpoint(
    request: Request, userId, chain: str, wallet_address: str, tier: str = "free"
):
    db = get_db(request.app)

    if not wallet_address:
        return {"error": "Wallet address is required"}

    # Validate Ethereum address format
    if not wallet_address.startswith("0x") or len(wallet_address) != 42:
        return {"error": "Invalid Ethereum address format"}

    # Validate tier
    if tier.lower() not in ["free", "pro"]:
        return {"error": "Invalid tier. Must be 'free' or 'pro'"}

    # Daily address limits
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
                "created_at": {"$gte": start.isoformat(), "$lt": end.isoformat()},
            },
        )

        if (
            wallet_address.lower() not in used_addresses
            and len(used_addresses) >= free_daily_limit
        ):
            raise HTTPException(
                status_code=403, detail="Free tier daily limit reached."
            )

    if tier.lower() == "pro":
        start, end = get_day_bounds()

        used_addresses = await db["wallet_query_logs"].distinct(
            "wallet_address",
            {
                "userId": userId,
                "tier": "pro",
                "created_at": {"$gte": start.isoformat(), "$lt": end.isoformat()},
            },
        )

        if (
            wallet_address.lower() not in used_addresses
            and len(used_addresses) >= pro_daily_limit
        ):
            raise HTTPException(status_code=403, detail="Pro tier daily limit reached.")

    # Log query after passing check
    await db["wallet_query_logs"].insert_one(
        WalletQueryLogs.from_query(
            user_id=userId, wallet_address=wallet_address, tier=tier, chain=chain
        ).dict()
    )

    # Return the graph data (now fully async)
    response = await get_wallet_graph_data(wallet_address, tier)

    if tier.lower() == "free":
        response["rate_limit_info"] = {
            "addresses_used_today": len(used_addresses)
            + (1 if wallet_address.lower() not in used_addresses else 0),
            "daily_limit": free_daily_limit,
            "remaining": max(
                0,
                5
                - len(used_addresses)
                - (1 if wallet_address.lower() not in used_addresses else 0),
            ),
        }

    if tier.lower() == "pro":
        response["rate_limit_info"] = {
            "addresses_used_today": len(used_addresses)
            + (1 if wallet_address.lower() not in used_addresses else 0),
            "daily_limit": pro_daily_limit,
            "remaining": max(
                0,
                50
                - len(used_addresses)
                - (1 if wallet_address.lower() not in used_addresses else 0),
            ),
        }

    return response


async def analyze_ethereum_wallet_endpoint2(wallet_address: str):
    if not wallet_address:
        return {"error": "Wallet address is required"}
    return await get_wallet_graph_data(wallet_address)
