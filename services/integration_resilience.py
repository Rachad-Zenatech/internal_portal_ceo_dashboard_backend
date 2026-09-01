"""
Integration Resilience Layer
Provides Circuit Breakers, Hard Timeout Handling, In-Memory Last-Known-Good Caching,
and Standardized State Normalization for external system dependencies (Admin Portal, M&A, etc.).
"""

import time
import asyncio
import logging
from typing import Any, Callable, Dict, Optional, TypeVar, Tuple
from datetime import datetime, timezone
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState:
    CLOSED = "CLOSED"      # Known reachable (confirmed by the last health check) - calls go through
    OPEN = "OPEN"          # Known unreachable - fast-reject, no network attempt at all
    HALF_OPEN = "HALF_OPEN"  # Unused by the health-check-driven flow; kept for external state display


StateChangeListener = Callable[[str, str, str], None]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        request_timeout_seconds: float = 5.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.request_timeout_seconds = request_timeout_seconds

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_state_change: float = time.time()
        self._listeners: list[StateChangeListener] = []

    def add_listener(self, listener: StateChangeListener) -> None:
        """Registers a callback fired as (circuit_name, old_state, new_state) on every state transition."""
        self._listeners.append(listener)

    def _transition_to(self, new_state: str) -> None:
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.time()
        if old_state == new_state:
            return
        for listener in self._listeners:
            try:
                listener(self.name, old_state, new_state)
            except Exception:
                logger.debug(f"[CircuitBreaker:{self.name}] State-change listener raised", exc_info=True)

    def record_success(self):
        """Called after a real data call succeeds. Only ever resets the failure count -
        recovery back to CLOSED is confirmed exclusively by mark_online() (the health poll),
        never inferred from a single lucky call."""
        self.failure_count = 0
        self.last_failure_time = None

    def record_failure(self, error: Exception):
        """Called after a real data call fails. Lets an outage that happens between health
        polls be detected immediately, without waiting for the next poll tick."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold and self.state != CircuitState.OPEN:
            logger.warning(
                f"[CircuitBreaker:{self.name}] Failure threshold reached ({self.failure_count} failures). "
                f"State transitioning -> OPEN. Error: {error}",
                extra={"event": "circuit_breaker_opened", "circuit": self.name, "error": str(error)},
            )
            self._transition_to(CircuitState.OPEN)

    def mark_online(self) -> None:
        """Called by the lightweight, frequent health poll when it confirms the service
        is reachable. This is the only path back to CLOSED - there is no blind timed retry."""
        if self.state != CircuitState.CLOSED:
            logger.info(
                f"[CircuitBreaker:{self.name}] Health check confirmed recovery. State transitioning "
                f"from {self.state} -> CLOSED",
                extra={"event": "circuit_breaker_closed", "circuit": self.name},
            )
            self._transition_to(CircuitState.CLOSED)
        self.failure_count = 0
        self.last_failure_time = None

    def mark_offline(self, reason: str = "health check failed") -> None:
        """Called by the lightweight, frequent health poll when it confirms the service is
        unreachable. Opens the circuit immediately so no data call even attempts the network
        until a later health check confirms recovery."""
        if self.state != CircuitState.OPEN:
            logger.warning(
                f"[CircuitBreaker:{self.name}] Health check confirmed outage ({reason}). "
                f"State transitioning -> OPEN.",
                extra={"event": "circuit_breaker_opened", "circuit": self.name, "error": reason},
            )
        self._transition_to(CircuitState.OPEN)

    def allow_request(self) -> bool:
        """Pure state check - no timers, no guessing. A call is allowed only while the last
        health check (or a prior successful call) confirmed the service is reachable."""
        return self.state != CircuitState.OPEN


class ResilientCacheEntry:
    def __init__(self, data: Any):
        self.data = data
        self.updated_at = datetime.now(timezone.utc).isoformat()
        self.timestamp = time.time()


class ResilientCache:
    """In-memory cache preserving last known good response during downstream outages."""
    def __init__(self):
        self._cache: Dict[str, ResilientCacheEntry] = {}

    def set(self, key: str, data: Any):
        if data is not None:
            self._cache[key] = ResilientCacheEntry(data)

    def get(self, key: str) -> Tuple[Optional[Any], Optional[str]]:
        entry = self._cache.get(key)
        if entry:
            return entry.data, entry.updated_at
        return None, None


# Global instances per integration - fast 1.2s timeout and single-failure trip for instant offline failover.
# Recovery is confirmed exclusively by the lightweight health poll (see poll_portal_health in
# tools/ceo_integration_router.py) calling mark_online() - there is no blind timed retry here.
admin_circuit_breaker = CircuitBreaker("AdminPortal", failure_threshold=1, request_timeout_seconds=1.2)
ma_circuit_breaker = CircuitBreaker("MASystem", failure_threshold=1, request_timeout_seconds=1.2)
resilient_cache = ResilientCache()


async def execute_resilient_call(
    circuit: CircuitBreaker,
    cache_key: str,
    fetch_fn: Callable[..., Any],
    timeout_seconds: Optional[float] = None,
    *args,
    **kwargs,
) -> Dict[str, Any]:
    """
    Executes a network call protected by a circuit breaker, hard timeout, and last-known-good cache.
    Returns a standardized dictionary:
    {
        "status": "connected" | "stale" | "disconnected" | "timeout" | "error",
        "data": Any,
        "last_updated": Optional[str],
        "error": Optional[str],
        "latency_ms": Optional[int]
    }
    """
    timeout = timeout_seconds or circuit.request_timeout_seconds
    cached_data, cached_updated_at = resilient_cache.get(cache_key)

    # 1. Event-Driven Service Status Check via Registry
    from services.service_status_registry import service_status_registry, normalize_service_name
    svc_key = normalize_service_name(circuit.name.replace("Portal", "").replace("System", ""))
    is_online = service_status_registry.is_service_online(svc_key)

    if not is_online or not circuit.allow_request():
        logger.debug(f"[Resilience] Service '{svc_key}' / Circuit {circuit.name} is not online. Fast-returning stale cached data.")
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": f"{circuit.name} is currently offline (service availability: {service_status_registry.get_service_status(svc_key)}).",
            "latency_ms": 0,
        }

    t0 = time.time()
    try:
        # 2. Hard Timeout Execution
        result = await asyncio.wait_for(fetch_fn(*args, **kwargs), timeout=timeout)
        latency_ms = round((time.time() - t0) * 1000)
        
        circuit.record_success()
        resilient_cache.set(cache_key, result)
        
        return {
            "status": "connected",
            "data": result,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "error": None,
            "latency_ms": latency_ms,
        }

    except asyncio.TimeoutError:
        latency_ms = round((time.time() - t0) * 1000)
        err_msg = f"Request to {circuit.name} timed out after {timeout}s"
        logger.warning(f"[Resilience] {err_msg}")
        circuit.record_failure(asyncio.TimeoutError(err_msg))

        return {
            "status": "stale" if cached_data is not None else "timeout",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": f"{circuit.name} request timed out.",
            "latency_ms": latency_ms,
        }

    except Exception as exc:
        latency_ms = round((time.time() - t0) * 1000)
        err_msg = f"{circuit.name} connection error: {exc}"
        logger.warning(f"[Resilience] {err_msg}")
        circuit.record_failure(exc)

        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": f"{circuit.name} is currently unavailable.",
            "latency_ms": latency_ms,
        }
