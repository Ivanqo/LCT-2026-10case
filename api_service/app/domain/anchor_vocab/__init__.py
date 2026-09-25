"""Anchor vocabulary: extra wordings under which a matrix parameter appears in real documents.

The catalog name (`Param.parameter_name`) stays the FIRST anchor of every parameter; a vocabulary file only adds
wordings the catalog does not use (bench A: 53% of extraction misses were "document wording != catalog name", e.g.
DOO25 «Площадь озеленения» vs «Площадь озеленения и газонов»). One file per internal code, `<code>.json`:

    {
      "code": "SPZU-027",                       internal (legacy) code == file name
      "matrix_code": "M-027",                   matrix 1.1 code (informational)
      "parameter_name": "...",                  catalog name (informational; never needs repeating in `phrases`)
      "phrases": [                              extra wordings, in priority order
        {"text": "Площадь озеленения",
         "stages": ["PD", "RD"],                optional: stages the wording is used at (default: all)
         "provenance": [{"object": "LOS3A", "file_id": "LOS3A-000003", "page": 14}]}
      ],
      "units": [{"text": "м2", "provenance": [...]}],   unit spellings seen next to the value
      "notes": ""
    }

Rules (Phase 12 data rules): a wording must come from a DEV object and carry its provenance (object, file, page);
no file names, page numbers or object constants in the wordings themselves; values from gold are never vocabulary.
`validate_entry` enforces the shape and the provenance; the test suite runs it over every file.

Consumers: the live candidate tagger (`live_candidate_tagger._build_param_anchors`) searches every phrase as an
extra anchor of its code; the generic mechanisms call `anchor_phrases(code, parameter_name, stage)`. The tagger's
scan cache is keyed by the exact anchor list, so adding a phrase invalidates exactly the affected scans, and empty
files change nothing (byte-identical output).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

VOCAB_DIR = Path(__file__).resolve().parent
STAGES = ("PD", "RD", "ID")


@dataclass(frozen=True, slots=True)
class VocabPhrase:
    text: str
    stages: tuple[str, ...]          # empty == every stage
    provenance: tuple[dict[str, Any], ...]

    def applies_to(self, stage: str | None) -> bool:
        return not self.stages or stage is None or str(stage).upper() in self.stages


@dataclass(frozen=True, slots=True)
class VocabEntry:
    code: str
    matrix_code: str | None
    phrases: tuple[VocabPhrase, ...]
    units: tuple[str, ...]


def vocab_path(code: str) -> Path:
    return VOCAB_DIR / f"{str(code).strip()}.json"


@lru_cache(maxsize=None)
def load_vocab(code: str) -> VocabEntry:
    """The vocabulary of one internal code; a missing file is an empty vocabulary."""
    code = str(code or "").strip()
    path = vocab_path(code) if code else None
    if path is None or not path.is_file():
        return VocabEntry(code=code, matrix_code=None, phrases=(), units=())
    raw = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_entry(raw, expected_code=code)
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    phrases = tuple(
        VocabPhrase(
            text=" ".join(str(item["text"]).split()),
            stages=tuple(str(stage).upper() for stage in item.get("stages") or ()),
            provenance=tuple(item.get("provenance") or ()),
        )
        for item in raw.get("phrases") or ()
    )
    units = tuple(" ".join(str(item["text"]).split()) for item in raw.get("units") or ())
    return VocabEntry(code=code, matrix_code=raw.get("matrix_code"), phrases=phrases, units=units)


def extra_phrases(code: str, stage: str | None = None) -> tuple[str, ...]:
    """Vocabulary wordings of `code` applicable at `stage` (catalog name NOT included), in file order, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for phrase in load_vocab(code).phrases:
        key = phrase.text.casefold()
        if phrase.applies_to(stage) and phrase.text and key not in seen:
            seen.add(key)
            out.append(phrase.text)
    return tuple(out)


def anchor_phrases(code: str, parameter_name: str | None, stage: str | None = None) -> tuple[str, ...]:
    """Every anchor of a parameter: the catalog name first, then the vocabulary wordings (duplicates of the name
    dropped). With an empty vocabulary this is exactly `(parameter_name.strip(),)` -- the name is NOT re-spaced, so
    the tagger's anchor list (part of its scan-cache key) is unchanged by this hook."""
    name = str(parameter_name or "").strip()
    out = [name] if name else []
    folded = " ".join(name.split()).casefold()
    out.extend(text for text in extra_phrases(code, stage) if text.casefold() != folded)
    return tuple(out)


def unit_spellings(code: str) -> tuple[str, ...]:
    return load_vocab(code).units


def validate_entry(raw: Any, *, expected_code: str | None = None) -> list[str]:
    """Shape + provenance problems of one vocabulary file (empty list == valid)."""
    if not isinstance(raw, dict):
        return ["top level must be an object"]
    problems: list[str] = []
    if expected_code is not None and raw.get("code") != expected_code:
        problems.append(f"code {raw.get('code')!r} != file name {expected_code!r}")
    for field in ("phrases", "units"):
        if not isinstance(raw.get(field, []), list):
            problems.append(f"{field} must be a list")
    for kind in ("phrases", "units"):
        for index, item in enumerate(raw.get(kind) or [] if isinstance(raw.get(kind), list) else []):
            where = f"{kind}[{index}]"
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                problems.append(f"{where}: needs a non-empty 'text'")
                continue
            stages = item.get("stages") or []
            if not isinstance(stages, list) or any(str(stage).upper() not in STAGES for stage in stages):
                problems.append(f"{where}: stages must be a subset of {list(STAGES)}")
            provenance = item.get("provenance")
            if not isinstance(provenance, list) or not provenance:
                problems.append(f"{where}: needs provenance [{{object, file_id, page}}]")
                continue
            for source in provenance:
                if not isinstance(source, dict) or not all(source.get(key) not in (None, "") for key in ("object", "file_id", "page")):
                    problems.append(f"{where}: every provenance item needs object, file_id and page")
                    break
    return problems


def clear_cache() -> None:
    load_vocab.cache_clear()
