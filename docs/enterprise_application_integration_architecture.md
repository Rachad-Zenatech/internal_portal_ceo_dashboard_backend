# Enterprise Application Integration Architecture

## 1. Executive Summary & Paradigm Shift

Historically, application-to-application integrations were designed with the CEO Dashboard at the center of the communication pipeline:
$$\text{Applications} \longrightarrow \text{Integration Layer} \longrightarrow \text{CEO Dashboard} \longrightarrow \text{Target Application APIs}$$

While this model works when the CEO Dashboard is the sole consumer or initiator of actions, it introduces severe architectural bottlenecks when domain applications (**Admin, Finance, CEO Dashboard**) need to interact directly.

### The Core Paradigm Shift
1. **The CEO Dashboard is an Application, Not the Integration Hub:** The CEO Dashboard is an executive visibility and command consumer on top of the enterprise integration platform. It is **not** a transit hub or a hard runtime dependency for application-to-application traffic.
2. **Autonomous System Resilience:** Applications must remain 100% operational, able to process workflows, exchange data, and execute transactions even when the CEO Dashboard is offline or undergoing maintenance.
3. **Decoupled Enterprise Integration Layer:** Replaces brittle point-to-point application meshes with a standardized two-tier communication layer:
   - **Synchronous Commands & Queries:** Via an **API Gateway**.
   - **Asynchronous Business Events:** Via **Amazon EventBridge + SQS**.
4. **Machine-to-Machine (M2M) Scope-Based Authorization:** Each application is granted strictly limited, whitelisted scopes under the Principle of Least Privilege.

---

## 2. Enterprise Integration Architecture Topology

```
                         ┌─────────────────────────────────┐
                         │          CEO DASHBOARD          │
                         │                                 │
                         │  • Executive KPIs & Metrics     │
                         │  • Multi-System Approvals       │
                         │  • Cross-Domain Audit History   │
                         └────────────────┬────────────────┘
                                          │
                                    APIs / Events
                                          │
              ┌───────────────────────────┴───────────────────────────┐
              │              ENTERPRISE INTEGRATION LAYER             │
              │                                                       │
              │   ┌────────────────────────┐  ┌───────────────────┐   │
              │   │      API Gateway       │  │  Event Platform   │   │
              │   │  (Commands & Queries)  │  │ (EventBridge+SQS) │   │
              │   └───────────┬────────────┘  └─────────┬─────────┘   │
              │               │                         │             │
              │   • AuthN / AuthZ Enforcement           │             │
              │   • M2M Scope Policy Enforcement        │             │
              │   • Rate Limiting & Routing             │             │
              │   • Schema & Contract Validation        │             │
              │   • Distributed Tracing & Observability │             │
              └───────────────┼─────────────────────────┼─────────────┘
                              │                         │
              ┌───────────────┴─────────────────────────┴───────────────┐
              │                                                         │
              ▼                                                         ▼
        ┌───────────┐                                             ┌───────────┐
        │   Admin   │                                             │  Finance  │
        │  Service  │                                             │  Service  │
        │ ┌───────┐ │                                             │ ┌───────┐ │
        │ │  DB   │ │                                             │ │  DB   │ │
        │ └───────┘ │                                             │ └───────┘ │
        └───────────┘                                             └───────────┘
              │                                                         │
              ▼                                                         ▼
          Purchasing & AP                                           GL & Accounting
```

---

## 3. Application Domain Ownership & Autonomy

* **Database Isolation (Database-per-Service):** Each application possesses exclusive ownership over its persistent data store. **Direct cross-database queries or shared databases between applications are strictly prohibited.**
* **Encapsulated Business Logic:** Business rules and invariants are enforced by the domain application owning the entity, never inside the gateway or integration broker.
* **Independent Scalability:** Each domain service deploys, scales, and manages its lifecycle independently without requiring lock-step deployments.

---

## 4. Communication Patterns

