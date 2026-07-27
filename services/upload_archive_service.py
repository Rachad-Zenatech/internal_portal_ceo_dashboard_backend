import gzip
import json
import mimetypes
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.s3_storage_service import (
    delete_file_from_s3,
    download_file_from_s3,
    upload_file_to_s3,
)


UPLOAD_TYPES = {
    "general-ledger": "General Ledger",
    "bank-statement": "Bank Statement",
    "bank-statement-preview": "Bank Statement Preview",
}
S3_UPLOAD_TYPES = frozenset({"general-ledger", "bank-statement"})
_index_lock = threading.Lock()


def _gzip_level() -> int:
    try:
        configured = int(os.getenv("UPLOAD_GZIP_LEVEL", "6"))
    except (TypeError, ValueError):
        configured = 6
    return max(1, min(9, configured))


def _base_dir() -> Path:
    path = Path(os.getenv("UPLOAD_FILES_DIR", "data/upload_files")).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Test writability
        test_file = path / ".write_test"
        test_file.touch()
        test_file.unlink()
    except (PermissionError, OSError):
        path = Path("/tmp/upload_files")
        path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path() -> Path:
    return _base_dir() / "upload_files.json"


def _safe_filename(filename: Optional[str]) -> str:
    name = Path(filename or "upload.bin").name
    stem = Path(name).stem or "upload"
    suffix = Path(name).suffix
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "upload"
    return f"{stem[:120]}{suffix[:20]}"


def _validate_upload_type(upload_type: str) -> str:
    if upload_type not in UPLOAD_TYPES:
        allowed = ", ".join(sorted(UPLOAD_TYPES))
        raise ValueError(f"upload_type must be one of: {allowed}")
    return upload_type


def _load_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _write_index(items: list[dict[str, Any]]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _resolve_stored_path(relative_path: str) -> Path:
    base = _base_dir()
    resolved = (base / relative_path).resolve()
    if base not in resolved.parents and resolved != base:
        raise FileNotFoundError("Stored upload path is outside the upload archive")
    return resolved


def _s3_key(upload_type: str, stored_at: datetime, file_id: str, filename: str) -> str:
    prefix = os.getenv("UPLOAD_S3_PREFIX", "data").strip("/")
    parts = [
        prefix,
        upload_type,
        stored_at.strftime("%Y"),
        stored_at.strftime("%m"),
        f"{stored_at.strftime('%Y%m%dT%H%M%SZ')}_{file_id}_{filename}",
    ]
    return "/".join(part for part in parts if part)


def archive_upload_bytes(
    content: bytes,
    *,
    upload_type: str,
    filename: Optional[str],
    content_type: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    upload_type = _validate_upload_type(upload_type)
    safe_name = _safe_filename(filename)
    file_id = uuid.uuid4().hex
    stored_at = datetime.now(timezone.utc)
    original_size = len(content)
    detected_type = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

    if upload_type in S3_UPLOAD_TYPES:
        stored_path = _s3_key(upload_type, stored_at, file_id, safe_name)
        upload_file_to_s3(content, stored_path, detected_type)
        storage = "s3"
        stored_size = original_size
    else:
        folder = _base_dir() / upload_type / stored_at.strftime("%Y") / stored_at.strftime("%m")
        folder.mkdir(parents=True, exist_ok=True)
        stored_name = f"{stored_at.strftime('%Y%m%dT%H%M%SZ')}_{file_id}_{safe_name}.gz"
        local_path = folder / stored_name

        with gzip.open(local_path, "wb", compresslevel=_gzip_level()) as gz:
            gz.write(content)

        stored_path = local_path.relative_to(_base_dir()).as_posix()
        storage = "server-gzip"
        stored_size = local_path.stat().st_size

    item = {
        "id": file_id,
        "upload_type": upload_type,
        "upload_type_label": UPLOAD_TYPES[upload_type],
        "filename": safe_name,
        "content_type": detected_type,
        "original_size": original_size,
        "compressed_size": stored_size,
        "compression_percent": (
            round((1 - (stored_size / original_size)) * 100, 1)
            if original_size
            else 0
        ),
        "stored_at": stored_at.isoformat(),
        "storage": storage,
        "path": stored_path,
        "metadata": metadata or {},
    }

    with _index_lock:
        items = _load_index()
        items.append(item)
        _write_index(items)
    return public_upload_item(item)


def public_upload_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "path"}


def list_uploads(upload_type: Optional[str] = None) -> list[dict[str, Any]]:
    if upload_type:
        _validate_upload_type(upload_type)

    items = _load_index()
    if upload_type:
        items = [item for item in items if item.get("upload_type") == upload_type]

    items.sort(key=lambda item: item.get("stored_at", ""), reverse=True)
    return [public_upload_item(item) for item in items]


def get_upload(file_id: str) -> dict[str, Any]:
    for item in _load_index():
        if item.get("id") == file_id:
            return item
    raise FileNotFoundError("Upload not found")


def read_upload(file_id: str) -> tuple[dict[str, Any], bytes]:
    item = get_upload(file_id)
    if item.get("storage") == "s3":
        return item, download_file_from_s3(item["path"])

    path = _resolve_stored_path(item["path"])
    if not path.exists():
        raise FileNotFoundError("Archived upload file is missing")
    with gzip.open(path, "rb") as gz:
        return item, gz.read()


def delete_upload(file_id: str) -> bool:
    with _index_lock:
        items = _load_index()
        next_items = []
        deleted_item = None

        for item in items:
            if item.get("id") == file_id:
                deleted_item = item
            else:
                next_items.append(item)

        if deleted_item is None:
            return False

        if deleted_item.get("storage") == "s3":
            delete_file_from_s3(deleted_item["path"])
        else:
            _resolve_stored_path(deleted_item["path"]).unlink(missing_ok=True)

        _write_index(next_items)

    return True
