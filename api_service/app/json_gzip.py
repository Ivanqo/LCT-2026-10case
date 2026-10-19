"""Opt-in gzip for large JSON responses (`CASE10_API_GZIP=1`, off by default).

Only buffered `application/json` responses of at least `min_bytes` are compressed, and only for clients that send
`Accept-Encoding: gzip`; images, PDFs, DOCX and anything streamed pass through untouched (PNG/PDF/DOCX are already
compressed and would only burn CPU). The bytes after decompression are the bytes the handler produced.

The Node gateway must not forward `Content-Encoding` of a body its `fetch` has already decoded -- see
FINAL_PERF2_REPORT.md before turning this on behind the gateway.
"""
from __future__ import annotations

import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class LargeJsonGzipMiddleware:
    def __init__(self, app: ASGIApp, *, min_bytes: int = 100 * 1024, compresslevel: int = 4) -> None:
        self.app = app
        self.min_bytes = int(min_bytes)
        self.compresslevel = int(compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def wrapped(message: Message) -> None:
            nonlocal start, passthrough
            if passthrough or message["type"] not in ("http.response.start", "http.response.body"):
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = Headers(raw=list(message.get("headers") or []))
                length = headers.get("content-length")
                candidate = (
                    message.get("status") == 200
                    and headers.get("content-type", "").lower().startswith("application/json")
                    and "content-encoding" not in headers
                    and length is not None and length.isdigit() and int(length) >= self.min_bytes
                )
                if not candidate:
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            assert start is not None
            headers = MutableHeaders(raw=list(start.get("headers") or []))
            compressed = gzip.compress(body, compresslevel=self.compresslevel, mtime=0)
            headers["content-encoding"] = "gzip"
            headers["content-length"] = str(len(compressed))
            headers.add_vary_header("Accept-Encoding")
            await send({**start, "headers": headers.raw})
            await send({"type": "http.response.body", "body": compressed, "more_body": False})

        await self.app(scope, receive, wrapped)