The enterprise architecture mandates two distinct communication channels based on interaction semantics.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ PATTERN 1: SYNCHRONOUS COMMANDS & QUERIES (API Gateway)                       │
│ "I need you to do something or give me information right now"                  │
├────────────────────────────────────────────────────────────────────────────────┤
│ • Strict Request-Response lifecycle with immediate HTTP status                 │
│ • Point-to-Domain via API Gateway (mTLS / OAuth2 Scoped Service Tokens)        │
│ • Synchronous validation and transaction commitment                            │
└────────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────────┐
│ PATTERN 2: ASYNCHRONOUS BUSINESS EVENTS (EventBridge + SQS)                    │
│ "Something has happened within my domain"                                      │
├────────────────────────────────────────────────────────────────────────────────┤
│ • Fire-and-forget broadcast from publishing service                            │
│ • Multi-subscriber fan-out (1 Publisher -> N Consumers)                        │
│ • Decoupled queuing with guaranteed delivery, retries, and DLQ                │
└────────────────────────────────────────────────────────────────────────────────┘
```

---

### 4.1 Pattern 1: Synchronous API Integration (Commands & Queries)

Used when the caller requires immediate execution confirmation, input validation, or data query results.

```
┌─────────────┐               ┌─────────────┐               ┌─────────────┐
│ Application │               │ API Gateway │               │ Application │
│   (Caller)  │               │             │               │  (Target)   │
└──────┬──────┘               └──────┬──────┘               └──────┬──────┘
       │                             │                             │
       │ POST /api/v1/finance/cost   │                             │
       │ (Bearer Scoped Token)       │                             │
       ├────────────────────────────►│                             │
       │                             │ 1. Validate Token & Scopes  │
       │                             │ 2. Check Rate Limits        │
       │                             │ 3. Forward to Target        │
       │                             ├────────────────────────────►│
       │                             │                             │ 4. Validate Rules
       │                             │                             │ 5. Execute & Commit
       │                             │                             │ 6. Return Payload
       │                             │◄────────────────────────────┤
       │◄────────────────────────────┤                             │
       │ 200 OK / 201 Created        │                             │
```

---

### 4.2 Pattern 2: Asynchronous Event-Driven Integration

Used when an event occurs in a domain that other subsystems need to react to without blocking the originating transaction.

```
                        ┌──────────────────────────────┐
                        │   Originating Application    │
                        │    (e.g., Purchasing AP)     │
                        └──────────────┬───────────────┘
                                       │
                                       │ 1. PutEvents (PURCHASE_APPROVED)
                                       ▼
                        ┌──────────────────────────────┐
                        │   AWS EventBridge Bus        │
                        │     (Enterprise Bus)         │
                        └──────┬───────────────┬───────┘
                               │               │
            ┌──────────────────┴──┐         ┌──┴──────────────────┐
            │ Rule: Target Finance│         │ Rule: Target CEO    │
            ▼                     ▼         ▼                     ▼
     ┌─────────────┐       ┌─────────────┐   ┌─────────────┐       ┌─────────────┐
     │ Finance SQS │       │  Admin SQS  │   │   CEO SQS   │       │   DLQ SQS   │
     │    Queue    │       │    Queue    │   │    Queue    │       │    Queue    │
     └──────┬──────┘       └──────┬──────┘   └──────┬──────┘       └─────────────┘
            ▼                     ▼                 ▼
     ┌─────────────┐       ┌─────────────┐   ┌─────────────┐
     │   Finance   │       │    Admin    │   │   CEO App   │
     │   Service   │       │   Service   │   │   Service   │
     └─────────────┘       └─────────────┘   └─────────────┘
```

#### Event Contract Standard:
```json
{
  "version": "1.0",
  "id": "c1f72b9a-4284-4b51-9e79-58b9f1d07c34",
  "source": "zenatech.purchasing",
  "detail-type": "PURCHASE_APPROVED",
  "time": "2026-09-10T14:30:00Z",
  "resources": ["urn:zenatech:po:REQ-10523"],
  "detail": {
    "entity_id": "REQ-10523",
    "vendor_id": "VEND-8821",
    "amount": 45000.00,
    "currency": "USD",
    "approved_by": "user-uuid-91",
    "department": "Engineering"
  }
}
```

---

## 5. Application-to-Application Scope & Security Policy

To prevent cross-system privilege escalation and data leakage, all inter-service communication is governed by an **Application Access Control Matrix** enforced via `services/service_security_policy.py`.

### 5.1 Application Access Matrix

```
┌─────────────────┬─────────────────────────────────────────────────┬────────────────────────────────┐
│ Application     │ Allowed Inbound Scopes                          │ Explicitly Restricted Actions  │
├─────────────────┼─────────────────────────────────────────────────┼────────────────────────────────┤
│ Admin Portal    │ * (Full Administrative Control)                 │ N/A                            │
│ CEO Dashboard   │ metrics:read, approvals:read/write,             │ Raw DB drops, credential dumps,│
│                 │ service_status:read, purchasing:summary_read,   │ user password alterations      │
│                 │ audit:read/write                                │                                │
│ Finance / AP    │ purchasing:read, purchasing:status_update,      │ User role administration,      │
│                 │ coa:read, coa:sync, company:read/write,         │ system logs deletion           │
│                 │ business_contacts:read                          │                                │
└─────────────────┴─────────────────────────────────────────────────┴────────────────────────────────┘
```

### 5.2 Inbound Scope Guard (`require_application_scope`)
Routes authenticate service identity and verify granted scopes:
```python
@router.get(
    "/api/v1/integrations/companies",
    dependencies=[Depends(require_application_scope("company:read", allowed_services=["admin", "ceo-dashboard"]))]
)
async def get_companies():
    return await get_companies()
