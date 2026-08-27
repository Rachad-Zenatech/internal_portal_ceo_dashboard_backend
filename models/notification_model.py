from typing import Optional, Union
from pydantic import BaseModel
from datetime import datetime
from uuid import UUID

class NotificationCreate(BaseModel):
    user_id: Union[UUID, str]
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None


class NotificationResponse(BaseModel):
    id: int
    user_id: Union[UUID, str]
    type: str
    title: str
    message: str
    link_url: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None

    is_read: bool
    created_at: datetime
    read_at: Optional[datetime] = None
