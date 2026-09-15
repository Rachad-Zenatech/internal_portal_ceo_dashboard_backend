"""
Enterprise Application Security & Scope Authorization Policy
Defines the strict Application-to-Application Access Matrix (Machine-to-Machine).
Controls what data each application (CEO Dashboard, Finance, M&A, HR, Purchasing)
can request, send, and synchronize.
"""

import logging
from typing import Dict, List, Set, Optional, Any
from fastapi import HTTPException, Security, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("SESSION_SECRET") or os.environ.get("JWT_SECRET") or "OU2YW8HGoJJMb7+aAVjoxRXah2gSUtvPLPlzK8G6j9c="
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "zenatech-internal-portal"

security_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# APPLICATION ACCESS CONTROL MATRIX
# ---------------------------------------------------------------------------

TRUSTED_APPLICATIONS = {
    "admin": {
        "description": "Zenatech Administration Portal",
        "allowed_inbound_scopes": {
            "*",  # Root admin access
        },
    },
    "ceo-dashboard": {
        "description": "CEO Executive Visibility & Command Portal",
        "allowed_inbound_scopes": {
            "metrics:read",
            "approvals:read",
            "approvals:write",
            "service_status:read",
            "purchasing:summary_read",
            "audit:read",
            "audit:write",
        },
    },
    "finance": {
        "description": "Finance & Enterprise System",
        "allowed_inbound_scopes": {
            "purchasing:read",
            "purchasing:status_update",
            "coa:read",
            "coa:sync",
            "company:read",
            "company:write",
            "business_contacts:read",
        },
    },
}

# Explicitly Forbidden Operations for non-admin M2M services
RESTRICTED_ADMIN_SCOPES = {
    "rbac:delete_role",
    "rbac:assign_super_admin",
    "database:raw_query",
    "users:delete",
    "tokens:impersonate",
}


# ---------------------------------------------------------------------------
# Inbound Security Guard Dependency
# ---------------------------------------------------------------------------

def require_application_scope(required_scope: str, allowed_services: Optional[List[str]] = None):
    """
    FastAPI dependency that enforces Application-Level Scope authorization.
    Verifies that the caller is an authenticated Service and is permitted to request `required_scope`.
    """
    async def _scope_guard(
        credentials: Optional[HTTPAuthorizationCredentials] = Security(security_bearer)
    ) -> Dict[str, Any]:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = credentials.credentials
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": True},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Service token has expired",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token signature: {exc}",
            )

        # 1. Identify if this is a Machine-to-Machine Service Token or a User Token
        is_service_token = payload.get("is_service_token", False)
        caller_service = payload.get("service") or payload.get("service_name") or ("admin" if payload.get("is_super_admin") else "unknown")
        token_scopes: Set[str] = set(payload.get("scopes", []))

        # 2. Check if the calling application is trusted
        if caller_service not in TRUSTED_APPLICATIONS:
            logger.warning(
                f"[Security Policy] Access denied: Untrusted calling application '{caller_service}'",
                extra={"caller_service": caller_service, "required_scope": required_scope},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Application '{caller_service}' is not registered as a trusted service",
            )

        # 3. If restricted to specific services, verify caller is in allowed list
        if allowed_services and caller_service not in allowed_services and caller_service != "admin":
            logger.warning(
                f"[Security Policy] Application '{caller_service}' blocked from accessing route restricted to {allowed_services}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Application '{caller_service}' is not authorized to access this resource",
            )

        # 4. Check against Global Restricted Admin Scopes
        if required_scope in RESTRICTED_ADMIN_SCOPES and caller_service != "admin":
            logger.warning(
                f"[Security Policy] Breach prevention: Application '{caller_service}' attempted restricted admin action '{required_scope}'"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Action restricted strictly to Admin Core Services",
            )

        # 5. Check application permissions matrix
        app_permissions = TRUSTED_APPLICATIONS[caller_service]["allowed_inbound_scopes"]
        if "*" not in app_permissions and required_scope not in app_permissions:
            logger.warning(
                f"[Security Policy] Scope authorization failed: Application '{caller_service}' lacks scope '{required_scope}'",
                extra={"caller_service": caller_service, "required_scope": required_scope, "app_scopes": list(app_permissions)},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Application '{caller_service}' is not granted scope '{required_scope}'",
            )

        # Authorized successfully
        return {
            "caller_service": caller_service,
            "is_service_token": is_service_token,
            "granted_scope": required_scope,
            "payload": payload,
        }

    return _scope_guard


# ---------------------------------------------------------------------------
# Outbound Scoped Token Generator (Principle of Least Privilege)
# ---------------------------------------------------------------------------

def generate_scoped_outbound_token(
    target_service: str,
    requested_scopes: List[str],
    source_service: str = "admin",
    expires_in_minutes: int = 60,
) -> str:
    """
    Generates a minimal, scope-restricted token for sending requests to another application.
    """
    import time
    from datetime import datetime, timezone, timedelta
    import secrets

    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_in_minutes)

    claims = {
        "sub": f"service:{source_service}",
        "service": source_service,
        "target_service": target_service,
        "is_service_token": True,
        "scopes": requested_scopes,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "jti": secrets.token_urlsafe(16),
        "exp": int(exp.timestamp()),
    }

    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)
