"""Optional, narrow LLM verifier for the generic anchor-search family's
cross-stage candidate ranking (`cross_stage_localization.pick_best_candidate`).

Scope, deliberately small (see evaluation/LLM_CANDIDATE_VERIFIER_PILOT_REPORT.md
for the measured pilot this module comes from):

* The model ONLY ever chooses among candidates the deterministic pipeline has
  already found (anchor match + parsed value + page/document provenance). It
  never generates, edits or normalizes a value -- its whole output is a
  1-based index into the list it was shown, or `null` ("none of these").
* Its output is strict JSON (`{"choice": <int|null>, "reason": "<short>"}`),
  validated by `parse_verdict`; anything else (free text, an out-of-range
  index, an invented value) is discarded as "no signal", never guessed at.
* The PD-stage value already resolved for a parameter is NEVER shown to the
  model when it verifies RD/ID candidates: showing it would bias the model
  toward agreement and hide exactly the PD/RD discrepancies this whole system
  exists to surface.
* A verdict is inspector-facing evidence (`reason` is written for the
  evidence card), never a decision: findings stay CANDIDATE until an
  inspector rules on them, exactly as before.

Everything heavy (torch/transformers/bitsandbytes) is imported lazily inside
`_load()`, and this module imports nothing from the rest of `app` at module
level, so (a) the app runs unchanged without those packages and (b) the
prompt/parse code can be evaluated standalone in an isolated GPU venv
(see the pilot report). Disabled by default (`CASE10_LLM_VERIFIER_ENABLED`),
same pattern as `CASE10_SEMANTIC_ANCHOR_FALLBACK_ENABLED` (semantic_similarity.py).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import threading
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_TRUE = ("1", "true", "yes", "on")

LLM_VERIFIER_MODEL_NAME = os.getenv("CASE10_LLM_VERIFIER_MODEL", "Qwen/Qwen3-4B-Instruct-2507")
# "auto" sizes against the memory this PROCESS is allowed to use, not the
# card's total: gpu.py caps the process at CASE10_GPU_MEMORY_FRACTION of the
# device (shared grading GPU). bf16 (~8 GB of weights for a 4B model) when
# the allowed share is >= BF16_MIN_ALLOWED_GB (the H100's default 0.2 * 80 GB
# = 16 GB), 4-bit NF4 (~2.7 GB) when >= NF4_MIN_ALLOWED_GB, otherwise the
# verifier refuses to load. Real finding from the pilot's end-to-end run: on
# a 6 GB laptop the default 0.2 share is 1.2 GB -- the model OOM'd at load and
# the verifier (correctly) degraded to a no-op. "4bit"/"bf16" force a choice.
LLM_VERIFIER_QUANTIZE = os.getenv("CASE10_LLM_VERIFIER_QUANTIZE", "auto").strip().lower()
BF16_MIN_ALLOWED_GB = 12.0
NF4_MIN_ALLOWED_GB = 3.5


def choose_precision(total_gb: float, memory_fraction: float, requested: str = "auto") -> str | None:
    """"bf16", "4bit", or None (does not fit -- do not try to load)."""
    if requested in ("bf16", "4bit"):
        return requested
    allowed = total_gb * memory_fraction
    if allowed >= BF16_MIN_ALLOWED_GB:
        return "bf16"
    if allowed >= NF4_MIN_ALLOWED_GB:
        return "4bit"
    return None
LLM_VERIFIER_MAX_NEW_TOKENS = int(os.getenv("CASE10_LLM_VERIFIER_MAX_NEW_TOKENS", "160"))
# How many visual rows above/below the value's own row go into a candidate's
# context -- enough to include a wrapped label or a table's section heading,
# small enough to keep a 10-candidate prompt well under 4k tokens.
CONTEXT_ROWS_BEFORE = 2
CONTEXT_ROWS_AFTER = 1
CONTEXT_MAX_CHARS = 700
# Hard cap on candidates per prompt (resolve_stage_round renders at most
# PAGES_PER_STAGE=15 pages per entry; more than this is truncated in the
# deterministic ranking's own order, which is what the model sees first).
MAX_CANDIDATES = 10


def is_enabled() -> bool:
    return os.getenv("CASE10_LLM_VERIFIER_ENABLED", "0").strip().lower() in _TRUE


def mode() -> str:
    """"shadow" (default): the verdict is recorded next to the deterministic
    pick for the inspector, the pick itself is unchanged. "rerank": the
    verdict's choice replaces the deterministic pick when the gate in
    `should_verify` fires (see the pilot report for why this is not the
    default)."""
    value = os.getenv("CASE10_LLM_VERIFIER_MODE", "shadow").strip().lower()
    return value if value in ("shadow", "rerank") else "shadow"


# ---------------------------------------------------------------------------
# Candidate context (computed from the same rendered page snapshot the
# deterministic match_fn already used -- no extra page rendering).
# ---------------------------------------------------------------------------

def _row_y_center(row: list[dict[str, Any]]) -> float:
    return sum((w["bbox"][1] + w["bbox"][3]) / 2 for w in row) / len(row)


def build_candidate_context(
    words: list[dict[str, Any]],
    value_bbox: Iterable[float] | None,
    *,
    rows_before: int = CONTEXT_ROWS_BEFORE,
    rows_after: int = CONTEXT_ROWS_AFTER,
    max_chars: int = CONTEXT_MAX_CHARS,
) -> tuple[str | None, str | None]:
    """Returns (row_text, context_text) for the visual row holding the found
    value (`value_bbox`, PDF coordinates, as every generic-tier payload
    carries in `bbox_pdf`): the row itself, and that row plus a few rows
    above/below it in visual (not content-stream) order. Visual order matters
    on real documents whose PDF reading order emits a value before its own
    label (multi-column reflow) -- the row cluster keeps label and value
    together either way."""
    from .anchor_search import cluster_rows  # local import: keep module standalone-loadable

    if not words or not value_bbox:
        return None, None
    bbox = list(value_bbox)
    if len(bbox) != 4:
        return None, None
    y_center = (bbox[1] + bbox[3]) / 2
    rows = cluster_rows([w for w in words if w.get("bbox")])
    if not rows:
        return None, None
    index = min(range(len(rows)), key=lambda i: abs(_row_y_center(rows[i]) - y_center))
    row_text = " ".join(str(w.get("text") or "") for w in rows[index])
    window = rows[max(0, index - rows_before): index + rows_after + 1]
    context = "\n".join(" ".join(str(w.get("text") or "") for w in row) for row in window)
    if len(context) > max_chars:
        context = context[:max_chars] + " …"
    return row_text, context


# ---------------------------------------------------------------------------
# Prompt / verdict
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Ты — узкий верификатор кандидатов для экспертизы строительной документации. "
    "Тебе дают точное определение одного параметра из матрицы проверки и пронумерованный "
    "список кандидатов, уже найденных детерминированным поиском в документах (значение, "
    "строка таблицы/текста, контекст, документ и страница). Твоя единственная задача — "
    "указать, какой кандидат является значением ИМЕННО этого параметра, или ответить, что "
    "ни один не подходит.\n"
    "Правила:\n"
    "1. Выбирай только из предъявленного списка. Никогда не придумывай и не исправляй значения.\n"
    "2. Подпись строки должна обозначать ту же величину, что и параметр, а не смежную "
    "(другой объект измерения, часть вместо целого, другая характеристика, другой норматив).\n"
    "3. Найденное значение должно относиться к этой подписи, а не к соседней строке, "
    "пороговому значению, номеру пункта, году или номеру норматива.\n"
    "4. Единица измерения должна быть совместима с единицей параметра.\n"
    "5. Пустые/нулевые значения незаполненных шаблонов не являются измерением.\n"
    "6. Если сомневаешься между кандидатами или ни один не подходит — choice = null.\n"
    "Ответ — только JSON без пояснений вокруг: "
    "{\"choice\": <номер кандидата или null>, \"reason\": \"<кратко, до 200 символов, по-русски>\"}"
)


def parameter_definition(param: Any, stage: str) -> dict[str, str]:
    """The parameter's exact catalog definition as plain strings (works on a
    `Param` ORM row or any object/dict with the same attribute names)."""
    def get(name: str) -> str:
        value = param.get(name) if isinstance(param, dict) else getattr(param, name, None)
        return str(value or "").strip()

    hints: dict[str, str] = {}
    raw = get("other_normative")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                hints = {k: str(parsed.get(k) or "") for k in ("source_pd", "source_rd", "source_id")}
        except (TypeError, ValueError):
            hints = {}
    for key in ("source_pd", "source_rd", "source_id"):
        if not hints.get(key) and isinstance(param, dict) and isinstance(param.get(key), str):
            hints[key] = param[key]
    return {
        "code": get("code"),
        "parameter_name": get("parameter_name"),
        "unit": get("unit"),
        "stage": stage,
        "source_hint": hints.get({"PD": "source_pd", "RD": "source_rd", "ID": "source_id"}.get(stage, ""), ""),
        "trigger_logic": get("trigger_logic"),
        "normative": "; ".join(v for v in (get("sp_reference"), get("gost_reference"), get("fz_reference")) if v),
    }


_STAGE_RU = {"PD": "проектная документация (ПД)", "RD": "рабочая документация (РД)", "ID": "исполнительная документация (ИД)"}


def build_messages(definition: dict[str, str], candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """`candidates`: dicts with `value`, `row_text`, `context`, `document`,
    `page` (already in the deterministic ranking's order)."""
    lines = [
        "ПАРАМЕТР",
        f"Код: {definition.get('code')}",
        f"Наименование: {definition.get('parameter_name')}",
        f"Единица: {definition.get('unit') or '—'}",
        f"Стадия поиска: {_STAGE_RU.get(definition.get('stage', ''), definition.get('stage'))}",
    ]
    if definition.get("source_hint"):
        lines.append(f"Где должен находиться (по каталогу): {definition['source_hint']}")
    if definition.get("trigger_logic"):
        lines.append(f"Логика проверки: {definition['trigger_logic']}")
    if definition.get("normative"):
        lines.append(f"Нормативная ссылка: {definition['normative']}")
    lines.append("")
    lines.append(f"КАНДИДАТЫ ({len(candidates)})")
    for number, candidate in enumerate(candidates, start=1):
        lines.append(f"[{number}] Документ: {candidate.get('document')}, стр. {candidate.get('page')}")
        lines.append(f"    Найденное значение: {candidate.get('value')}")
        if candidate.get("row_text"):
            lines.append(f"    Строка: {candidate['row_text']}")
        if candidate.get("context"):
            context = str(candidate["context"]).replace("\n", " ⏎ ")
            lines.append(f"    Контекст: {context}")
    lines.append("")
    lines.append("Какой кандидат — значение именно этого параметра? Ответь JSON.")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


@dataclass(slots=True)
class Verdict:
    # 0-based index into the candidate list shown, or None for "none of them".
    choice: int | None
    reason: str
    raw: str
    latency_seconds: float
    model: str


_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(text: str, n_candidates: int) -> tuple[int | None, str] | None:
    """Strictly validates the model's reply. Returns (0-based choice or None,
    reason) or None when the reply is unusable (no JSON object, missing
    `choice`, non-integer choice, index outside 1..n) -- an unusable reply is
    "no signal", never coerced into a pick."""
    if not text:
        return None
    for match in _JSON_OBJECT_RE.finditer(text):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict) or "choice" not in obj:
            continue
        choice = obj.get("choice")
        reason = str(obj.get("reason") or "").strip()[:300]
        if choice is None or (isinstance(choice, str) and choice.strip().lower() in ("", "null", "none")):
            return None, reason
        if isinstance(choice, bool):
            return None
        if isinstance(choice, str) and choice.strip().isdigit():
            choice = int(choice.strip())
        if not isinstance(choice, int) or not (1 <= choice <= n_candidates):
            return None
        return choice - 1, reason
    return None


