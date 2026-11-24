"""Combined AI health, feature-to-core mapping, and usage overview for System & Security."""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from postgresql_db.database import fetch_all

_FEATURE_CATALOG: List[Dict[str, Any]] = [
    {
        "feature": "CHAT",
        "purpose": "Answers executive KPI, financial, and operational questions via MCP tools.",
        "primary_core": "bedrock_llm",
        "cores": [
            "bedrock_llm",
            "titan_embeddings"
        ],
        "routed": true
    },
    {
        "feature": "EMBEDDINGS",
        "purpose": "Generates vector embeddings for semantic search and context retrieval.",
        "primary_core": "titan_embeddings",
        "cores": [
            "titan_embeddings"
        ],
        "routed": false
    }
]

_CORE_LABELS: Dict[str, str] = {
    "bedrock_llm": "AWS Bedrock (Claude LLM)",
    "titan_embeddings": "Amazon Titan + Vector Store",
}


def _has_core(core_id: str) -> bool:
    return any(core_id in f["cores"] for f in _FEATURE_CATALOG)


def _routing_tier(feature: str, routed: bool) -> str:
    if not routed:
        return "DIRECT"
    try:
        from services.ai.core import model_router
        return model_router.base_tier(feature)
    except Exception:
        return "STANDARD"


def _features_by_core() -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for entry in _FEATURE_CATALOG:
        for cid in entry["cores"]:
            grouped.setdefault(cid, []).append(entry["feature"])
    return grouped


def _get_bedrock_service():
    for mod in ("services.ai.core.bedrock_service", "services.ai.bedrock_service"):
        try:
            return __import__(mod, fromlist=["*"])
        except Exception:
            continue
    return None


def _get_ai_health():
    try:
        from services.ai.observability import ai_health
        return ai_health
    except Exception:
        return None


def _get_ai_usage_service():
    try:
        from services.ai.observability import ai_usage_service
        return ai_usage_service
    except Exception:
        return None


async def _fetch_usage_aggregates(hours: int) -> Dict[str, Any]:
    try:
        model_rows = await fetch_all(
            """
            SELECT feature, model,
                   COUNT(*)::int AS calls,
                   COUNT(*) FILTER (WHERE status = 'ERROR')::int AS errors,
                   COUNT(*) FILTER (WHERE status = 'BLOCKED')::int AS blocked,
                   COUNT(*) FILTER (WHERE status = 'CACHE_HIT')::int AS cache_hits,
                   COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
                   ROUND(AVG(latency_ms))::int AS avg_latency_ms,
                   PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_latency_ms
            FROM ai_prompt_logs
            WHERE created_at > NOW() - make_interval(hours => $1)
            GROUP BY feature, model
            ORDER BY calls DESC
            """,
            hours,
        )
    except Exception:
        model_rows = []

    try:
        feature_last = await fetch_all(
            """
            SELECT DISTINCT ON (feature) feature, status AS last_status, error_type AS last_error_type, created_at AS last_call_at
            FROM ai_prompt_logs
            WHERE created_at > NOW() - make_interval(hours => $1)
            ORDER BY feature, created_at DESC
            """,
            hours,
        )
    except Exception:
        feature_last = []

    try:
        step_rows = await fetch_all(
            """
            SELECT kind, name,
                   COUNT(*)::int AS calls,
                   COUNT(*) FILTER (WHERE status <> 'OK')::int AS not_ok,
                   ROUND(AVG(duration_ms))::int AS avg_ms,
                   PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::int AS p95_ms
            FROM ai_trace_spans
            WHERE created_at > NOW() - make_interval(hours => $1)
            GROUP BY kind, name
            ORDER BY calls DESC
            """,
            hours,
        )
    except Exception:
        step_rows = []

    try:
        guard_rows = await fetch_all(
            """
            SELECT feature, guard_verdict, COUNT(*)::int AS calls
            FROM ai_prompt_logs
            WHERE created_at > NOW() - make_interval(hours => $1)
              AND guard_verdict IS NOT NULL
            GROUP BY feature, guard_verdict
            ORDER BY calls DESC
            """,
            hours,
        )
    except Exception:
        guard_rows = []

    return {
        "models": [dict(r) for r in model_rows],
        "feature_last": {r["feature"]: dict(r) for r in feature_last},
        "steps": [dict(r) for r in step_rows],
        "guards": [dict(r) for r in guard_rows],
    }


