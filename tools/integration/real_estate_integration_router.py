"""Real Estate Integration Router for the CEO Dashboard."""

from typing import Any, Dict, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from services.integration.services.real_estate_integration_service import (
    fetch_real_estate_binary,
    get_real_estate_floor_map_project,
    get_real_estate_properties,
    get_real_estate_property_detail,
)
from services.shared.auth_service import get_current_user_id_dependency

router = APIRouter()


@router.get("/real-estate/properties")
async def api_real_estate_properties(
    search: Optional[str] = Query(None, max_length=200),
    record_status: Optional[str] = Query(None, alias="status", max_length=40),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """List Real Estate properties with resilient caching and circuit breaker status."""
    return await get_real_estate_properties(
        user_id=user_id,
        search=search,
        status=record_status,
        limit=limit,
        offset=offset,
    )


@router.get("/real-estate/properties/{property_id}")
async def api_real_estate_property_detail(
    property_id: UUID,
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """Return full property details, units, documents, and Floor Map AI models."""
    return await get_real_estate_property_detail(property_id=property_id, user_id=user_id)


@router.get("/real-estate/projects/{project_id}")
async def api_real_estate_floor_map_project(
    project_id: UUID,
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Dict[str, Any]:
    """Return a specific Floor Map AI project (renders + 3D wall mesh)."""
    return await get_real_estate_floor_map_project(project_id=project_id, user_id=user_id)


@router.get("/real-estate/renders/{render_id}/image")
async def api_real_estate_render_image(
    render_id: UUID,
    format: str = Query("png", pattern="^(png|jpg|jpeg|webp)$"),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Response:
    """Proxy a 2D or 3D Floor Map render image."""
    try:
        data, content_type, _ = await fetch_real_estate_binary(
            f"renders/{render_id}/image",
            user_id=user_id,
            params={"format": format},
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="Render image not available") from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Real Estate unreachable: {exc}") from exc
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})


@router.get("/real-estate/projects/{project_id}/floorplan-image")
async def api_real_estate_floorplan_image(
    project_id: UUID,
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Response:
    """Proxy the original normalized floor plan image for 3D projection."""
    try:
        data, content_type, _ = await fetch_real_estate_binary(
            f"projects/{project_id}/floorplan-image",
            user_id=user_id,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="Floorplan image not available") from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Real Estate unreachable: {exc}") from exc
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})


@router.get("/real-estate/properties/{property_id}/documents/{document_id}/file")
async def api_real_estate_document_file(
    property_id: UUID,
    document_id: UUID,
    download: bool = Query(False),
    user_id: UUID = Depends(get_current_user_id_dependency),
) -> Response:
    """Proxy a property document for inline preview or download."""
    try:
        data, content_type, disposition = await fetch_real_estate_binary(
            f"properties/{property_id}/documents/{document_id}/file",
            user_id=user_id,
            params={"download": str(download).lower()},
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail="Property document not available") from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Real Estate unreachable: {exc}") from exc
    headers = {"X-Content-Type-Options": "nosniff"}
    if disposition:
        headers["Content-Disposition"] = disposition
    return Response(content=data, media_type=content_type, headers=headers)
