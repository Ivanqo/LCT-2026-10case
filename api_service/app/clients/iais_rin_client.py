"""ТЗ 9.6 "Модуль интеграции по API с внешней ИС (целевая ИАИС РИН)": REST
client for the outbound leg -- POST {base_url}/api/v1/inspection/{process_id}
once a protocol is PROTOCOL_FINALIZED. `base_url` is configurable
(`IAIS_RIN_BASE_URL`, or passed directly) so tests can point this at a local
mock HTTP server instead of a real deployment (see
tests/test_case10_iais_rin_sync.py).

УКЭП (ТЗ 12.10): every request to ИАИС РИН must be signed with a qualified
electronic signature. No certified УКЭП signing provider is available in
this codebase. `IAIS_RIN_CLIENT_CERT_PATH`/`IAIS_RIN_CLIENT_KEY_PATH`
configure mutual-TLS client-certificate authentication instead (the closest
transport-level equivalent httpx offers) -- a documented limitation, not a
real УКЭП implementation.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings


class IaisRinError(Exception):
    """Base error for a failed ИАИС РИН call."""


class IaisRinRetryableError(IaisRinError):
    """5xx response, timeout, or connection failure -- retryable per ТЗ 9.6
    ("При ошибках 5xx или таймаутах -- до 3 повторных попыток...")."""


class IaisRinRejectedError(IaisRinError):
    """4xx response -- the request itself was rejected (bad process_id,
    malformed payload, auth failure); retrying the same request would not
    help, so this is not retried."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"ИАИС РИН rejected inspection sync: HTTP {status_code} {detail}")
        self.status_code = status_code
        self.detail = detail


class IaisRinClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url if base_url is not None else settings.IAIS_RIN_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.IAIS_RIN_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        """False when no target system is configured at all -- distinct from
        a configured-but-unreachable target, which is a retryable failure."""
        return bool(self.base_url)

    def _cert(self) -> tuple[str, str] | str | None:
        cert_path = settings.IAIS_RIN_CLIENT_CERT_PATH
        key_path = settings.IAIS_RIN_CLIENT_KEY_PATH
        if cert_path and key_path:
            return (cert_path, key_path)
        if cert_path:
            return cert_path
        return None

    async def send_inspection_result(self, *, process_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise IaisRinError("IAIS_RIN_BASE_URL is not configured")
        url = f"{self.base_url}/api/v1/inspection/{process_id}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, cert=self._cert()) as client:
                response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            # Covers both "таймаут" and any connection-level failure -- ТЗ 9.6
            # groups them with 5xx under the same retry policy.
            raise IaisRinRetryableError(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code >= 500:
            raise IaisRinRetryableError(f"HTTP {response.status_code}: {response.text[:500]}")
        if response.status_code >= 400:
            raise IaisRinRejectedError(response.status_code, response.text[:500])
        try:
            return response.json()
        except ValueError:
            return {"status_code": response.status_code, "raw": response.text}
