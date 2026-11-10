from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from postgresql_db.database import execute, fetch_one
from models.customization.board_model import BoardState


def _decode_state(row: Any) -> Any:
    """asyncpg returns JSONB as text unless a codec is registered; normalise to a dict."""
    if row is None:
        return None
    state = row["state"]
    if isinstance(state, str):
        state = json.loads(state)
    return {"state": state, "updated_at": row["updated_at"]}


async def get_board(user_id: UUID, board_key: str) -> Optional[Any]:
    row = await fetch_one(
        "SELECT state, updated_at FROM user_dashboard_layouts WHERE user_id = $1 AND board_key = $2",
        user_id,
        board_key,
    )
    return _decode_state(row)


async def save_board(user_id: UUID, board_key: str, state: BoardState) -> Optional[Any]:
    # Keyed by (user_id, board_key): one user can never read or overwrite another user's board.
    row = await fetch_one(
        """
        INSERT INTO user_dashboard_layouts (user_id, board_key, state)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (user_id, board_key)
        DO UPDATE SET state = EXCLUDED.state, updated_at = now()
        RETURNING state, updated_at
        """,
        user_id,
        board_key,
        state.model_dump_json(),
    )
    return _decode_state(row)


async def delete_board(user_id: UUID, board_key: str) -> None:
    await execute(
        "DELETE FROM user_dashboard_layouts WHERE user_id = $1 AND board_key = $2",
        user_id,
        board_key,
    )
