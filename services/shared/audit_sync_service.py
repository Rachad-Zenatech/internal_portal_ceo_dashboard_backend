"""Cross-project audit log ingestion for CEO Dashboard over RabbitMQ Message Hub."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

_schema_ready = False

SERVICE_DISPLAY_NAMES: Dict[str, str] = {
    "ceo": "CEO Dashboard",
    "administration": "Administration",
    "admin": "Administration",
    "ma": "M&A",
    "m7a": "M&A",
    "finance": "Finance",
    "marketing": "Marketing",
    "real_estate": "Real Estate",
    "social_media_compliance": "Social Media Compliance",
    "travel": "Travel Management",
    "sec_filing": "SEC Filing",
    "share_document": "SEC Filing",
    "template_automation": "Template Automation",
}


async def ensure_audit_sync_columns(conn) -> None:
    """Idempotently add cross-project metadata columns to audit_logs."""
    global _schema_ready
    if _schema_ready:
        return
    try:
        await conn.execute(
            """
            ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS source_service VARCHAR(100) DEFAULT 'ceo';
            ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_name VARCHAR(255);
            ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_email VARCHAR(255);
            ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(128);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_logs_source_event_id
                ON audit_logs (source_event_id)
                WHERE source_event_id IS NOT NULL;
            """
        )
        _schema_ready = True
    except Exception as exc:
        logger.warning(f"Could not ensure audit_logs sync columns: {exc}")


def _parse_uuid(val: Any) -> Optional[UUID]:
    if not val:
        return None
    try:
        return UUID(str(val))
    except Exception:
        return None


def _parse_timestamp(val: Any) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _to_json_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, default=str)
    except Exception:
        return json.dumps({"raw": str(val)})


async def ingest_synced_audit_log(message: Dict[str, Any]) -> bool:
    """Persist an audit log published from any project into CEO audit_logs and ceo_audit_logs."""
    source_service = str(message.get("source_service") or message.get("service") or "unknown").strip().lower()
    source_label = str(message.get("source_label") or SERVICE_DISPLAY_NAMES.get(source_service, source_service.title()))
    action = str(message.get("action") or "SYSTEM_EVENT").strip()[:255]
    entity_type = str(message.get("entity_type") or "System").strip()[:255]
    entity_id = str(message.get("entity_id") or "").strip()[:255]
    actor_user_id = _parse_uuid(message.get("actor_user_id"))
    actor_name = (str(message.get("actor_name")).strip()[:255]) if message.get("actor_name") else None
    actor_email = (str(message.get("actor_email")).strip()[:255]) if message.get("actor_email") else None
    event_id = str(message.get("event_id") or "").strip()[:128] or None
    old_val = message.get("old_value")
    new_val = message.get("new_value")
    ip_address = (str(message.get("ip_address")).strip()[:255]) if message.get("ip_address") else None
    user_agent = str(message.get("user_agent")) if message.get("user_agent") else None
    created_at = _parse_timestamp(message.get("created_at") or message.get("timestamp"))

    pool = get_pool()
    async with pool.acquire() as conn:
        await ensure_audit_sync_columns(conn)
        inserted_row = await conn.fetchrow(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id,
                old_value, new_value, ip_address, user_agent,
                created_at, source_service, actor_name, actor_email, source_event_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
            RETURNING id
            """,
            actor_user_id,
            action,
            entity_type,
            entity_id,
            _to_json_str(old_val),
            _to_json_str(new_val),
            ip_address,
            user_agent,
            created_at,
            source_service,
            actor_name,
            actor_email,
            event_id,
        )
        if event_id and not inserted_row:
            return False

        # Mirror into ceo_audit_logs for the executive dashboard Audit Trail tab
        try:
            target_entity = f"{entity_type} #{entity_id}" if entity_id else entity_type
            requested_by = actor_name or actor_email or (str(actor_user_id) if actor_user_id else "System")
            details_payload = {
                "source_service": source_service,
                "source_label": source_label,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "old_value": old_val,
                "new_value": new_val,
                "event_id": event_id,
            }
            await conn.execute(
                """
                INSERT INTO ceo_audit_logs (
                    action, source_application, target_application, target_entity,
                    requested_by, result, details, created_at
                )
                VALUES ($1, $2, 'ceo_dashboard', $3, $4, 'SYNCED', $5, $6)
                """,
                action,
                source_label,
                target_entity,
                requested_by,
                json.dumps(details_payload, default=str),
                created_at,
            )
        except Exception as mirror_err:
            logger.debug(f"Could not mirror synced audit log into ceo_audit_logs: {mirror_err}")

    # Real-time UI cache invalidation via WebSocket & SSE
    realtime_payload = {
        "eventType": "AUDIT_LOG_SYNCED",
        "event_type": "AUDIT_LOG_SYNCED",
        "service": source_service,
        "source": source_service,
        "entityId": entity_id,
        "affected_query_keys": ["auditLogs", "auditLogs-native", "ceoAuditLogs"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from tools.integration.service_status_router import ws_manager
        await ws_manager.broadcast(realtime_payload)
    except Exception as ws_err:
        logger.debug(f"Audit log WS broadcast skipped: {ws_err}")
    try:
        from tools.integration.ceo_integration_router import broadcast_event
        await broadcast_event(realtime_payload)
    except Exception as sse_err:
        logger.debug(f"Audit log SSE broadcast skipped: {sse_err}")

    return True


async def list_synced_audit_logs(limit: int = 1000):
    """Fetch local + cross-project synced audit logs for CEO Dashboard."""
    pool = await get_pool() if callable(getattr(get_pool, "__call__", None)) else get_pool()
    if hasattr(pool, "__await__"):
        pool = await pool
    async with pool.acquire() as conn:
        await ensure_audit_sync_columns(conn)
        rows = await conn.fetch(
            """
            SELECT a.*,
                   COALESCE(u.full_name, a.actor_name, 'System') as actor_name,
                   COALESCE(u.email, a.actor_email) as actor_email,
                   COALESCE(a.source_service, 'ceo') as source_service
            FROM audit_logs a
            LEFT JOIN users u ON a.actor_user_id = u.id
            ORDER BY a.created_at DESC
            LIMIT $1
            """,
            limit,
        )
    result = []
    for r in rows:
        d = dict(r)
        for key in ("old_value", "new_value"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
            if isinstance(d.get(key), dict):
                d[key].pop("password", None)
                d[key].pop("password_hash", None)
        result.append(d)
    return result


def apply_urgent_override_fields(parsed: Dict[str, Any], raw_item: Dict[str, Any], quote_data_val: Any) -> Dict[str, Any]:
    """Attach urgent payable override flags, priority, and reason onto parsed purchase request dicts."""
    is_urgent = bool(isinstance(quote_data_val, dict) and quote_data_val.get("is_urgent_override"))
    reason = (quote_data_val.get("urgent_override_reason") if isinstance(quote_data_val, dict) else None) or None
    parsed["is_urgent_override"] = is_urgent
    parsed["urgent_override_reason"] = reason
    if is_urgent:
        parsed["priority"] = "URGENT"
        if not parsed.get("approval_note") and reason:
            parsed["approval_note"] = f"Urgent Override: {reason}"
    return parsed


def _history_sort_key(item: Dict[str, Any]) -> tuple[float, int]:
    qd = item.get("quote_data") if isinstance(item.get("quote_data"), dict) else {}
    ts_val = 0.0
    for candidate in (
        item.get("approved_at"),
        item.get("level_2_approved_at"),
        item.get("level_1_approved_at"),
        qd.get("urgent_override_at"),
        item.get("created_at"),
        item.get("request_date"),
    ):
        if not candidate:
            continue
        try:
            if isinstance(candidate, datetime):
                dt = candidate if candidate.tzinfo else candidate.replace(tzinfo=timezone.utc)
            else:
                s = str(candidate).strip().replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            ts_val = dt.timestamp()
            break
        except Exception:
            continue
    raw_id = str(item.get("id") or "")
    num_id = int(raw_id) if raw_id.isdigit() else 0
    return (ts_val, num_id)


def sort_approval_history_desc(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort approval history items newest-first by approval/override/creation date descending."""
    return sorted(items, key=_history_sort_key, reverse=True)

