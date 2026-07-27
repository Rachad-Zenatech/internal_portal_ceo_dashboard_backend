from __future__ import annotations

import os
from pathlib import Path
from typing import Collection, Optional

from fastapi import HTTPException, UploadFile


DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def max_upload_bytes() -> int:
    return max(
        UPLOAD_READ_CHUNK_BYTES,
        int(os.getenv("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
    )


async def read_upload_limited(
    upload: UploadFile,
    *,
    allowed_suffixes: Optional[Collection[str]] = None,
    maximum_bytes: Optional[int] = None,
) -> bytes:
    """Read an upload with an extension allowlist and a hard byte limit."""
    filename = Path(upload.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    normalized_suffixes = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in (allowed_suffixes or [])
    }
    if normalized_suffixes and suffix not in normalized_suffixes:
        allowed = ", ".join(sorted(normalized_suffixes))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported upload type. Allowed extensions: {allowed}",
        )

    limit = maximum_bytes or max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {limit // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)
