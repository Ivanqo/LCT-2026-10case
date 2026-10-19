"""Size of the small process pools that scan documents outside the live tagger (explication tables, free hypotheses).

Both used to read `CASE10_LIVE_TAGGER_WORKERS` literally, so the delivered default `0` ("auto = the CPUs this container may
use", the tagger's own meaning) became ONE worker -- the scans ran serially in the parent -- and any larger value was cut
to 3. The scans are pure functions of the document bytes and their results are merged in input order, so the pool size
changes how long they take, never what they return.
"""
from __future__ import annotations

import os

_DEFAULT_WITHOUT_ANY_SETTING = 3      # what both pools used when nothing at all was configured
_UPPER_LIMIT = 12                     # each worker holds one whole PDF in memory


def worker_count(pending: int, *specific_env: str) -> int:
    """Workers for `pending` documents. `specific_env` are tried in order, then `CASE10_LIVE_TAGGER_WORKERS`; a
    positive value is used as given (up to the limit), 0 or negative = the tagger's automatic size."""
    requested = None
    for name in (*specific_env, "CASE10_LIVE_TAGGER_WORKERS"):
        value = os.getenv(name)
        if value is not None and value.strip():
            requested = value.strip()
            break
    if requested is None:
        count = _DEFAULT_WITHOUT_ANY_SETTING
    else:
        try:
            count = int(requested)
        except ValueError:
            count = _DEFAULT_WITHOUT_ANY_SETTING
        if count <= 0:
            from ..config import settings
            from .live_tagger_scan import available_cpu_count

            count = min(available_cpu_count(), max(1, int(settings.LIVE_TAGGER_MAX_WORKERS)))
    return max(1, min(count, _UPPER_LIMIT, int(pending)))
