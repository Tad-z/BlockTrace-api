from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from utils.auth import get_current_user
from utils.excel import export_wallet_data_to_excel
from fastapi.responses import StreamingResponse


router = APIRouter()

class WalletData(BaseModel):
    wallet_address: str
    balance: float
    subscription_tier: str
    tier_limits: Dict[str, Any]
    total_transactions: int
    graph_data: Dict[str, Any]
    summary: Dict[str, Any]
    chain: Optional[str] = "SOL"  # Default to SOL, will be ETH or SOL


@router.post("/excel")
async def generate_excel_report(
    wallet_data: WalletData,
    request: Request,
    # current_user=Depends(get_current_user)
):
    try:
        output, filename = await export_wallet_data_to_excel(wallet_data)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate Excel report: {str(e)}")
    

