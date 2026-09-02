"""
Projection Service
Serves local filtered read-models with freshness metadata, decoupling UI reads from remote service availability.
"""

import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from postgresql_db.database import get_pool
from services.service_status_registry import service_status_registry

logger = logging.getLogger(__name__)


class ProjectionService:
    async def get_projection(
        self,
        service_name: str,
        resource_type: str,
        resource_id: str,
    ) -> Dict[str, Any]:
        """
        Retrieves a single projected entity with freshness metadata.
        """
        svc_name = service_name.lower()
        res_type = resource_type.lower()
        pool = get_pool()

        # Check live service status from registry
        svc_status = service_status_registry.get_service_status(svc_name)
        is_online = (svc_status == "online")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data, version, source_status, last_synchronized_at, is_stale, updated_at
                FROM ceo_service_projections
                WHERE service_name = $1 AND resource_type = $2 AND resource_id = $3
                """,
                svc_name,
                res_type,
                str(resource_id),
            )

            if not row:
                return {
                    "data": None,
                    "source_service": svc_name,
                    "source_status": "ONLINE" if is_online else "OFFLINE",
                    "last_synchronized_at": None,
                    "is_stale": not is_online,
                }

            data = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
            return {
                "data": data,
                "version": row["version"],
                "source_service": svc_name,
                "source_status": "ONLINE" if is_online else "OFFLINE",
                "last_synchronized_at": row["last_synchronized_at"].isoformat() if row["last_synchronized_at"] else None,
                "is_stale": row["is_stale"] or (not is_online),
            }

    async def list_projections(
        self,
        service_name: str,
        resource_type: str,
        limit: int = 50,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """
        Lists multiple projected entities of a given resource type with freshness metadata.
        """
        svc_name = service_name.lower()
        res_type = resource_type.lower()
        pool = get_pool()

        svc_status = service_status_registry.get_service_status(svc_name)
        is_online = (svc_status == "online")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource_id, data, version, source_status, last_synchronized_at, is_stale, updated_at
                FROM ceo_service_projections
                WHERE service_name = $1 AND resource_type = $2
                ORDER BY updated_at DESC
                LIMIT $3 OFFSET $4
                """,
                svc_name,
                res_type,
                limit,
                skip,
            )

            items = []
            for r in rows:
                d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
                if "id" not in d:
                    d["id"] = r["resource_id"]
                items.append(d)

            last_sync = max([r["last_synchronized_at"] for r in rows if r["last_synchronized_at"]], default=None)

            return {
                "data": items,
                "total": len(items),
                "source_service": svc_name,
                "source_status": "ONLINE" if is_online else "OFFLINE",
                "last_synchronized_at": last_sync.isoformat() if last_sync else datetime.now(timezone.utc).isoformat(),
                "is_stale": not is_online,
            }

    async def upsert_projection(
        self,
        service_name: str,
        resource_type: str,
        resource_id: str,
        data: Dict[str, Any],
        version: int = 1,
        source_status: Optional[str] = None,
    ) -> None:
        """
        Updates or creates a projected record in the database.
        """
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ceo_service_projections (
                    service_name, resource_type, resource_id, version, data,
                    source_status, last_synchronized_at, is_stale, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, NOW(), false, NOW())
                ON CONFLICT (service_name, resource_type, resource_id)
                DO UPDATE SET
                    version = EXCLUDED.version,
                    data = EXCLUDED.data,
                    source_status = COALESCE(EXCLUDED.source_status, ceo_service_projections.source_status),
                    last_synchronized_at = NOW(),
                    is_stale = false,
                    updated_at = NOW()
                """,
                service_name.lower(),
                resource_type.lower(),
                str(resource_id),
                version,
                json.dumps(data),
                source_status,
            )


projection_service = ProjectionService()
