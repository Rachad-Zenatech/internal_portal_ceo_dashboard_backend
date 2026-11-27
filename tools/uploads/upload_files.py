from typing import List, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from services.shared.auth_service import get_current_user_id_dependency
from services.uploads.upload_archive_service import (
    UPLOAD_TYPES,
    archive_upload_bytes,
    delete_upload,
    get_storage_status,
    list_uploads_with_status,
    read_upload,
)
from services.uploads.upload_security_service import read_upload_limited

ALLOWED_UPLOAD_EXTENSIONS = (
    ".csv", ".xlsx", ".xls", ".pdf", ".txt", ".json",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".doc", ".docx", ".ppt", ".pptx",
)

router = APIRouter(dependencies=[Depends(get_current_user_id_dependency)])


def _content_disposition(disposition: str, filename: str) -> str:
    fallback = filename.replace('"', "").encode("ascii", "replace").decode("ascii")
    encoded = quote(filename)
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


@router.get("")
async def list_uploaded_files(upload_type: Optional[str] = Query(None)):
    try:
        types = [
            {"value": value, "label": label}
            for value, label in UPLOAD_TYPES.items()
            if value != "bank-statement-preview"
        ]
        raw_files, storage_warning = list_uploads_with_status(upload_type)
        files = [f for f in raw_files if f.get("upload_type") != "bank-statement-preview"]
        return {
            "upload_types": types,
            "files": files,
            "storage_status": get_storage_status(),
            "storage_warning": storage_warning,
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_archive_files(
    files: List[UploadFile] = File(...),
    upload_type: str = Form("executive-document"),
    company_id: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    user_id: UUID = Depends(get_current_user_id_dependency),
):
    if not files:
        raise HTTPException(status_code=422, detail="Select at least one file to upload.")

    metadata: dict[str, str] = {"uploaded_by": str(user_id)}
    if company_id and company_id.strip():
        metadata["company_id"] = company_id.strip()
    if notes and notes.strip():
        metadata["notes"] = notes.strip()[:500]

    uploaded = []
    for upload in files:
        content = await read_upload_limited(upload, allowed_suffixes=ALLOWED_UPLOAD_EXTENSIONS)
        if not content:
            raise HTTPException(status_code=422, detail=f"File '{upload.filename or 'upload'}' is empty.")
        try:
            item = archive_upload_bytes(
                content,
                upload_type=upload_type,
                filename=upload.filename,
                content_type=upload.content_type,
                metadata=metadata,
            )
            uploaded.append(item)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"uploaded": uploaded, "count": len(uploaded)}


@router.get("/{file_id}/view")
async def view_uploaded_file(file_id: str):
    try:
        item, content = read_upload(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=item.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition("inline", item.get("filename") or "upload"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{file_id}/download")
async def download_uploaded_file(file_id: str):
    try:
        item, content = read_upload(file_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=content,
        media_type=item.get("content_type") or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition("attachment", item.get("filename") or "upload"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{file_id}", status_code=204)
async def delete_uploaded_file(file_id: str):
    try:
        if not delete_upload(file_id):
            raise HTTPException(status_code=404, detail="Upload not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return None