# ---------------------------------------------------------------------------
# Model backend (lazy; transformers)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_model: Any = None
_tokenizer: Any = None
_load_failed = False


def _load() -> bool:
    global _model, _tokenizer, _load_failed
    if _model is not None:
        return True
    if _load_failed:
        return False
    with _lock:
        if _model is not None:
            return True
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            kwargs: dict[str, Any] = {}
            if torch.cuda.is_available():
                total_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
                fraction = float(os.getenv("CASE10_GPU_MEMORY_FRACTION", "0.2"))  # same knob/default as gpu.py
                precision = choose_precision(total_gb, fraction, LLM_VERIFIER_QUANTIZE)
                if precision is None:
                    raise RuntimeError(
                        f"allowed GPU share {total_gb * fraction:.1f} GB (CASE10_GPU_MEMORY_FRACTION={fraction}) "
                        f"< {NF4_MIN_ALLOWED_GB} GB needed even for 4-bit"
                    )
                if precision == "4bit":
                    from transformers import BitsAndBytesConfig

                    kwargs["quantization_config"] = BitsAndBytesConfig(
                        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                    )
                else:
                    kwargs["dtype"] = torch.bfloat16
                kwargs["device_map"] = "cuda:0"
            else:
                # CPU inference of a 4B model is far outside any useful
                # per-document budget -- refuse rather than silently crawl.
                raise RuntimeError("no CUDA device visible; LLM verifier requires a GPU")
            tokenizer = AutoTokenizer.from_pretrained(LLM_VERIFIER_MODEL_NAME)
            model = AutoModelForCausalLM.from_pretrained(LLM_VERIFIER_MODEL_NAME, **kwargs)
            model.eval()
            _tokenizer, _model = tokenizer, model
            logger.info("LLM verifier loaded: %s (%s)", LLM_VERIFIER_MODEL_NAME, "4bit" if "quantization_config" in kwargs else "bf16")
            return True
        except Exception as exc:  # noqa: BLE001 -- optional layer, never fatal
            _load_failed = True
            logger.warning("LLM verifier unavailable (%s: %s); deterministic ranking unchanged", type(exc).__name__, exc)
            return False


