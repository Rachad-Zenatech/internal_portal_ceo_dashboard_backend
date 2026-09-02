"""
Cross-Service Persistence Schema
Creates tables for:
1. service_commands: Durable generic command store
2. service_outbox: Transactional outbox for reliable RabbitMQ publishing
3. service_inbox: Deduplication and idempotency for consumed results and events
4. ceo_service_projections: Local filtered read-model projections with freshness metadata
"""

import logging
from postgresql_db.database import get_pool

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = """
-- 1. Generic Command Store
CREATE TABLE IF NOT EXISTS service_commands (
    id BIGSERIAL PRIMARY KEY,
    command_id UUID UNIQUE NOT NULL,
    idempotency_key VARCHAR(255) UNIQUE,
    target_service VARCHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(128) NOT NULL,
    command_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    expected_source_version INT,
    requested_by_user_id VARCHAR(128) NOT NULL,
    requested_by_display_name VARCHAR(255),
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    failure_code VARCHAR(64),
    failure_message TEXT,
    retryable BOOLEAN DEFAULT FALSE,
    correlation_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    processing_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_service_commands_target_status
ON service_commands (target_service, status);

CREATE INDEX IF NOT EXISTS idx_service_commands_resource
ON service_commands (resource_type, resource_id);

CREATE INDEX IF NOT EXISTS idx_service_commands_created_at
ON service_commands (created_at DESC);

-- 2. Transactional Outbox
CREATE TABLE IF NOT EXISTS service_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID UNIQUE NOT NULL,
    command_id UUID,
    exchange_name VARCHAR(128) NOT NULL,
    routing_key VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_service_outbox_status_created
ON service_outbox (status, created_at);

-- 3. Deduplicating Inbox
CREATE TABLE IF NOT EXISTS service_inbox (
    id BIGSERIAL PRIMARY KEY,
    message_id VARCHAR(128) UNIQUE NOT NULL,
    source_service VARCHAR(64) NOT NULL,
    message_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_service_inbox_source_type
ON service_inbox (source_service, message_type);

-- 4. Local Projections Store
CREATE TABLE IF NOT EXISTS ceo_service_projections (
    id BIGSERIAL PRIMARY KEY,
    service_name VARCHAR(64) NOT NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(128) NOT NULL,
    version INT NOT NULL DEFAULT 1,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_status VARCHAR(64),
    last_synchronized_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_stale BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_service_projections UNIQUE (service_name, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_ceo_projections_service_resource
ON ceo_service_projections (service_name, resource_type);

-- 5. Workflow Assignments Store
CREATE TABLE IF NOT EXISTS workflow_assignments (
    id SERIAL PRIMARY KEY,
    role VARCHAR(50) NOT NULL,
    user_id UUID,
    user_ids UUID[],
    team_id UUID,
    request_type VARCHAR(50),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

async def ensure_cross_service_schema():
    """Initializes all cross-service database tables and indices."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(CREATE_TABLES_SQL)

            # Seed base purchasing request projections if empty so offline mode works instantly out of the box
            count = await conn.fetchval("SELECT COUNT(*) FROM ceo_service_projections WHERE service_name = 'administration' AND resource_type = 'purchase_request'")
            if count == 0:
                import json
                seed_requests = [
                    {
                        "id": "6",
                        "department": "HR",
                        "amount": 401.49,
                        "status": "WAITING_APPROVAL",
                        "description": "Request from approver",
                        "product_name": "Request from approver",
                        "priority": "MEDIUM",
                        "requester_name": "Test B",
                        "created_at": "2026-09-01T12:00:00Z",
                    },
                    {
                        "id": "7",
                        "department": "Engineering",
                        "amount": 12450.00,
                        "status": "WAITING_APPROVAL",
                        "description": "Engineering workstation dual-monitor expansion for AI robotics lab",
                        "product_name": "Engineering workstation dual-monitor expansion for AI robotics lab",
                        "priority": "HIGH",
                        "requester_name": "Rachad Quintyne",
                        "created_at": "2026-09-01T14:30:00Z",
                    },
                    {
                        "id": "1",
                        "department": "Operations",
                        "amount": 1250.00,
                        "status": "APPROVED",
                        "description": "Quarterly operational supplies",
                        "product_name": "Quarterly operational supplies",
                        "priority": "LOW",
                        "requester_name": "Operations Lead",
                        "created_at": "2026-08-15T10:00:00Z",
                    },
                    {
                        "id": "2",
                        "department": "DevOps",
                        "amount": 4800.00,
                        "status": "APPROVED",
                        "description": "Enterprise cloud compute reserved instances",
                        "product_name": "Enterprise cloud compute reserved instances",
                        "priority": "HIGH",
                        "requester_name": "DevOps Team",
                        "created_at": "2026-08-18T11:00:00Z",
                    },
                    {
                        "id": "3",
                        "department": "Security",
                        "amount": 9500.00,
                        "status": "APPROVED",
                        "description": "SOC2 annual compliance penetration testing audit",
                        "product_name": "SOC2 annual compliance penetration testing audit",
                        "priority": "HIGH",
                        "requester_name": "Security Officer",
                        "created_at": "2026-08-20T09:00:00Z",
                    },
                    {
                        "id": "4",
                        "department": "Facilities",
                        "amount": 13733.00,
                        "status": "APPROVED",
                        "description": "Executive ergonomics and collaboration pods",
                        "product_name": "Executive ergonomics and collaboration pods",
                        "priority": "NORMAL",
                        "requester_name": "Facilities Mgr",
                        "created_at": "2026-08-22T15:00:00Z",
                    },
                ]
                for r in seed_requests:
                    await conn.execute(
                        """
                        INSERT INTO ceo_service_projections (
                            service_name, resource_type, resource_id, version, data,
                            source_status, last_synchronized_at, is_stale, updated_at
                        ) VALUES ('administration', 'purchase_request', $1, 1, $2, $3, NOW(), false, NOW())
                        ON CONFLICT (service_name, resource_type, resource_id) DO NOTHING
                        """,
                        r["id"],
                        json.dumps(r),
                        r["status"],
                    )
                logger.info("Initialized baseline purchase request projections in CEO local store.")

            # Seed base M&A deals projections if empty
            ma_count = await conn.fetchval("SELECT COUNT(*) FROM ceo_service_projections WHERE service_name = 'ma' AND resource_type = 'deal'")
            if ma_count == 0:
                import json
                seed_ma_deals = [
                    {
                        "id": "1",
                        "company_name": "Apex Drone Dynamics",
                        "industry_name": "Autonomous Robotics",
                        "priority_name": "LOI Sent - Accepted",
                        "priority_color": "#10B981",
                        "revenue": "$18.5M",
                        "state_name": "Texas",
                        "state_code": "TX",
                        "country_name": "United States",
                        "analyst_name": "Marcus Vance",
                        "analyst_email": "m.vance@zenatech.com",
                        "latest_note": "LOI formally accepted by board; entering final confirmatory due diligence.",
                        "stage": "DUE_DILIGENCE",
                        "created_at": "2026-08-10T14:00:00Z",
                        "updated_at": "2026-09-01T15:30:00Z",
                    },
                    {
                        "id": "2",
                        "company_name": "SkyVision AI Systems",
                        "industry_name": "Aerospace & Defense",
                        "priority_name": "LOI Sent",
                        "priority_color": "#3B82F6",
                        "revenue": "$32.0M",
                        "state_name": "California",
                        "state_code": "CA",
                        "country_name": "United States",
                        "analyst_name": "Elena Rostova",
                        "analyst_email": "e.rostova@zenatech.com",
                        "latest_note": "Initial LOI delivered to founder; waiting for counter-proposal on earnout terms.",
                        "stage": "LOI_NEGOTIATION",
                        "created_at": "2026-08-14T09:00:00Z",
                        "updated_at": "2026-09-01T11:00:00Z",
                    },
                    {
                        "id": "3",
                        "company_name": "CyberShield Quantum Defense",
                        "industry_name": "Cybersecurity",
                        "priority_name": "LOI Sent - Accepted",
                        "priority_color": "#10B981",
                        "revenue": "$14.2M",
                        "state_name": "Virginia",
                        "state_code": "VA",
                        "country_name": "United States",
                        "analyst_name": "David Chen",
                        "analyst_email": "d.chen@zenatech.com",
                        "latest_note": "Legal reps completed antitrust review; preparing definitive purchase agreement.",
                        "stage": "DEFINITIVE_AGREEMENT",
                        "created_at": "2026-08-01T10:00:00Z",
                        "updated_at": "2026-08-30T16:00:00Z",
                    },
                    {
                        "id": "4",
                        "company_name": "Orbital Sensor Tech",
                        "industry_name": "Geospatial Intelligence",
                        "priority_name": "In Review",
                        "priority_color": "#F59E0B",
                        "revenue": "$8.4M",
                        "state_name": "Colorado",
                        "state_code": "CO",
                        "country_name": "United States",
                        "analyst_name": "Marcus Vance",
                        "analyst_email": "m.vance@zenatech.com",
                        "latest_note": "Technical architecture evaluation underway with engineering leadership.",
                        "stage": "PRELIMINARY_EVALUATION",
                        "created_at": "2026-08-25T11:30:00Z",
                        "updated_at": "2026-08-28T13:45:00Z",
                    },
                ]
                for deal in seed_ma_deals:
                    await conn.execute(
                        """
                        INSERT INTO ceo_service_projections (
                            service_name, resource_type, resource_id, version, data,
                            source_status, last_synchronized_at, is_stale, updated_at
                        ) VALUES ('ma', 'deal', $1, 1, $2, $3, NOW(), false, NOW())
                        ON CONFLICT (service_name, resource_type, resource_id) DO NOTHING
                        """,
                        deal["id"],
                        json.dumps(deal),
                        deal.get("stage", "ACTIVE"),
                    )
                logger.info("Initialized baseline M&A deal projections in CEO local store.")

            # Seed base Administration tasks projections if empty
            task_count = await conn.fetchval("SELECT COUNT(*) FROM ceo_service_projections WHERE service_name = 'administration' AND resource_type = 'task'")
            if task_count == 0:
                import json
                seed_tasks = [
                    {
                        "id": "1",
                        "title": "Vendor Certificate Verification",
                        "description": "Verify insurance certificate for AI hardware procurement",
                        "status": "COMPLETED",
                        "priority": "HIGH",
                        "assigned_to": "Compliance Lead",
                        "due_date": "2026-09-02T18:00:00Z",
                    },
                    {
                        "id": "2",
                        "title": "Q3 Budget Review Reconciliation",
                        "description": "Cross-check departmental spend against allocated cap",
                        "status": "IN_PROGRESS",
                        "priority": "MEDIUM",
                        "assigned_to": "Finance Officer",
                        "due_date": "2026-09-05T18:00:00Z",
                    },
                ]
                for task in seed_tasks:
                    await conn.execute(
                        """
                        INSERT INTO ceo_service_projections (
                            service_name, resource_type, resource_id, version, data,
                            source_status, last_synchronized_at, is_stale, updated_at
                        ) VALUES ('administration', 'task', $1, 1, $2, $3, NOW(), false, NOW())
                        ON CONFLICT (service_name, resource_type, resource_id) DO NOTHING
                        """,
                        task["id"],
                        json.dumps(task),
                        task["status"],
                    )
                logger.info("Initialized baseline administration task projections in CEO local store.")

        logger.info("Cross-service communication schema successfully ensured.")

    except Exception as exc:
        logger.error(f"Failed ensuring cross-service schema: {exc}", exc_info=True)
        raise

