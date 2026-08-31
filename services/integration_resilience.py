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
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Failing, fast-reject to prevent cascading load
    HALF_OPEN = "HALF_OPEN"# Probing if service has recovered


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 5.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.last_state_change: float = time.time()

    def record_success(self):
        if self.state != CircuitState.CLOSED:
            logger.info(
                f"[CircuitBreaker:{self.name}] Service recovered. State transitioning from {self.state} -> CLOSED",
                extra={"event": "circuit_breaker_closed", "circuit": self.name},
            )
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None

    def record_failure(self, error: Exception):
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.warning(
                    f"[CircuitBreaker:{self.name}] Failure threshold reached ({self.failure_count} failures). "
                    f"State transitioning -> OPEN (cooling down for {self.recovery_timeout_seconds}s). Error: {error}",
                    extra={"event": "circuit_breaker_opened", "circuit": self.name, "error": str(error)},
                )
            self.state = CircuitState.OPEN
            self.last_state_change = time.time()

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery cooldown period has passed
            if time.time() - self.last_state_change >= self.recovery_timeout_seconds:
                logger.info(
                    f"[CircuitBreaker:{self.name}] Recovery cooldown expired. State transitioning -> HALF_OPEN (probing)",
                    extra={"event": "circuit_breaker_half_open", "circuit": self.name},
                )
                self.state = CircuitState.HALF_OPEN
                self.last_state_change = time.time()
                return True
            return False
            
        if self.state == CircuitState.HALF_OPEN:
            return True
            
        return True


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


# Global instances per integration - fast 1.2s timeout and single-failure trip for instant offline failover
admin_circuit_breaker = CircuitBreaker("AdminPortal", failure_threshold=1, recovery_timeout_seconds=10.0, request_timeout_seconds=1.2)
ma_circuit_breaker = CircuitBreaker("MASystem", failure_threshold=1, recovery_timeout_seconds=30.0, request_timeout_seconds=1.2)
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

    # 1. Circuit Breaker Fast-Fail Check
    if not circuit.allow_request():
        logger.debug(f"[Resilience] Circuit {circuit.name} is OPEN. Serving stale cached data if available.")
        return {
            "status": "stale" if cached_data is not None else "disconnected",
            "data": cached_data if cached_data is not None else [],
            "last_updated": cached_updated_at,
            "error": f"{circuit.name} is temporarily offline (circuit open).",
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