```
* If an unauthorized application attempts an unpermitted call, the guard halts execution immediately with `HTTP 403 Forbidden`.

### 5.3 Outbound Scoped Token Generation
When sending requests to downstream systems, services sign scoped JWT tokens requesting only the minimum required operations:
```python
token = generate_scoped_outbound_token(
    target_service="finance",
    requested_scopes=["coa:read", "company:read", "business_contacts:read"]
)
```

---

## 6. Role of the CEO Dashboard in the Architecture

The CEO Dashboard participates as an advanced consumer and commander within the Enterprise Integration Layer:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              CEO DASHBOARD ROLE                                 │
├────────────────────────────────┬────────────────────────────────────────────────┤
│ INBOUND (Consumer)             │ OUTBOUND (Commander)                           │
├────────────────────────────────┼────────────────────────────────────────────────┤
│ • Subscribes to enterprise-wide│ • Issues cross-application executive commands  │
│   EventBridge streams via SQS. │   via the API Gateway (e.g. approve cap-ex,   │
│ • Aggregates KPIs, metrics,    │   freeze project budgets, lock company books). │
│   and performance benchmarks.  │ • Enforces multi-application authorization and │
│ • Maintains executive auditing.│   executive signing policies.                  │
└────────────────────────────────┴────────────────────────────────────────────────┘
```

---

## 7. Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails | Correct Solution |
| :--- | :--- | :--- |
| **CEO Dashboard as Hub** | Single point of failure; breaks service-to-service communication when dashboard is down. | Enterprise Integration Layer (API Gateway + EventBridge). |
| **Point-to-Point Mesh** | $N \times (N - 1)$ connections create unmaintainable spaghetti dependencies. | Centralized Event Bus + API Gateway routing. |
| **Cross-Database Querying** | Breaks schema boundaries, creates hidden coupling, and risks lock contentions. | REST/gRPC API queries and async event projections. |
| **Fat Gateway Logic** | Putting domain rules in the gateway leads to centralized deployment bottlenecks. | Dumb routing in Gateway; smart domain logic in microservices. |
| **Universal Super Tokens** | Machine-to-Machine tokens with wildcard access risk total compromise if leaked. | Scoped M2M tokens enforced by Application Security Policies. |

---

## 8. Reliability, Circuit Breakers & Dead-Letter Queues

1. **Enterprise Circuit Breaker (`CircuitBreaker`):**
   - Each integration client uses `CircuitBreaker` (`CLOSED`, `OPEN`, `HALF_OPEN`).
   - Trips open immediately when a downstream system slows down or fails, preventing connection pileup.
2. **In-Memory Last-Known-Good Cache (`ResilientCache`):**
   - Retains the last valid response and serves it with `status: "stale"` and 0ms latency during downstream outages.
3. **Dead-Letter Queues (DLQ) & Retry Policy:**
   - SQS queues route unprocessable messages to dedicated DLQs after 3 attempts with exponential backoff and jitter.

---

## 9. Implementation & Migration Roadmap

```
Phase 1: Standardization & Security
  ├── Define OpenAPI 3.1 & CloudEvents Schemas for all applications
  ├── Deploy Scope Authorization & Security Policies across services
  └── Configure API Gateway routing & Entra ID token validation policies

Phase 2: Event-Driven Backbone
  ├── Provision AWS EventBridge custom enterprise event bus
  ├── Create per-application dedicated SQS queues & DLQs
  └── Refactor services to emit domain events on state transitions

Phase 3: CEO Dashboard Realignment
  ├── Reconnect CEO Dashboard as an EventBridge consumer
  ├── Direct all CEO commands through the Enterprise API Gateway
  └── Decommission direct transit coupling
```