def generate(messages: list[dict[str, str]]) -> str:
    import torch

    text = _tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = _tokenizer(text, return_tensors="pt").to(_model.device)
    with torch.inference_mode():
        output = _model.generate(
            **inputs, max_new_tokens=LLM_VERIFIER_MAX_NEW_TOKENS, do_sample=False,
            temperature=None, top_p=None, top_k=None, pad_token_id=_tokenizer.eos_token_id,
        )
    return _tokenizer.decode(output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def _param_from_extra(extra: Any) -> Any:
    """The generic-tier mechanisms pass their `Param` to `resolve_stage_round`
    in different shapes (`param`, `(param, family)`, `(param, components)`,
    or not at all for table-row-count) -- recover it when present."""
    if hasattr(extra, "parameter_name"):
        return extra
    if isinstance(extra, tuple) and extra and hasattr(extra[0], "parameter_name"):
        return extra[0]
    return None


def _display_value(payload: Any) -> str | None:
    for attr in ("display_value", "value", "normalized_value"):
        value = getattr(payload, attr, None)
        if value is not None:
            return str(value)
    return None


def review_pick(candidates: list[Any], best: Any, extra: Any) -> tuple[Any, bool]:
    """Runs the verifier over one (parameter, stage) candidate pool AFTER the
    deterministic `pick_best_candidate` already chose `best`, and returns
    (pick, verified). `candidates` are `cross_stage_localization.
    StageCandidate`s in the same order `pick_best_candidate` saw them.

    Policy (see the pilot report for the measurements behind each rule):
    * Never called when the deterministic ranking abstained (`best is None`):
      on the pilot set, 5 of the verifier's 6 regressions were exactly that --
      the semantic floor had correctly dropped junk (an engineer's initials,
      a clause number) and the model then "accepted" it.
    * An unusable/unavailable verdict is a no-op (the deterministic pick
      stands, nothing is recorded).
    * shadow (default): the pick is unchanged; the verdict is attached to the
      picked payload's `llm_verification` for the evidence card.
    * rerank: the model's choice replaces the pick; "none of these" makes the
      stage abstain (returns (None, True)).
    """
    param = _param_from_extra(extra)
    if best is None or not candidates or param is None:
        return best, False
    stage = getattr(best.document, "dataset_stage", None) or ""
    shown = [
        {
            "document": getattr(c.document, "filename", None) or getattr(c.document, "document_code", None),
            "page": c.page,
            "value": _display_value(c.payload),
            "row_text": getattr(c, "verifier_row_text", None),
            "context": getattr(c, "verifier_context", None),
        }
        for c in candidates
    ]
    verdict = verify(parameter_definition(param, stage), shown)
    if verdict is None:
        return best, False
    chosen = candidates[verdict.choice] if verdict.choice is not None else None
    record = {
        "model": verdict.model,
        "mode": mode(),
        "candidates_shown": min(len(candidates), MAX_CANDIDATES),
        "choice": None if chosen is None else {
            "document": shown[verdict.choice]["document"], "page": chosen.page, "value": shown[verdict.choice]["value"],
        },
        "agrees_with_deterministic_pick": chosen is best,
        "reason": verdict.reason,
        "latency_seconds": round(verdict.latency_seconds, 2),
    }
    if not record["agrees_with_deterministic_pick"]:
        logger.info("LLM verifier disagrees for %s/%s: %s", getattr(param, "code", "?"), stage, record)
    if mode() == "rerank":
        if chosen is None:
            return None, True
        _attach(chosen.payload, record)
        return chosen, True
    _attach(best.payload, record)
    return best, True


def _attach(payload: Any, record: dict[str, Any]) -> None:
    try:
        payload.llm_verification = record
    except AttributeError:  # a payload type without the optional field
        pass


def verify(definition: dict[str, str], candidates: list[dict[str, Any]]) -> Verdict | None:
    """Runs one verification call. Returns None when the verifier is
    unavailable or the reply is unusable (see `parse_verdict`)."""
    if not candidates or not _load():
        return None
    shown = candidates[:MAX_CANDIDATES]
    started = time.perf_counter()
    try:
        raw = generate(build_messages(definition, shown))
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM verifier call failed (%s: %s)", type(exc).__name__, exc)
        return None
    latency = time.perf_counter() - started
    parsed = parse_verdict(raw, len(shown))
    if parsed is None:
        logger.info("LLM verifier reply unusable for %s: %r", definition.get("code"), raw[:200])
        return None
    choice, reason = parsed
    return Verdict(choice=choice, reason=reason, raw=raw, latency_seconds=latency, model=LLM_VERIFIER_MODEL_NAME)
