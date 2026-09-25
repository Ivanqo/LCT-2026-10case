"""Small, controlled per-domain vocabularies for the CASE10 matrix's
`ENUM_CLASS` unit family (see `matrix_unit_classifier.classify_unit`) --
categorical construction/fire/energy codes such as concrete grade ("B25"),
rebar class ("A500C"), steel grade ("C345"), fire-resistance degree ("II"),
energy-efficiency letter ("A++"), and fire-hazard class ("KM1"/"C0").

Generalizes the pattern already proven, per-code, by
`official_rule_packs._extract_concrete_classes` (the KR-055 rule pack) into a
small registry any *other* ENUM_CLASS-classified matrix parameter can use,
instead of a new hand-written regex per code. Each family below is built from
normative vocabulary (GOST/SP/postановления) -- never from the organizer's
closed gold methodology, which this module never reads or imports.

Deliberately stdlib-only (pure `re` + dataclasses), like
`matrix_unit_classifier.py`, so it stays importable from
`evaluation/matrix_coverage.py` without pulling in the API service's
SQLAlchemy/pydantic runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .matrix_unit_classifier import normalize_anchor_text


@dataclass(frozen=True, slots=True)
class EnumFamily:
    name: str
    # Substrings that must appear in a parameter's own `parameter_name` (or
    # `pd_section`, passed separately) for it to resolve to this family --
    # keyword-based, not a per-code list, so any future catalog parameter
    # with matching wording is picked up automatically.
    name_keywords: tuple[str, ...]
    # Matches ONE canonical value's raw spelling in real page text.
    # Case-insensitive; must define exactly one capturing group holding the
    # class-defining part (e.g. "25" in "B25", "500С" in "А500С").
    pattern: re.Pattern[str]
    canonicalize: "callable[[re.Match[str]], str]"


def _concrete_canonical(match: re.Match[str]) -> str:
    return f"B{match.group(1).replace(',', '.')}"


def _rebar_modern_canonical(match: re.Match[str]) -> str:
    suffix = "C" if match.group(2) else ""
    return f"A{match.group(1)}{suffix}"


def _rebar_legacy_canonical(match: re.Match[str]) -> str:
    return f"A-{match.group(1).upper()}"


def _steel_canonical(match: re.Match[str]) -> str:
    suffix = match.group(2).upper() if match.group(2) else ""
    suffix = "K" if suffix in {"K", "К"} else suffix
    return f"C{match.group(1)}{suffix}"


def _energy_letter_canonical(match: re.Match[str]) -> str:
    return match.group(1).upper().replace("PLUS", "+")


def _fire_hazard_material_canonical(match: re.Match[str]) -> str:
    return f"KM{match.group(1)}"


def _fire_hazard_building_canonical(match: re.Match[str]) -> str:
    return f"C{match.group(1)}"


def _fire_resistance_degree_canonical(match: re.Match[str]) -> str:
    return match.group(1).upper()


def _power_category_canonical(match: re.Match[str]) -> str:
    raw = match.group(1).upper()
    digit = raw[0] if raw and raw[0].isdigit() else None
    return {"1": "I", "2": "II", "3": "III"}[digit] if digit else raw


def _fire_resistance_class_canonical(match: re.Match[str]) -> str:
    return f"{match.group(1).upper()}{match.group(2)}"


# Concrete compressive-strength grade per ГОСТ 26633 -- B7.5 .. B80.
# Note: the ONE currently-eligible catalog code for this family, KR-055, is
# already a tuned, gold-tested rule pack (`official_rule_packs.py`) and is
# excluded from this generic mechanism by `SUPPORTED_RULE_CODES` before this
# registry is ever consulted -- kept here for completeness/reuse should a
# second concrete-grade parameter ever enter the catalog.
CONCRETE_GRADE = EnumFamily(
    name="CONCRETE_GRADE",
    name_keywords=("бетон",),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[BВ]\s?-?\s?(\d{1,2}(?:[.,]5)?)(?!\d)", re.IGNORECASE),
    canonicalize=_concrete_canonical,
)

# Reinforcement (rebar) strength class -- modern ГОСТ 34028 notation
# (A240..A1000, optionally weldable "С"/"C" suffix) and legacy ГОСТ 5781-82
# roman-numeral notation (A-I .. A-VI).
REBAR_CLASS_MODERN = EnumFamily(
    name="REBAR_CLASS",
    name_keywords=("арматур",),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[AА]\s?-?\s?(240|300|400|500|600|800|1000)\s?([CС])?(?!\d)", re.IGNORECASE),
    canonicalize=_rebar_modern_canonical,
)
REBAR_CLASS_LEGACY = EnumFamily(
    name="REBAR_CLASS",
    name_keywords=("арматур",),
    # Alternatives ordered longest-first so a shorter prefix (e.g. "I" inside
    # "III") never wins the match before the fuller roman numeral is tried.
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[AА][-\s]?(III|II|IV|VI|I|V)(?![A-Za-zА-Яа-я0-9])"),
    canonicalize=_rebar_legacy_canonical,
)

# Structural-steel grade per ГОСТ 27772 -- C235..C590, optional К (corrosion-
# resistant) suffix. Cyrillic "С" and Latin "C" are visually identical in
# most PDF fonts and both appear in real documents; matched interchangeably.
STEEL_GRADE = EnumFamily(
    name="STEEL_GRADE",
    name_keywords=("сталь", "металлопрокат"),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[CС]\s?-?\s?(2[3-9]\d|3[0-9]\d|4[0-4]\d|590)\s?([KК])?(?!\d)", re.IGNORECASE),
    canonicalize=_steel_canonical,
)

# Building energy-efficiency letter class per Приказ Минстроя России от
# 06.06.2016 №399/пр (residential): А++, А+, А, В+, В, С+, С, D, E.
ENERGY_EFFICIENCY_LETTER = EnumFamily(
    name="ENERGY_EFFICIENCY_LETTER",
    name_keywords=("энергетическ", "энергоэффектив"),
    pattern=re.compile(
        r"(?<![A-Za-zА-Яа-я0-9+])([AА]\+\+|[AА]\+|[AА]|[BВ]\+|[BВ]|[CС]\+|[CС]|D|E)(?![A-Za-zА-Яа-я0-9+])",
    ),
    canonicalize=lambda m: (
        m.group(1).upper()
        .replace("А", "A").replace("В", "B").replace("С", "C")
    ),
)

# Fire-hazard class of finishing/insulation materials per СП 1.13130 -- КМ0..КМ5.
FIRE_HAZARD_MATERIAL_CLASS = EnumFamily(
    name="FIRE_HAZARD_MATERIAL_CLASS",
    name_keywords=("отделочных материал", "внутренней отделки", "класс км"),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[KК][MМ]\s?-?\s?([0-5])(?!\d)", re.IGNORECASE),
    canonicalize=_fire_hazard_material_canonical,
)

# Class of structural fire hazard of the building per 123-ФЗ/СП 2.13130 -- С0..С3.
FIRE_HAZARD_BUILDING_CLASS = EnumFamily(
    name="FIRE_HAZARD_BUILDING_CLASS",
    name_keywords=("конструктивной пожарной опасности",),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])[CС]\s?-?\s?([0-3])(?!\d)", re.IGNORECASE),
    canonicalize=_fire_hazard_building_canonical,
)

# Degree of fire resistance of the building per СП 2.13130 -- roman I..V.
FIRE_RESISTANCE_DEGREE = EnumFamily(
    name="FIRE_RESISTANCE_DEGREE",
    name_keywords=("степень огнестойкости",),
    # Longest-first, same reasoning as REBAR_CLASS_LEGACY above.
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])(III|II|IV|I|V)(?![A-Za-zА-Яа-я0-9])"),
    canonicalize=_fire_resistance_degree_canonical,
)

# Power-supply reliability category per ПУЭ -- I, II, III (also seen written
# as arabic "1-я/2-я/3-я категория" in real documents).
POWER_SUPPLY_RELIABILITY_CATEGORY = EnumFamily(
    name="POWER_SUPPLY_RELIABILITY_CATEGORY",
    name_keywords=("категория надежности электроснабжения",),
    pattern=re.compile(r"(?<![A-Za-zА-Яа-я0-9])(I{1,3}|1-я|2-я|3-я|1|2|3)\s*катего", re.IGNORECASE),
    canonicalize=_power_category_canonical,
)

# Fire-resistance rating of a building element per СП 2.13130 -- e.g. REI 60,
# EI 30, R 120. Kept for normative completeness/reuse (e.g. a future
# compound-component or per-row table check); no ENUM_CLASS-classified
# catalog parameter currently resolves to this family on its own (PPM-103's
# "мин" unit routes it to the NUMERIC_COUNT/table-row-count class instead --
# see CASE10_MATRIX_132_COVERAGE.md).
FIRE_RESISTANCE_CLASS = EnumFamily(
    name="FIRE_RESISTANCE_CLASS",
    name_keywords=("предел огнестойкост",),
    pattern=re.compile(r"(?<![A-Za-zА-Я0-9])(REI|EIW|EI|EW|R)\s?-?\s?(1[5-8]0|90|60|45|30|15)(?!\d)", re.IGNORECASE),
    canonicalize=_fire_resistance_class_canonical,
)

# Tried in this order: first family whose `name_keywords` matches the
# parameter's own name/section wins. Order matters where keyword sets could
# both plausibly match the same text (none currently do, but e.g. a future
# "материал и класс" wording should not have to worry about ordering
# surprises -- keep the more specific/narrower keyword set earlier).
ALL_FAMILIES: tuple[EnumFamily, ...] = (
    FIRE_HAZARD_BUILDING_CLASS,
    FIRE_HAZARD_MATERIAL_CLASS,
    FIRE_RESISTANCE_DEGREE,
    FIRE_RESISTANCE_CLASS,
    POWER_SUPPLY_RELIABILITY_CATEGORY,
    ENERGY_EFFICIENCY_LETTER,
    REBAR_CLASS_MODERN,
    REBAR_CLASS_LEGACY,
    STEEL_GRADE,
    CONCRETE_GRADE,
)


def resolve_enum_families(*, parameter_name: str | None, pd_section: str | None = None) -> list[EnumFamily]:
    """Every family whose keyword set matches this parameter's own text,
    most-specific-first (registry order). More than one family can apply in
    principle (kept as a list, not a single winner) so a caller can try each
    in turn; in the current catalog no parameter matches more than one."""
    haystack = normalize_anchor_text(f"{parameter_name or ''} {pd_section or ''}")
    return [family for family in ALL_FAMILIES if any(keyword in haystack for keyword in family.name_keywords)]


def find_enum_value(text: str, family: EnumFamily) -> tuple[str, int, int] | None:
    """First match of `family.pattern` in `text`, returned as
    (canonical_value, start_char, end_char)."""
    match = family.pattern.search(text)
    if not match:
        return None
    return family.canonicalize(match), match.start(), match.end()
