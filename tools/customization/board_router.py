from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from models.customization.board_model import BoardResponse, BoardState, to_response, validate_board_key
from services.customization import board_service
from services.shared.auth_service import get_current_user_id_dependency

board_router = APIRouter()


def _checked_key(board_key: str) -> str:
    try:
        return validate_board_key(board_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@board_router.get("/customization/boards/{board_key}", response_model=BoardResponse)
async def api_get_board(board_key: str, user_id: UUID = Depends(get_current_user_id_dependency)):
    """The signed-in user's saved board, or `state: null` if they have not customized it yet."""
    key = _checked_key(board_key)
    return to_response(key, await board_service.get_board(user_id, key))


@board_router.put("/customization/boards/{board_key}", response_model=BoardResponse)
async def api_save_board(board_key: str, state: BoardState, user_id: UUID = Depends(get_current_user_id_dependency)):
    key = _checked_key(board_key)
    return to_response(key, await board_service.save_board(user_id, key, state))


@board_router.delete("/customization/boards/{board_key}")
async def api_reset_board(board_key: str, user_id: UUID = Depends(get_current_user_id_dependency)):
    """Reset to the default layout by removing the user's saved board."""
    key = _checked_key(board_key)
    await board_service.delete_board(user_id, key)
    return {"success": True}
