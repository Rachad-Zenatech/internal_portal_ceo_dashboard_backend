"""Validated secret lookup from environment variables with no hardcoded fallbacks."""

import os


def require_secret(*env_vars: str, min_length: int = 32) -> str:
    """Return the first configured secret meeting min_length, or raise RuntimeError."""
    for name in env_vars:
        value = os.environ.get(name, "").strip()
        if value:
            if len(value) < min_length:
                raise RuntimeError(
                    f"{name} must be configured with at least {min_length} characters"
                )
            return value
    joined = " or ".join(env_vars)
    raise RuntimeError(f"{joined} must be configured with at least {min_length} characters")
