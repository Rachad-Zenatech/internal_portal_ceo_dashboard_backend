"""Social Media Compliance Integration Router for the CEO Dashboard."""

from typing import Any, Dict, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from services.integration.services.compliance_integration_service import (
    get_compliance_approved_content,
    get_compliance_public_mood,
    trigger_compliance_public_mood_scrape,
)
from services.shared.auth_service import get_current_user_id_dependency

router = APIRouter()


@router.get("/compliance/approved-content")
async def api_compliance_approved_content(
    brand_code: Optional[str] = Query(None, max_length=50),
    limit: int = Query(500, ge=1, le=2000),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """Approved content/posts from Social Media Compliance, optionally filtered by brand."""
    return await get_compliance_approved_content(user_id=user_id, brand_code=brand_code, limit=limit)


@router.get("/compliance/public-mood")
async def api_compliance_public_mood(
    refresh: bool = Query(False),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """Public Mood overview (Financial ZenaTech + Today's Posts) from Social Media Compliance."""
    return await get_compliance_public_mood(user_id=user_id, refresh=refresh)


@router.post("/compliance/public-mood/scrape")
async def api_compliance_public_mood_scrape(
    scope: Literal["all", "financial", "today"] = Query("all"),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """Trigger a live Public Mood scrape on Social Media Compliance."""
    try:
        return await trigger_compliance_public_mood_scrape(user_id=user_id, scope=scope)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not trigger Public Mood scrape: {exc}",
        )
