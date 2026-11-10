from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

BOARD_KEY_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")
MAX_WIDGETS = 60


def validate_board_key(board_key: str) -> str:
    if not BOARD_KEY_PATTERN.match(board_key):
        raise ValueError("board_key must be 1-64 characters of a-z, 0-9, '-' or '_'")
    return board_key


class WidgetConfigModel(BaseModel):
    """One widget on a board. Unknown keys are kept so the frontend can add widget options without a backend change."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(max_length=120)
    sourceId: str = Field(max_length=64)


class LayoutItemModel(BaseModel):
    i: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=64)
    y: int = Field(ge=0, le=10000)
    w: int = Field(ge=1, le=64)
    h: int = Field(ge=1, le=200)


class BoardState(BaseModel):
    widgets: List[WidgetConfigModel] = Field(default_factory=list, max_length=MAX_WIDGETS)
    layout: List[LayoutItemModel] = Field(default_factory=list, max_length=MAX_WIDGETS)

    @field_validator("widgets")
    @classmethod
    def widget_ids_unique(cls, widgets: List[WidgetConfigModel]) -> List[WidgetConfigModel]:
        ids = [w.id for w in widgets]
        if len(ids) != len(set(ids)):
            raise ValueError("widget ids must be unique")
        return widgets


class BoardResponse(BaseModel):
    board_key: str
    # None means the user has never customized this board: the client shows its defaults.
    state: Optional[BoardState] = None
    updated_at: Optional[datetime] = None


def to_response(board_key: str, row: Optional[Any]) -> BoardResponse:
    if row is None:
        return BoardResponse(board_key=board_key)
    return BoardResponse(board_key=board_key, state=BoardState.model_validate(row["state"]), updated_at=row["updated_at"])
