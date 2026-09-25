from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_client_ip: ContextVar[str | None] = ContextVar("_client_ip", default=None)
_request_id: ContextVar[str | None] = ContextVar("_request_id", default=None)
_user_id: ContextVar[int | None] = ContextVar("_user_id", default=None)


def set_client_ip(value: str | None) -> None:
    _client_ip.set(value)


def get_client_ip() -> str | None:
    return _client_ip.get()


def resolve_client_ip(request: Request) -> str | None:
    """Nginx sets X-Forwarded-For in prod; use its first hop, falling back to
    the direct peer address for local/dev requests without a proxy in front."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else None


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_user_id(value: int | None) -> None:
    _user_id.set(value)


def get_user_id() -> int | None:
    return _user_id.get()


_REQUEST_ID_HEADER_NAME = b"x-request-id"


class RequestIDMiddleware:
    """ТЗ 13: every HTTP request gets a request id (the incoming X-Request-ID
    header if present, otherwise a generated uuid4), stashed in a contextvar
    so the JSON log formatter (see logging_setup.py) can attach it to every
    record emitted while handling the request, and echoed back on the
    response so callers can correlate their own logs against ours."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = None
        for name, value in scope.get("headers") or []:
            if name == _REQUEST_ID_HEADER_NAME:
                request_id = value.decode("latin-1").strip()
                break
        request_id = request_id or str(uuid.uuid4())
        set_request_id(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((_REQUEST_ID_HEADER_NAME, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
