"""Per-process cache of the finished JSON body of `GET /case10/protocols/current` (the 2.6 MB protocol payload).

The body is a pure function of one `protocols` row, so the key is the identity of the latest row as the database reports
it (id, version, status, timestamps, who finalized, byte length of the payload) plus the request scope (organization,
project, process). Reading that identity needs no payload decode; a hit skips fetching and decoding the payload and
encoding the response. Any insert/update/delete of a `Protocol` row in this process empties the cache (finalize,
unfinalize, inspector decision -> new protocol version, ИАИС РиН sync state in the payload); a change made by another
process (the worker creating a version) changes the identity and therefore the key. At most `MAX_ENTRIES` bodies are
kept (least recently used out)."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from sqlalchemy import Text, cast, event, func

from ..db.models import Protocol

MAX_ENTRIES = 8

_lock = threading.Lock()
_entries: "OrderedDict[tuple, bytes]" = OrderedDict()
_stats = {"hits": 0, "misses": 0}


def protocol_identity(db, *, project_id: int, organization_id: int, process_id: str | None) -> tuple | None:
    """Identity of the latest protocol without loading its payload; None when there is no protocol."""
    query = db.query(
        Protocol.id, Protocol.version, Protocol.status, Protocol.created_at, Protocol.finalized_at,
        Protocol.finalized_by_user_id, Protocol.unfinalized_at, Protocol.unfinalized_by_user_id,
        Protocol.unfinalize_reason, func.length(cast(Protocol.payload_json, Text)),
    ).filter(Protocol.project_id == int(project_id), Protocol.organization_id == int(organization_id))
    if process_id:
        query = query.filter(Protocol.process_id == process_id)
    row = query.order_by(Protocol.created_at.desc(), Protocol.version.desc(), Protocol.id.desc()).first()
    if row is None:
        return None
    return (int(organization_id), int(project_id), process_id or "", *tuple(row))


def get(key: tuple) -> bytes | None:
    with _lock:
        body = _entries.get(key)
        if body is None:
            _stats["misses"] += 1
            return None
        _entries.move_to_end(key)
        _stats["hits"] += 1
        return body


def put(key: tuple, body: bytes) -> None:
    with _lock:
        _entries[key] = body
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)


def clear() -> None:
    with _lock:
        _entries.clear()


def stats() -> dict[str, Any]:
    with _lock:
        return {**_stats, "entries": len(_entries), "max_entries": MAX_ENTRIES}


def _invalidate(*_args: Any, **_kwargs: Any) -> None:
    clear()


for _event_name in ("after_insert", "after_update", "after_delete"):
    event.listen(Protocol, _event_name, _invalidate)
