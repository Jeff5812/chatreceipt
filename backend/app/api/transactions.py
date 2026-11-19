"""POST /api/transactions/calculate: backend totals for the review form."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import rate_limit
from app.calculations import calculate
from app.models import Calculation, TransactionDraft

router = APIRouter()


@router.post(
    "/api/transactions/calculate",
    response_model=Calculation,
    dependencies=[Depends(rate_limit("calculate"))],
)
async def calculate_transaction(draft: TransactionDraft) -> Calculation:
    return calculate(draft)
