from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from services.auth_service import require_permission
from uuid import UUID
from fastapi import Depends

from services.upload_archive_service import (
    UPLOAD_TYPES,
    delete_upload,
    list_uploads,
    read_upload,
)


router = APIRouter()


@router.get("")
async def list_uploaded_files(upload_type: Optional[str] = Query(None)):
    try:
        types = [
            {"value": value, "label": label}
            for value, label in UPLOAD_TYPES.items()
            if value != "bank-statement-preview"
        ]
        files = [
            f for f in list_uploads(upload_type)
            if f.get("upload_type") != "bank-statement-preview"
        ]
        return {
            "upload_types": types,
            "files": files,
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _content_disposition(disposition: str, filename: str) -> str:
    fallback = filename.replace('"', "")
    encoded = quote(filename)
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


@router.get("/{file_id}/view")
async def view_uploaded_file(file_id: str):
    try:
        item, content = read_upload(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=item.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(
                "inline", item.get("filename") or "upload"
            )
        },
    )


@router.get("/{file_id}/download")
async def download_uploaded_file(file_id: str):
    try:
        item, content = read_upload(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=item.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(
                "attachment", item.get("filename") or "upload"
            )
        },
    )


@router.delete("/{file_id}", status_code=204)
async def delete_uploaded_file(file_id: str):
    if not delete_upload(file_id):
        raise HTTPException(status_code=404, detail="Upload not found")
    return None