async def get_ai_overview(hours: int = 24, user_id: Optional[UUID] = None) -> Dict[str, Any]:
    usage = await _fetch_usage_aggregates(hours)
    ai_health = _get_ai_health()
    health_dict = ai_health.get_status_dict() if ai_health else {"available": True, "reason": None, "feature_issues": []}

    feature_issues = {}
    for issue in health_dict.get("feature_issues", []):
        for f in issue.get("affected_features") or []:
            feature_issues[f] = issue

    usage_service = _get_ai_usage_service()
    quota_by_feature: Dict[str, Any] = {}
    if usage_service and hasattr(usage_service, "get_user_usage_summary") and user_id is not None:
        try:
            q = await usage_service.get_user_usage_summary(user_id)
            quota_by_feature = q.get("features") or {}
        except Exception:
            quota_by_feature = {}

    model_rows = usage["models"]
    step_rows = usage["steps"]
    by_core = _features_by_core()

    bedrock_svc = _get_bedrock_service()
    has_key = bool(getattr(bedrock_svc, "has_ai_key", lambda: False)()) if bedrock_svc else False
    bedrock_profile = (getattr(bedrock_svc, "get_bedrock_profile_id", lambda: None)() or "Not set") if bedrock_svc else "Not set"
    bedrock_region = (getattr(bedrock_svc, "get_bedrock_region", lambda: None)() or "Not set") if bedrock_svc else "Not set"

    llm_calls = sum(int(r.get("calls") or 0) for r in model_rows)
    llm_tokens = sum(int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0) for r in model_rows)

    bedrock_status = "OUT"
    if has_key:
        if not health_dict.get("available", True):
            reason = health_dict.get("reason")
            bedrock_status = "OVERFLOW" if reason in {"QUOTA", "BUSY"} else "OUT"
        else:
            bedrock_status = "OK" if llm_calls > 0 else "READY"

    cores: List[Dict[str, Any]] = [
        {
            "core": "bedrock_llm",
            "name": _CORE_LABELS["bedrock_llm"],
            "provider": f"AWS Bedrock ({bedrock_region})",
            "status": bedrock_status,
            "configured": has_key,
            "model": bedrock_profile,
            "detail": health_dict.get("message") or f"Region {bedrock_region}",
            "note": "Primary LLM engine for chat, extraction, and analysis features.",
            "calls": llm_calls,
            "tokens": llm_tokens,
            "features": by_core.get("bedrock_llm", []),
        }
    ]

    if _has_core("titan_embeddings"):
        embed_calls = sum(int(r.get("calls") or 0) for r in step_rows if r.get("kind") in {"embedding", "retrieval"})
        cores.append(
            {
                "core": "titan_embeddings",
                "name": _CORE_LABELS["titan_embeddings"],
                "provider": "AWS Bedrock / Vector Store",
                "status": "OK" if embed_calls > 0 else ("READY" if has_key else "UNCONFIGURED"),
                "configured": has_key,
                "model": "amazon.titan-embed-text-v2:0",
                "detail": f"{embed_calls} embedding/retrieval steps in window",
                "note": "Vector embeddings for semantic search and RAG.",
                "calls": embed_calls,
                "tokens": 0,
                "features": by_core.get("titan_embeddings", []),
            }
        )

    core_status_map = {c["core"]: c["status"] for c in cores}
    features: List[Dict[str, Any]] = []
    for entry in _FEATURE_CATALOG:
        fname = entry["feature"]
        f_models = [r for r in model_rows if (r.get("feature") or "").upper() == fname]
        calls = sum(int(r.get("calls") or 0) for r in f_models)
        errors = sum(int(r.get("errors") or 0) for r in f_models)
        blocked = sum(int(r.get("blocked") or 0) for r in f_models)
        cache_hits = sum(int(r.get("cache_hits") or 0) for r in f_models)
        in_tok = sum(int(r.get("input_tokens") or 0) for r in f_models)
        out_tok = sum(int(r.get("output_tokens") or 0) for r in f_models)
        latencies = [int(r["avg_latency_ms"]) for r in f_models if r.get("avg_latency_ms") is not None]
        p95s = [int(r["p95_latency_ms"]) for r in f_models if r.get("p95_latency_ms") is not None]

        issue = feature_issues.get(fname)
        p_core = entry["primary_core"]
        status = core_status_map.get(p_core, "READY")
        if issue:
            status = "OVERFLOW" if issue.get("reason") in {"QUOTA", "BUSY"} else "OUT"
        elif status == "READY" and calls > 0:
            status = "OK"

        last = usage["feature_last"].get(fname) or {}
        features.append(
            {
                "feature": fname,
                "label": fname.replace("_", " ").title(),
                "purpose": entry["purpose"],
                "primary_core": p_core,
                "primary_core_label": _CORE_LABELS.get(p_core, p_core),
                "cores": [{"id": cid, "label": _CORE_LABELS.get(cid, cid)} for cid in entry["cores"]],
                "routing_tier": _routing_tier(fname, entry["routed"]),
                "status": status,
                "healthy": status in {"OK", "READY"},
                "issue_reason": issue.get("reason") if issue else None,
                "issue_message": issue.get("message") if issue else None,
                "issue_since": issue.get("since") if issue else None,
                "calls": calls,
                "errors": errors,
                "blocked": blocked,
                "cache_hits": cache_hits,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
                "p95_latency_ms": max(p95s) if p95s else None,
                "models_used": sorted({r["model"] for r in f_models if r.get("model")}),
                "last_call_at": last.get("last_call_at").isoformat() if last.get("last_call_at") else None,
                "last_status": last.get("last_status"),
                "last_error_type": last.get("last_error_type"),
                "usage_24h": quota_by_feature.get(fname),
            }
        )

    total_calls = sum(f["calls"] for f in features)
    total_errors = sum(f["errors"] for f in features)
    total_blocked = sum(f["blocked"] for f in features)
    total_cache = sum(f["cache_hits"] for f in features)
    total_in = sum(f["input_tokens"] for f in features)
    total_out = sum(f["output_tokens"] for f in features)

    return {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "overall_status": "OUT" if any(c["status"] == "OUT" for c in cores) else ("OVERFLOW" if any(c["status"] == "OVERFLOW" for c in cores) else "OK"),
            "cores_total": len(cores),
            "cores_healthy": sum(1 for c in cores if c["status"] in {"OK", "READY"}),
            "cores_out": sum(1 for c in cores if c["status"] == "OUT"),
            "cores_overflow": sum(1 for c in cores if c["status"] == "OVERFLOW"),
            "features_total": len(features),
            "features_healthy": sum(1 for f in features if f["healthy"]),
            "features_out": sum(1 for f in features if f["status"] == "OUT"),
            "features_overflow": sum(1 for f in features if f["status"] == "OVERFLOW"),
            "total_calls": total_calls,
            "total_errors": total_errors,
            "total_blocked": total_blocked,
            "total_cache_hits": total_cache,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
        },
        "cores": cores,
        "features": features,
        "models": model_rows,
        "steps": step_rows,
        "guards": usage["guards"],
    }
