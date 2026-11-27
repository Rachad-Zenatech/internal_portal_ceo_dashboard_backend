import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.uploads.s3_storage_service import (
    delete_file_from_s3,
    download_file_from_s3,
    get_configured_bucket_name,
    is_s3_configured,
    list_files_in_s3,
    upload_file_to_s3,
)

logger = logging.getLogger(__name__)

UPLOAD_TYPES = {
    "general-ledger": "General Ledger",
    "bank-statement": "Bank Statement",
    "financial-report": "Financial Report",
    "executive-document": "Executive Document",
    "bank-statement-preview": "Bank Statement Preview",
}
S3_UPLOAD_TYPES = frozenset({"general-ledger", "bank-statement", "financial-report", "executive-document"})
_S3_NAME_RE = re.compile(r"^(?P<ts>\d{8}T\d{6}Z)_(?P<fid>[0-9a-f]{32})_(?P<name>.+)$")
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
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (json.JSONDecodeError, OSError):
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


def _s3_prefix() -> str:
    return os.getenv("UPLOAD_S3_PREFIX", "data").strip("/")


def _s3_key(upload_type: str, stored_at: datetime, file_id: str, filename: str) -> str:
    parts = [
        _s3_prefix(),
        upload_type,
        stored_at.strftime("%Y"),
        stored_at.strftime("%m"),
        f"{stored_at.strftime('%Y%m%dT%H%M%SZ')}_{file_id}_{filename}",
    ]
    return "/".join(part for part in parts if part)


def _store_local_gzip(upload_type: str, stored_at: datetime, file_id: str, safe_name: str, content: bytes) -> tuple[str, int]:
    folder = _base_dir() / upload_type / stored_at.strftime("%Y") / stored_at.strftime("%m")
    folder.mkdir(parents=True, exist_ok=True)
    stored_name = f"{stored_at.strftime('%Y%m%dT%H%M%SZ')}_{file_id}_{safe_name}.gz"
    local_path = folder / stored_name
    with gzip.open(local_path, "wb", compresslevel=_gzip_level()) as gz:
        gz.write(content)
    return local_path.relative_to(_base_dir()).as_posix(), local_path.stat().st_size


def get_storage_status() -> dict[str, Any]:
    """Return active storage backend details for UI diagnostics."""
    s3_ready = is_s3_configured()
    return {
        "mode": "s3" if s3_ready else "local",
        "s3_configured": s3_ready,
        "bucket": get_configured_bucket_name(),
    }


def _s3_obj_to_item(obj: dict[str, Any]) -> dict[str, Any]:
    key = str(obj.get("key") or "")
    prefix = _s3_prefix()
    rel = key[len(prefix) + 1 :] if prefix and key.startswith(f"{prefix}/") else key
    parts = [p for p in rel.split("/") if p]
    leaf = parts[-1] if parts else "upload.bin"

    inferred_type = parts[0] if parts and parts[0] in UPLOAD_TYPES else "executive-document"
    match = _S3_NAME_RE.match(leaf)
    if match:
        file_id = match.group("fid")
        filename = match.group("name")
    else:
        file_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        filename = leaf

    last_mod = obj.get("last_modified")
    stored_at = (
        last_mod.astimezone(timezone.utc).isoformat()
        if isinstance(last_mod, datetime)
        else datetime.now(timezone.utc).isoformat()
    )
    size = int(obj.get("size") or 0)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "id": file_id,
        "upload_type": inferred_type,
        "upload_type_label": UPLOAD_TYPES.get(inferred_type, inferred_type.replace("-", " ").title()),
        "filename": filename,
        "content_type": content_type,
        "original_size": size,
        "compressed_size": size,
        "compression_percent": 0,
        "stored_at": stored_at,
        "storage": "s3",
        "path": key,
        "metadata": {"source": "s3-discovered"},
    }


def _sync_s3_into_index() -> tuple[list[dict[str, Any]], Optional[str]]:
    """Merge S3 objects into the local index so files survive container restarts."""
    with _index_lock:
        items = _load_index()
        if not is_s3_configured():
            return items, None
        try:
            s3_objects = list_files_in_s3(_s3_prefix())
        except Exception as exc:
            logger.warning("Could not sync S3 uploads index: %s", exc)
            return items, f"S3 bucket sync unavailable ({exc}). Showing local index."

        by_path = {item.get("path"): item for item in items if item.get("storage") == "s3" and item.get("path")}
        s3_keys = set()
        changed = False
        for obj in s3_objects:
            key = obj.get("key")
            if not key:
                continue
            s3_keys.add(key)
            if key not in by_path:
                items.append(_s3_obj_to_item(obj))
                changed = True

        # Drop stale s3 entries that were deleted externally from the bucket
        filtered = [item for item in items if item.get("storage") != "s3" or item.get("path") in s3_keys]
        if len(filtered) != len(items):
            items = filtered
            changed = True

        if changed:
            try:
                _write_index(items)
            except OSError as exc:
                logger.warning("Could not persist synced upload index: %s", exc)
        return items, None


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

    storage = "server-gzip"
    stored_path = ""
    stored_size = original_size

    if upload_type in S3_UPLOAD_TYPES and is_s3_configured():
        try:
            stored_path = _s3_key(upload_type, stored_at, file_id, safe_name)
            upload_file_to_s3(content, stored_path, detected_type)
            storage = "s3"
            stored_size = original_size
        except Exception as exc:
            logger.warning("S3 upload failed for %s (%s); falling back to local gzip storage.", safe_name, exc)
            stored_path, stored_size = _store_local_gzip(upload_type, stored_at, file_id, safe_name, content)
            storage = "server-gzip"
    else:
        stored_path, stored_size = _store_local_gzip(upload_type, stored_at, file_id, safe_name, content)

    item = {
        "id": file_id,
        "upload_type": upload_type,
        "upload_type_label": UPLOAD_TYPES[upload_type],
        "filename": safe_name,
        "content_type": detected_type,
        "original_size": original_size,
        "compressed_size": stored_size,
        "compression_percent": round((1 - (stored_size / original_size)) * 100, 1) if original_size else 0,
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


def list_uploads_with_status(upload_type: Optional[str] = None) -> tuple[list[dict[str, Any]], Optional[str]]:
    if upload_type:
        _validate_upload_type(upload_type)

    items, warning = _sync_s3_into_index()
    if upload_type:
        items = [item for item in items if item.get("upload_type") == upload_type]

    items.sort(key=lambda item: item.get("stored_at", ""), reverse=True)
    return [public_upload_item(item) for item in items], warning


def list_uploads(upload_type: Optional[str] = None) -> list[dict[str, Any]]:
    items, _ = list_uploads_with_status(upload_type)
    return items


def get_upload(file_id: str) -> dict[str, Any]:
    for item in _load_index():
        if item.get("id") == file_id:
            return item
    items, _ = _sync_s3_into_index()
    for item in items:
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
