from fastapi import APIRouter, Query, Depends
from typing import List, Optional
from pydantic import BaseModel
from postgresql_db.database import fetch_all
from uuid import UUID
from services.auth_service import get_current_user_id_dependency, user_can_access_page

router = APIRouter()

class SearchResult(BaseModel):
    type: str
    id: int
    title: str
    subtitle: Optional[str] = None
    url: Optional[str] = None

@router.get("")
async def global_search(
    q: str = Query(..., min_length=1, max_length=100),
    user_id: UUID = Depends(get_current_user_id_dependency)
) -> List[SearchResult]:
    results = []
    search_term = f"%{q}%"

    # Add any future global search logic here (e.g., searching users or roles)

    return results
