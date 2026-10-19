"""Runtime controls for defensive batch-input handling and approval policy."""
from __future__ import annotations

import os


ROBUST_INPUTS_ENV = "CASE10_ROBUST_INPUTS"
APPROVAL_UNKNOWN_POLICY_ENV = "CASE10_APPROVAL_UNKNOWN_POLICY"


def robust_inputs_enabled() -> bool:
    """Whether defensive handling for hostile or damaged batch inputs is active (enabled after its gate)."""
    value = os.getenv(ROBUST_INPUTS_ENV, "1").strip().lower()
    return value in {"1", "true", "yes", "on"}


def approval_unknown_policy() -> str:
    """Allow only singleton, non-conflicting unverified editions as assumptions by default."""
    value = os.getenv(APPROVAL_UNKNOWN_POLICY_ENV, "soft").strip().lower()
    return value if value in {"soft", "block"} else "block"


def default_max_unpacked_bytes() -> int:
    """Use the bounded ZIP limit in robust mode and the legacy ceiling otherwise."""
    if robust_inputs_enabled():
        return 8 * 1024**3
    return 256 * 1024**3
