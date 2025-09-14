from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import pandas as pd
import io
from datetime import datetime
import json

app = FastAPI()

class WalletData(BaseModel):
    wallet_address: str
    balance: float
    subscription_tier: str
    tier_limits: Dict[str, Any]
    total_transactions: int
    graph_data: Dict[str, Any]
    summary: Dict[str, Any]
    chain: Optional[str] = "SOL"  # Default to SOL, will be ETH or SOL

async def export_wallet_data_to_excel(data: WalletData):
    """
    Export wallet tracking data to Excel format
    """
    try:
        # Create Excel writer object
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            
            # 1. WALLET OVERVIEW SHEET
            # Format balance with chain symbol
            if data.chain == "Solana":
                balance_str = f"{data.balance:,.6f} SOL"
                total_volume = f"{data.summary.get('total_solana_volume', 0):,.6f} SOL"
            elif data.chain == "Ethereum":
                balance_str = f"{data.balance:,.18f} ETH"
                total_volume = f"{data.summary.get('total_ethereum_volume', 0):,.18f} ETH"

            overview_data = {
                'Metric': [
                    'Wallet Address',
                    'Blockchain',
                    'Current Balance',
                    'Total Transactions Analyzed',
                    'Unique Addresses Interacted',
                    f'Total {data.chain} Volume',
                    'Analysis Period (Days)',
                    'Date Range Start',
                    'Date Range End',
                    'Incoming Transactions',
                    'Outgoing Transactions',
                    'Tokens Found'
                ],
                'Value': [
                    data.wallet_address,
                    data.chain,
                    balance_str,
                    data.total_transactions,
                    data.summary.get('unique_addresses', 0),
                    total_volume,
                    data.tier_limits.get('time_range_days', 0),
                    data.summary.get('date_range', {}).get('earliest', 'N/A'),
                    data.summary.get('date_range', {}).get('latest', 'N/A'),
                    data.summary.get('total_incoming', 0),
                    data.summary.get('total_outgoing', 0),
                    ', '.join(data.summary.get('tokens_found', []))
                ]
            }
            
            overview_df = pd.DataFrame(overview_data)
            overview_df.to_excel(writer, sheet_name='Wallet Overview', index=False)
            
            # 2. TRANSACTIONS DETAIL SHEET
            transactions_data = []
            for edge in data.graph_data.get('edges', []):
                tx_data = {
                    'Transaction Hash': edge.get('transaction_hash', ''),
                    'Timestamp': edge.get('timestamp', ''),
                    'Direction': edge.get('direction', '').title(),
                    'From Address': edge.get('source', ''),
                    'To Address': edge.get('destination', ''),
                    'Amount': edge.get('amount', 0),
                    'Token': edge.get('token', ''),
                    'Token Address': edge.get('token_address', '') or 'Native Token',
                    'USD Value': f"${edge.get('usd_equivalent', 0):,.2f}",
                    'Transaction Fee': edge.get('fee', 0),
                    'Status': edge.get('status', '').title(),
                    'Blockchain': data.chain,
                    'Weight': edge.get('weight', 0)
                }
                transactions_data.append(tx_data)
            
            if transactions_data:
                transactions_df = pd.DataFrame(transactions_data)
                # Sort by timestamp (most recent first)
                transactions_df = transactions_df.sort_values('Timestamp', ascending=False)
                transactions_df.to_excel(writer, sheet_name='Transaction Details', index=False)
            
           # 3. ADDRESS INTERACTIONS SHEET
            addresses_data = []
            main_wallet = data.wallet_address.lower()  # Normalize to lowercase for comparison

            for node in data.graph_data.get('nodes', []):
                node_id = node.get('id', '').lower()  # Normalize to lowercase
                if node_id != main_wallet and node.get('type') != 'main_wallet':
                    # Calculate interaction stats with this address (case-insensitive)
                    incoming_count = sum(1 for edge in data.graph_data.get('edges', []) 
                                    if edge.get('destination', '').lower() == main_wallet and edge.get('source', '').lower() == node_id)
                    outgoing_count = sum(1 for edge in data.graph_data.get('edges', []) 
                                    if edge.get('source', '').lower() == main_wallet and edge.get('destination', '').lower() == node_id)
                    
                    total_incoming_value = sum(edge.get('usd_equivalent', 0) for edge in data.graph_data.get('edges', []) 
                                            if edge.get('destination', '').lower() == main_wallet and edge.get('source', '').lower() == node_id)
                    total_outgoing_value = sum(edge.get('usd_equivalent', 0) for edge in data.graph_data.get('edges', []) 
                                            if edge.get('source', '').lower() == main_wallet and edge.get('destination', '').lower() == node_id)
                    
                    addr_data = {
                        'Address': node.get('full_address', node.get('id', '')),
                        'Label': node.get('label', ''),
                        'Type': node.get('type', '').replace('_', ' ').title(),
                        'Incoming Transactions': incoming_count,
                        'Outgoing Transactions': outgoing_count,
                        'Total Transactions': incoming_count + outgoing_count,
                        'Total Received (USD)': f"${total_incoming_value:,.2f}",
                        'Total Sent (USD)': f"${total_outgoing_value:,.2f}",
                        'Net Flow (USD)': f"${total_incoming_value - total_outgoing_value:,.2f}",
                    }
                    addresses_data.append(addr_data)

            if addresses_data:
                addresses_df = pd.DataFrame(addresses_data)
                # Sort by total transaction count
                addresses_df = addresses_df.sort_values('Total Transactions', ascending=False)
                addresses_df.to_excel(writer, sheet_name='Address Interactions', index=False)
            
            # 4. TOKEN SUMMARY SHEET
            token_summary = {}
            for edge in data.graph_data.get('edges', []):
                token = edge.get('token', 'Unknown')
                token_addr = edge.get('token_address', 'Native')
                direction = edge.get('direction', '')
                amount = edge.get('amount', 0)
                usd_value = edge.get('usd_equivalent', 0)
                
                key = f"{token}|{token_addr}"
                
                if key not in token_summary:
                    token_summary[key] = {
                        'Token': token,
                        'Token Address': token_addr,
                        'Total Incoming Amount': 0,
                        'Total Outgoing Amount': 0,
                        'Total Incoming USD': 0,
                        'Total Outgoing USD': 0,
                        'Transaction Count': 0,
                        'Blockchain': data.chain
                    }
                
                token_summary[key]['Transaction Count'] += 1
                
                if direction == 'incoming':
                    token_summary[key]['Total Incoming Amount'] += amount
                    token_summary[key]['Total Incoming USD'] += usd_value
                elif direction == 'outgoing':
                    token_summary[key]['Total Outgoing Amount'] += amount
                    token_summary[key]['Total Outgoing USD'] += usd_value
            
            if token_summary:
                token_data = []
                for token_info in token_summary.values():
                    net_amount = token_info['Total Incoming Amount'] - token_info['Total Outgoing Amount']
                    net_usd = token_info['Total Incoming USD'] - token_info['Total Outgoing USD']
                    
                    token_data.append({
                        **token_info,
                        'Net Amount': net_amount,
                        'Net USD Value': f"${net_usd:,.2f}",
                        'Total Incoming USD': f"${token_info['Total Incoming USD']:,.2f}",
                        'Total Outgoing USD': f"${token_info['Total Outgoing USD']:,.2f}"
                    })
                
                tokens_df = pd.DataFrame(token_data)
                tokens_df = tokens_df.sort_values('Transaction Count', ascending=False)
                tokens_df.to_excel(writer, sheet_name='Token Summary', index=False)
            
            # 5. ACCOUNT LIMITS & INFO SHEET
            limits_data = {
                'Setting': [
                    'Subscription Tier',
                    'Time Range (Days)',
                    'Daily Address Limit',
                    'Max Transactions',
                    'Graph Types Available',
                    'Graph Depth',
                    'Export Enabled',
                    'Time Limited Applied',
                    'Transaction Limited Applied'
                ],
                'Value': [
                    data.subscription_tier.title(),
                    data.tier_limits.get('time_range_days', 0),
                    data.tier_limits.get('daily_address_limit', 0),
                    data.tier_limits.get('max_transactions', 0),
                    ', '.join(data.tier_limits.get('graph_types', [])),
                    data.tier_limits.get('graph_depth', 0),
                    'Yes' if data.tier_limits.get('export_enabled', False) else 'No',
                    'Yes' if data.summary.get('limitations_applied', {}).get('time_limited', False) else 'No',
                    'Yes' if data.summary.get('limitations_applied', {}).get('transaction_limited', False) else 'No'
                ]
            }
            
            limits_df = pd.DataFrame(limits_data)
            limits_df.to_excel(writer, sheet_name='Account Limits', index=False)
        
        output.seek(0)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chain_prefix = data.chain.lower()
        short_address = data.wallet_address[:8] + "..." + data.wallet_address[-6:]
        filename = f"wallet_analysis_{chain_prefix}_{short_address}_{timestamp}.xlsx"

        
        return output, filename
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating Excel file: {str(e)}")

# Alternative endpoint that accepts raw JSON (if frontend prefers this approach)

async def export_wallet_data_from_json(request_data: Dict[str, Any]):
    """
    Alternative endpoint that accepts raw JSON data
    """
    try:
        # Convert dict to WalletData model
        wallet_data = WalletData(**request_data)
        return await export_wallet_data_to_excel(wallet_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")
