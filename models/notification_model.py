from typing import Optional
from pydantic import BaseModel
from datetime import datetime
from uuid import UUID

class NotificationCreate(BaseModel):
    user_id: UUID
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None


class NotificationResponse(BaseModel):
    id: int
    user_id: UUID
    type: str
    title: str
    message: str
    link_url: Optional[str]
    entity_type: Optional[str]
    entity_id: Optional[str]

    is_read: bool
    created_at: datetime
    read_at: Optional[datetime]
