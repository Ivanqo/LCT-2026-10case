"""Typed, reviewable trigger policy for the 132-parameter matrix (Phase 10, prompt B, item 3).

Why this exists
    The generic anchor mechanisms (numeric / enum / compound / table-count) used to call a group
    `CANDIDATE` for ANY difference between the PD value and the RD/ID value ("equal with rounding"
    was the only test) and ignored the catalog's own `trigger` text. The catalog says, for example,
    that KR-058 fires on a *decrease* of the slab thickness, so "RD slab thicker than PD" is a hard
    negative (NO_VIOLATION), not a candidate. That is a precision leak with no recall benefit.

What this is
    One typed row per catalog code: `kind` in
        DECREASE | INCREASE | ANY_CHANGE_PCT | ABS_THRESHOLD | ENUM_DOWNGRADE | PRESENCE |
        COUNT_CHANGE | NOT_EVALUABLE
    plus the catalog's verbatim trigger text and a `needs_review` flag. The table is built ONLY from
    the catalog's trigger wording (never from any gold label). It is one review pass over the 132
    codes -- rows that are genuinely ambiguous carry `needs_review=True`.

Conservative rule (protects recall, which matters most on critical points)
    A trigger that is ambiguous, multi-clause with an opposite-direction clause, or worded as
    "any change" is classified `ANY_CHANGE_PCT(0)`: every difference the mechanism found stays a
    CANDIDATE, exactly as before. The table can therefore only *remove* candidates whose change
    direction/size provably cannot satisfy the trigger; it never adds a candidate where the
    mechanism found equal values.

`ANY_CHANGE` in the review notes is shorthand for `ANY_CHANGE_PCT` with `pct == 0`.

Rule-pack tier: not touched. The five tuned rule packs (`official_rule_packs.SUPPORTED_RULE_CODES`)
keep their own `rule_is_violation`; their rows here are informational (`applies_to_rule_pack=True`).

Stdlib-only on purpose (importable from `evaluation/` without the API service runtime).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Any, Iterable, Sequence

DECREASE = "DECREASE"
INCREASE = "INCREASE"
ANY_CHANGE_PCT = "ANY_CHANGE_PCT"
ABS_THRESHOLD = "ABS_THRESHOLD"
ENUM_DOWNGRADE = "ENUM_DOWNGRADE"
PRESENCE = "PRESENCE"
COUNT_CHANGE = "COUNT_CHANGE"
NOT_EVALUABLE = "NOT_EVALUABLE"

TRIGGER_KINDS = (DECREASE, INCREASE, ANY_CHANGE_PCT, ABS_THRESHOLD, ENUM_DOWNGRADE, PRESENCE, COUNT_CHANGE, NOT_EVALUABLE)

# Trigger outcome for one (expected, later-stage value) pair.
FIRES = "FIRES"
NOT_FIRING = "NOT_FIRING"
UNDETERMINED = "UNDETERMINED"   # value form/unit not comparable to the threshold -> keep the candidate (recall)
NOT_EVALUABLE_STATUS = "NOT_EVALUABLE"

CHANGE_NOT_TRIGGERING = "change_not_triggering"
TRIGGER_NOT_EVALUABLE = "trigger_not_evaluable"

# How a numeric equality is judged (identical to generic_matrix_extraction.values_equal).
def _tolerance(expected: Decimal) -> Decimal:
    return max(Decimal("0.01"), abs(expected) * Decimal("0.001"))


@dataclass(frozen=True, slots=True)
class TriggerRule:
    code: str
    kind: str
    source_text: str
    # DECREASE / INCREASE: optional minimum relative change (percent) for the trigger to fire.
    # ANY_CHANGE_PCT: the percent threshold (0 == any change).
    pct: Decimal | None = None
    # ABS_THRESHOLD
    op: str | None = None                # "<", "<=", ">", ">="
    threshold: Decimal | None = None
    threshold_unit: str | None = None
    target: str = "VALUE"                # "VALUE" (RD/ID value vs threshold) or "DELTA" (|RD/ID - PD| vs threshold)
    # ENUM_DOWNGRADE: canonical values best-first ("I", "II", ...) OR numeric_order=True (higher number = stronger).
    order: tuple[str, ...] | None = None
    numeric_order: bool = False
    needs_review: bool = False
    note: str = ""
    applies_to_rule_pack: bool = False

    def label(self) -> str:
        if self.kind == ANY_CHANGE_PCT:
            return "ANY_CHANGE" if not self.pct else f"ANY_CHANGE_PCT({self.pct})"
        if self.kind in (DECREASE, INCREASE) and self.pct:
            return f"{self.kind}(>{self.pct}%)"
        if self.kind == ABS_THRESHOLD:
            scope = "" if self.target == "VALUE" else "|delta|"
            return f"ABS_THRESHOLD({scope}{self.op} {self.threshold} {self.threshold_unit})"
        if self.kind == ENUM_DOWNGRADE:
            return "ENUM_DOWNGRADE(" + ("numeric" if self.numeric_order else ">".join(self.order or ())) + ")"
        return self.kind

    def to_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind,
            "label": self.label(),
            "pct": str(self.pct) if self.pct is not None else None,
            "op": self.op,
            "threshold": str(self.threshold) if self.threshold is not None else None,
            "threshold_unit": self.threshold_unit,
            "target": self.target if self.kind == ABS_THRESHOLD else None,
            "order": list(self.order) if self.order else ("numeric_higher_is_stronger" if self.numeric_order else None),
            "needs_review": self.needs_review,
            "applies_to_rule_pack": self.applies_to_rule_pack,
            "note": self.note,
            "source_text": self.source_text,
        }


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    status: str                      # FIRES | NOT_FIRING | UNDETERMINED | NOT_EVALUABLE
    rule: TriggerRule
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fires_or_unknown(self) -> bool:
        """True when a candidate must be kept (recall-preserving): fires, or could not be decided."""
        return self.status in (FIRES, UNDETERMINED)

    def to_delta(self) -> dict[str, Any]:
        return {
            "kind": self.rule.kind,
            "label": self.rule.label(),
            "needs_review": self.rule.needs_review,
            "evaluation": self.status,
            "source_text": self.rule.source_text,
            **self.detail,
        }

# ---------------------------------------------------------------------------------------------
# Verbatim trigger text of parameter_catalog_132.jsonl (drift-checked by verify_against_catalog).
# ---------------------------------------------------------------------------------------------
_CATALOG_TRIGGER_TEXT: dict[str, str] = {
    'PZ-001': 'Расхождение контуров здания на генплане с данными БТИ или РД > 0.',
    'PZ-002': 'Дельта общей площади между ПД и РД (или ИД) > 1%.',
    'PZ-003': 'Сокращение полезной площади в РД в пользу технических зон.',
    'PZ-004': 'Изменение внешних объемных габаритов здания в РД без корректировки ПД.',
    'PZ-005': 'Изменение глубины заложения или объема паркинга/подвала в РД.',
    'PZ-006': 'Изменение общего числа этажей (включая технический) в РД.',
    'PZ-007': 'Несовпадение количества надземных этажей в РД с утвержденной ПД.',
    'PZ-008': 'Увеличение высоты здания в РД (риск нарушения ограничений приаэродромных зон).',
    'PZ-009': 'Несоответствие абсолютной отметки нуля на чертежах РД геоподоснове ПД.',
    'PZ-010': 'Изменение общего количества жилых единиц (перепланировка в РД).',
    'PZ-011': 'Изменение пропорций (например, замена 3-комнатной на две студии).',
    'PZ-012': 'Сокращение количества машино-мест в РД (нарушение местных нормативов).',
    'PZ-013': 'Снижение мощности (выпуск продукции в год) или вместимости (школа/детсад).',
    'PZ-014': 'Превышение расчетной мощности в РД над лимитом ТУ, выданным в ПД.',
    'PZ-015': 'Снижение категории (например, перевод ИТП с 1-й на 2-ю категорию).',
    'PZ-016': 'Выход расчетного расхода в РД за объемы, утвержденные экспертизой в ПД.',
    'PZ-017': 'Превышение суммарной тепловой нагрузки в спецификациях и таблицах РД.',
    'PZ-018': 'Увеличение потребления газа в РД выше лимита, одобренного в ПД.',
    'PZ-019': 'Превышение КЗ над предельным значением, утвержденным в ПД.',
    'PZ-020': 'Выход за нормативные рамки плотности, утвержденные в ПД.',
    'PZ-021': 'Снижение класса энергоэффективности в РД/ИД относительно ПД.',
    'PZ-022': 'Снижение степени огнестойкости (например, с I на II) в РД.',
    'PZ-023': 'Снижение класса пожарной опасности (например, с С0 на С1) в РД.',
    'SPZU-024': 'Дельта объемов выемки и обратной засыпки между стадиями > 5%.',
    'SPZU-025': 'Увеличение или уменьшение площади твердых покрытий > 5%.',
    'SPZU-026': 'Изменение объемов мощения без согласования заказчика.',
    'SPZU-027': 'Сокращение площади озеленения в РД/ИД ниже минимальных требований ГПЗУ.',
    'SPZU-028': 'Уменьшение нормативной площади площадок отдыха/спорта в РД.',
    'SPZU-029': 'Сокращение позиций МАФ; подмена на аналоги низкого качества.',
    'SPZU-030': 'Ширина пожарного проезда в РД или в натуре (ИД) менее 4.2 м.',
    'SPZU-031': 'Уменьшение радиуса поворота пожарной техники < 10-12 м (норматив).',
    'SPZU-032': 'Исключение слоев или уменьшение толщины асфальта/щебня в РД и ИД.',
    'SPZU-033': 'Нарушение уклонов, ведущее к застою воды или превышению крутизны.',
    'SPZU-034': 'Смещение точки подключения наружной сети относительно ТУ и ПД > 0.5 м.',
    'SPZU-035': 'Посадка временных зданий, кранов или сетей в охранную зону.',
    'SPZU-036': 'Замена типа ограждения (например, шумозащита на сетку); занижение высоты.',
    'SPZU-037': 'Сокращение количества наземных парковочных мест в РД.',
    'SPZU-038': 'Отсутствие или уменьшение доли мест для МГН (< 10% от общего числа).',
    'SPZU-039': 'Исключение из РД открытых лотков или закрытых систем пристенного дренажа.',
    'AR-040': 'Снижение ширины коридора в РД/ИД менее 1.2 м (СП 1.13130).',
    'AR-041': 'Ширина дверного полотна на путях эвакуации в РД/ИД < 0.9 м.',
    'AR-042': 'Высота коридоров < 2.0 м или дверей < 1.9 м (СП 1.13130).',
    'AR-043': 'Дверь открывается внутрь помещения или блокирует смежный поток эвакуации.',
    'AR-044': 'Замена кровельной мембраны на класс ниже; исключение пароизоляционного слоя.',
    'AR-045': 'Уменьшение уклона кровли ниже проектного; сокращение количества воронок.',
    'AR-046': 'Сокращение количества окон; уменьшение площади остекления (риск КЕО).',
    'AR-047': 'Глубина тамбура в РД/ИД менее 1.5-1.8 м (невозможность проезда коляски).',
    'AR-048': 'Изменение количества ступеней; высота подступенка > 150 мм или проступь < 300 мм.',
    'AR-049': 'Снижение высоты ограждения < 1.2 м для кровли/балкона (СП 54.13330).',
    'AR-050': 'Подмена негорючей отделки (КМ0/КМ1) на горючую (КМ3) на путях эвакуации.',
    'AR-051': 'Изменение конфигурации оконных переплетов в РД, снижающее светопропускание.',
    'AR-052': 'Изменение колористического решения фасада в РД без согласования.',
    'AR-053': 'Отсутствие в РД демпферных лент в местах сопряжения перегородок и перекрытий.',
    'KR-054': 'Самовольное изменение шага колонн или сдвиг осей в РД/ИД.',
    'KR-055': 'Понижение класса бетона несущих элементов (например, B35 на B30) в РД/ИД.',
    'KR-056': 'Подмена марки стали на менее прочную (например, С345 на С245).',
    'KR-057': 'Замена класса арматуры на менее пластичный (например, А500С на А400).',
    'KR-058': 'Уменьшение проектной толщины плиты в РД или по факту заливки (ИД).',
    'KR-059': 'Уменьшение конструктивной толщины диска перекрытия (риск деформаций).',
    'KR-060': 'Уменьшение площади поперечного сечения несущей вертикальной конструкции.',
    'KR-061': 'Уменьшение толщины стен лифтовых шахт или пилонов диафрагм жесткости.',
    'KR-062': 'Занижение диаметра продольных арматурных стержней в спецификациях РД.',
    'KR-063': 'Отсутствие, исключение или конструктивное искажение узла шва в РД.',
    'KR-064': 'Сужение шахты или смещение закладных элементов в РД (риск монтажа лифта).',
    'KR-065': 'Самовольная заделка крупных проемов в РД без обрамляющего армирования.',
    'KR-066': 'Замена состава на менее огнестойкий (снижение предела REI).',
    'KR-067': 'Итоговое расхождение объемов материалов между ПД, РД и ИД > 2%.',
    'IOS1-068': 'Занижение или завышение токов уставки в РД (риск пожара или ложных отключений).',
    'IOS1-069': 'Занижение сечения кабелей или подмена негорючих марок (например, FRLS на обычный LS).',
    'IOS1-070': 'Уменьшение количества заземлителей или исключение элементов молниезащиты в РД.',
    'IOS2-071': 'Уменьшение диаметров стояков или магистралей ХВС/ГВС в РД/ИД.',
    'IOS2-072': 'Подмена оцинкованных/чугунных труб на полипропилен без перерасчета расширения.',
    'IOS2-073': 'Занижение рабочих параметров закупаемого насосного оборудования в РД.',
    'IOS3-074': 'Уменьшение диаметров выпусков канализации из здания в РД (риск засоров).',
    'IOS3-075': 'Самовольная замена малошумных или чугунных труб на тонкостенный ПВХ.',
    'IOS4-076': 'Изменение диаметров Т1/Т2, нарушающее гидравлическую увязку системы.',
    'IOS4-077': 'Сокращение количества секций или замена на модели с меньшей теплоотдачей.',
    'IOS4-078': 'Уменьшение площади сечения воздуховода в РД (падение объема воздуха, рост шума).',
    'IOS4-079': 'Подмена установок на аналоги с меньшей кратностью воздухообмена.',
    'IOS5-080': 'Сокращение количества извещателей в РД или нарушение расстояний между ними.',
    'POS-081': 'Выход опасной зоны работы стрелы крана за границы участка или на жилые здания.',
    'POS-082': 'Превышение продолжительности критического этапа в графике РД > 10%.',
    'POS-083': 'Размещение бытовок в охранных зонах сетей или с нарушением пожарных разрывов.',
    'POS-084': 'Сужение временных дорог < 3.5-4.5 м (нарушение ПОС).',
    'POS-085': 'Размещение тяжелых конструкций в зоне откосов котлована или на непредусмотренных перекрытиях.',
    'POS-086': 'Превышение пиковой численности персонала в РД над вместимостью бытового городка.',
    'POS-087': 'Самовольное изменение критических технологий (например, монолит на сборный ЖБ).',
    'POS-088': 'Превышение запрашиваемой мощности для строймашин над лимитом ПОС.',
    'POS-089': 'Отсутствие или перенос пункта мойки колес в зону без оборотного водоснабжения.',
    'POD-090': 'Выход зоны развала в ППР за границы участка или на пешеходные зоны.',
    'POD-091': 'Самовольное изменение метода (например, с плазменного на обрушение экскаватором).',
    'POD-092': 'Отсутствие в ППР защитных мероприятий для сетей в зоне работы техники.',
    'POD-093': 'Расхождение объемов лома между ПД и РД/ИД > 5%.',
    'POD-094': 'Превышение объемов опасных отходов в ИД или занижение класса опасности.',
    'POD-095': 'Отсутствие систем гидроорошения или защитных экранов в РД при демонтаже стен.',
    'POD-096': 'Занижение сечений элементов расчленения в РД; отсутствие актов монтажа.',
    'POD-097': 'Складирование тяжелого мусора на бровке котлована или над действующими сетями.',
    'OOS-098': 'Начало земляных/демонтажных работ без открытого разрешения на перемещение ОСС.',
    'OOS-099': 'Рейс самосвала без ГЛОНАСС-трекера или не зарегистрированного в РНИС.',
    'OOS-100': 'Выезд самосвала с отходами без генерации QR-кода в "Мобильном КПТС".',
    'OOS-101': 'Выгрузка отходов на полигон, не входящий в ТРПО или без лицензии ГРОО.',
    'PPM-102': 'Нарушение непрерывности противопожарных стен в РД/ИД; увеличение площади отсека.',
    'PPM-103': 'Снижение предела огнестойкости (например, EI-60 на EI-30) в РД.',
    'PPM-104': 'Сужение коридоров < 1.2 м (СП 1.13130).',
    'PPM-105': 'Ширина дверного полотна в РД/ИД < 0.9 м.',
    'PPM-106': 'Дверь открывается внутрь или блокирует смежный поток эвакуации.',
    'PPM-107': 'Подмена КМ0/КМ1 на КМ3 на путях эвакуации в РД/ИД.',
    'PPM-108': 'Сокращение количества датчиков в РД или нарушение расстояний между ними.',
    'PPM-109': 'Применение кабеля без индекса огнестойкости (например, замена FRLS на обычный LS).',
    'PPM-110': 'Сокращение количества динамиков; отсутствие табло "Выход" на путях эвакуации.',
    'PPM-111': 'Отсутствие ОЗК в местах пересечения воздуховодами противопожарных преград в РД.',
    'PPM-112': 'Занижение производительности или давления вентиляторов ДУ в РД.',
    'PPM-113': 'Уменьшение диаметра кольцевого трубопровода ВПВ; сокращение количества кранов.',
    'PPM-114': 'Радиус действия ПГ на чертеже РД не покрывает наиболее удаленную точку здания.',
    'ODI-115': 'Исключение подъемника из РД или замена на модель без автоматического управления.',
    'ODI-116': 'Сужение коридоров < 1.5-1.8 м (СП 59.13330).',
    'ODI-117': 'Ширина полотна двери в свету < 1.5 м (СП 59.13330).',
    'ODI-118': 'Высота порога > 0.014 м (СП 59.13330).',
    'ODI-119': 'Внутренние размеры универсальной кабины в РД/ИД < 1.5 м (СП 59.13330).',
    'ODI-120': 'Отсутствие в РД откидных и стационарных поручней в санузлах для инвалидов.',
    'ODI-121': 'Сокращение количества мест для инвалидов; уменьшение ширины места < 3.5 м.',
    'ODI-122': 'Отсутствие в РД предупреждающих тактильных полос перед лестницами и дверями.',
    'ODI-123': 'Отсутствие в РД кнопок вызова персонала в санузлах или на входе в здание.',
    'ZU-124': 'Снижение проектного класса энергоэффективности в РД/ИД.',
    'ZU-125': 'Уменьшение толщины утеплителя в РД, ведущее к падению сопротивления теплопередаче.',
    'ZU-126': 'Замена утеплителя на аналог с более высоким коэффициентом теплопроводности.',
    'ZU-127': 'Ухудшение теплозащитных свойств оконных блоков в РД/ИД.',
    'ZU-128': 'Уменьшение проектного слоя кровельного утеплителя в РД.',
    'ZU-129': 'Отсутствие в РД общедомовых/поквартирных счетчиков (электричество, вода, тепло).',
    'ZU-130': 'Замена светодиодных светильников (LED) на люминесцентные или лампы накаливания.',
    'ZU-131': 'Превышение удельного годового расхода энергии в РД над лимитом из ПД.',
    'SM-132': 'Превышение итоговой стоимости строительства в РД/ИД над утвержденной ПД > 5%.',
}


# ---------------------------------------------------------------------------------------------
# Classification: ONE review pass over the 132 catalog triggers (wording only, no gold).
# (kind, options...) -- see TriggerRule. `nr=True` == needs_review.
# ---------------------------------------------------------------------------------------------
_A0 = (ANY_CHANGE_PCT, {"pct": Decimal(0)})


def _dec(pct: int | float | str | None = None, **kw: Any) -> tuple[str, dict[str, Any]]:
    return (DECREASE, {"pct": Decimal(str(pct)) if pct else None, **kw})


def _inc(pct: int | float | str | None = None, **kw: Any) -> tuple[str, dict[str, Any]]:
    return (INCREASE, {"pct": Decimal(str(pct)) if pct else None, **kw})


def _any(pct: int | float | str = 0, **kw: Any) -> tuple[str, dict[str, Any]]:
    return (ANY_CHANGE_PCT, {"pct": Decimal(str(pct)), **kw})


def _abs(op: str, value: str, unit: str, *, target: str = "VALUE", **kw: Any) -> tuple[str, dict[str, Any]]:
    return (ABS_THRESHOLD, {"op": op, "threshold": Decimal(value), "threshold_unit": unit, "target": target, **kw})


def _enum(order: Sequence[str] | None = None, *, numeric: bool = False, **kw: Any) -> tuple[str, dict[str, Any]]:
    return (ENUM_DOWNGRADE, {"order": tuple(order) if order else None, "numeric_order": numeric, **kw})


_KM_ORDER = ("KM0", "KM1", "KM2", "KM3", "KM4", "KM5")
_ENERGY_ORDER = ("A++", "A+", "A", "B+", "B", "C+", "C", "D", "E")
_ROMAN_ORDER = ("I", "II", "III", "IV", "V")
_HAZARD_ORDER = ("C0", "C1", "C2", "C3")
_CATEGORY_ORDER = ("I", "II", "III")

_NE = (NOT_EVALUABLE, {})
_PRES = (PRESENCE, {})
_CNT = (COUNT_CHANGE, {})

_CLASSIFICATION: dict[str, tuple[str, dict[str, Any]]] = {
    "PZ-001": _any(0, note='"> 0" on contour divergence == any change'),
    "PZ-002": _any(1),
    "PZ-003": _dec(needs_review=True, note='"в пользу технических зон" is not evaluable from one number; direction = decrease'),
    "PZ-004": _any(0),
    "PZ-005": _any(0),
    "PZ-006": _any(0, needs_review=True, note="trigger names the floor count, the parameter is the volume: any change kept"),
    "PZ-007": _CNT,
    "PZ-008": _inc(),
    "PZ-009": _any(0, applies_to_rule_pack=True, note="rule pack: exact inequality"),
    "PZ-010": _CNT,
    "PZ-011": (COUNT_CHANGE, {"needs_review": True, "note": "proportions vs row count: any count change kept"}),
    "PZ-012": _dec(),
    "PZ-013": _dec(),
    "PZ-014": _inc(),
    "PZ-015": _enum(_CATEGORY_ORDER),
    "PZ-016": _inc(),
    "PZ-017": _inc(),
    "PZ-018": _inc(),
    "PZ-019": _inc(),
    "PZ-020": _any(0, needs_review=True, note='"выход за нормативные рамки" is two-sided: any change kept'),
    "PZ-021": _enum(_ENERGY_ORDER),
    "PZ-022": _enum(_ROMAN_ORDER),
    "PZ-023": _enum(_HAZARD_ORDER),
    "SPZU-024": _any(5),
    "SPZU-025": _any(5),
    "SPZU-026": _any(0),
    "SPZU-027": _dec(needs_review=True, note='"ниже минимальных требований ГПЗУ" needs the ГПЗУ minimum; direction = decrease'),
    "SPZU-028": _dec(),
    "SPZU-029": _dec(needs_review=True, note='"подмена на аналоги" is not evaluable; positions decrease'),
    "SPZU-030": _abs("<", "4.2", "м"),
    "SPZU-031": _abs("<", "12", "м", needs_review=True, note='catalog "10-12 м": the larger bound is used so no candidate is lost'),
    "SPZU-032": _dec(needs_review=True, note="layer exclusion is also a trigger clause"),
    "SPZU-033": _any(0, needs_review=True, note="slope violation has no numeric bound in the catalog"),
    "SPZU-034": _abs(">", "0.5", "м", target="DELTA"),
    "SPZU-035": _NE,
    "SPZU-036": _dec(needs_review=True, note='"замена типа" clause is not numeric; height decrease evaluated'),
    "SPZU-037": _dec(),
    "SPZU-038": (COUNT_CHANGE, {"needs_review": True, "note": 'share < 10% depends on the total; any count change kept'}),
    "SPZU-039": _PRES,
    "AR-040": _abs("<", "1.2", "м"),
    "AR-041": _abs("<", "0.9", "м"),
    "AR-042": _abs("<", "2.0", "м", needs_review=True, note="two thresholds (corridor 2.0 / door 1.9): the larger one is used so no candidate is lost"),
    "AR-043": _NE,
    "AR-044": _any(0, needs_review=True, note="membrane class has no ordering in the catalog"),
    "AR-045": _dec(needs_review=True, note="slope decrease evaluated; funnel count is the second clause"),
    "AR-046": _dec(),
    "AR-047": _abs("<", "1.8", "м", needs_review=True, note='catalog "1.5-1.8 м": the larger bound is used so no candidate is lost'),
    "AR-048": _any(0, needs_review=True, note="mixed clauses (count change / riser > 150 / tread < 300)"),
    "AR-049": _abs("<", "1.2", "м"),
    "AR-050": _enum(_KM_ORDER),
    "AR-051": _dec(needs_review=True, note="window-frame configuration -> light transmission: decrease evaluated"),
    "AR-052": _any(0),
    "AR-053": _PRES,
    "KR-054": _any(0),
    "KR-055": _enum(numeric=True, applies_to_rule_pack=True, note="rule pack: numeric grade decrease"),
    "KR-056": _enum(numeric=True),
    "KR-057": _enum(numeric=True, needs_review=True, note='text says "менее пластичный" but the catalog example is a numeric decrease (А500С -> А400)'),
    "KR-058": _dec(applies_to_rule_pack=True, note="rule pack: min thickness decrease"),
    "KR-059": _dec(),
    "KR-060": _dec(),
    "KR-061": _dec(),
    "KR-062": _dec(),
    "KR-063": _PRES,
    "KR-064": _dec(needs_review=True, note='"смещение закладных" clause is not numeric; narrowing evaluated'),
    "KR-065": _dec(needs_review=True, note="opening infill == opening area decrease"),
    "KR-066": _enum(numeric=True, needs_review=True, note="REI/thickness pair: numeric decrease of the rating"),
    "KR-067": _any(2),
    "IOS1-068": _any(0),
    "IOS1-069": _dec(needs_review=True, note='"подмена негорючих марок" clause is not numeric'),
    "IOS1-070": _dec(),
    "IOS2-071": _dec(),
    "IOS2-072": _any(0, needs_review=True, note="pipe material has no ordering in the catalog"),
    "IOS2-073": _dec(),
    "IOS3-074": _dec(),
    "IOS3-075": _any(0, needs_review=True, note="pipe material has no ordering in the catalog"),
    "IOS4-076": _any(0),
    "IOS4-077": _dec(),
    "IOS4-078": _dec(applies_to_rule_pack=True, needs_review=True, note="rule pack owns the decision"),
    "IOS4-079": _dec(applies_to_rule_pack=True, needs_review=True, note="rule pack owns the decision"),
    "IOS5-080": _dec(needs_review=True, note='"нарушение расстояний" clause is not numeric'),
    "POS-081": _NE,
    "POS-082": _inc(10),
    "POS-083": _NE,
    "POS-084": _abs("<", "4.5", "м", needs_review=True, note='catalog "3.5-4.5 м": the larger bound is used so no candidate is lost'),
    "POS-085": _NE,
    "POS-086": _inc(),
    "POS-087": _any(0),
    "POS-088": _inc(),
    "POS-089": _PRES,
    "POD-090": _NE,
    "POD-091": _any(0),
    "POD-092": _PRES,
    "POD-093": _any(5),
    "POD-094": _any(0, needs_review=True, note="two clauses in opposite directions (mass increase / class downgrade)"),
    "POD-095": _PRES,
    "POD-096": _dec(needs_review=True, note='"отсутствие актов монтажа" clause is not numeric'),
    "POD-097": _NE,
    "OOS-098": _NE,
    "OOS-099": _NE,
    "OOS-100": _NE,
    "OOS-101": _NE,
    "PPM-102": _any(0, needs_review=True, note="continuity (presence) and compartment-area increase clauses"),
    "PPM-103": _enum(numeric=True),
    "PPM-104": _abs("<", "1.2", "м"),
    "PPM-105": _abs("<", "0.9", "м"),
    "PPM-106": _NE,
    "PPM-107": _enum(_KM_ORDER),
    "PPM-108": _dec(needs_review=True, note='"нарушение расстояний" clause is not numeric'),
    "PPM-109": _any(0, needs_review=True, note="cable marking has no ordering in the catalog"),
    "PPM-110": _dec(needs_review=True, note='"отсутствие табло" clause is a second, non-count clause'),
    "PPM-111": _PRES,
    "PPM-112": _dec(),
    "PPM-113": _dec(),
    "PPM-114": _NE,
    "ODI-115": _PRES,
    "ODI-116": _abs("<", "1.8", "м", needs_review=True, note='catalog "1.5-1.8 м": the larger bound is used so no candidate is lost'),
    "ODI-117": _abs("<", "1.5", "м", needs_review=True, note="threshold 1.5 m taken as written in the catalog"),
    "ODI-118": _abs(">", "0.014", "м"),
    "ODI-119": _abs("<", "1.5", "м"),
    "ODI-120": _PRES,
    "ODI-121": _dec(needs_review=True, note='"ширина места < 3.5 м" clause is the second clause'),
    "ODI-122": _PRES,
    "ODI-123": _PRES,
    "ZU-124": _enum(_ENERGY_ORDER),
    "ZU-125": _dec(),
    "ZU-126": _inc(),
    "ZU-127": _dec(),
    "ZU-128": _dec(),
    "ZU-129": _PRES,
    "ZU-130": _any(0, needs_review=True, note="lamp type has no ordering in the catalog"),
    "ZU-131": _inc(),
    "SM-132": _inc(5),
}


def _build_policy() -> dict[str, TriggerRule]:
    missing = set(_CATALOG_TRIGGER_TEXT) ^ set(_CLASSIFICATION)
    if missing:  # import-time guard: the table must cover exactly the catalog's codes
        raise RuntimeError(f"trigger policy is out of sync with the catalog text table: {sorted(missing)}")
    out: dict[str, TriggerRule] = {}
    for code, (kind, options) in _CLASSIFICATION.items():
        out[code] = TriggerRule(code=code, kind=kind, source_text=_CATALOG_TRIGGER_TEXT[code], **options)
    return out


TRIGGER_POLICY: dict[str, TriggerRule] = _build_policy()

_TRIGGER_IN_LOGIC_RE = re.compile(r"(?:^|;)\s*trigger=(.*)$", re.DOTALL)


def trigger_text_from_logic(trigger_logic: object) -> str:
    """`Param.trigger_logic` of an official-catalog parameter is `key=value;...;trigger=<text>`."""
    match = _TRIGGER_IN_LOGIC_RE.search(str(trigger_logic or ""))
    return match.group(1).strip() if match else ""


def rule_for(code: object, *, trigger_logic: object = None) -> TriggerRule:
    """The reviewed rule for `code`; a code missing from the table (e.g. a matrix revision that adds a
    parameter) degrades to the conservative ANY_CHANGE rule with `needs_review`."""
    key = str(code or "")
    rule = TRIGGER_POLICY.get(key)
    if rule is not None:
        return rule
    return TriggerRule(
        code=key, kind=ANY_CHANGE_PCT, pct=Decimal(0), needs_review=True,
        source_text=trigger_text_from_logic(trigger_logic), note="code not in the reviewed trigger table",
    )


# ---------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------
_UNIT_STRIP_RE = re.compile(r"[\s.]+")


def normalize_unit(unit: object) -> str:
    text = str(unit or "").strip().lower().replace("²", "2").replace("³", "3")
    return _UNIT_STRIP_RE.sub("", text)


_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}
_MAGNITUDE_GUARD = Decimal(200)


def _ev(rule: TriggerRule, status: str, **detail: Any) -> TriggerEvaluation:
    return TriggerEvaluation(status=status, rule=rule, detail={k: (str(v) if isinstance(v, Decimal) else v) for k, v in detail.items()})


def evaluate_numeric(
    rule: TriggerRule, expected: Decimal, later: Decimal, *, value_unit: str | None = None, stage: str | None = None,
) -> TriggerEvaluation:
    """Does the change `expected` (PD) -> `later` (RD or ID) satisfy `rule`? Only meaningful for values the
    mechanism already judged different; identical values simply return NOT_FIRING for every kind but
    ABS_THRESHOLD/PRESENCE, whose semantics do not depend on a change."""
    base: dict[str, Any] = {"expected": expected, "later": later}
    if stage:
        base["stage"] = stage
    kind = rule.kind
    if kind == NOT_EVALUABLE:
        return _ev(rule, NOT_EVALUABLE_STATUS, **base)
    tol = _tolerance(expected)
    change = later - expected
    changed = abs(change) > tol
    pct_change: Decimal | None = None
    if expected != 0:
        pct_change = abs(change) / abs(expected) * 100
    base["change"] = change
    if pct_change is not None:
        base["change_pct"] = pct_change.quantize(Decimal("0.01"))

    if kind == PRESENCE:
        gone = expected != 0 and abs(later) <= Decimal("0.0000001")
        return _ev(rule, FIRES if gone else NOT_FIRING, **base)
    if kind == COUNT_CHANGE:
        return _ev(rule, FIRES if later != expected else NOT_FIRING, **base)
    if kind in (DECREASE, INCREASE):
        directional = changed and ((change < 0) if kind == DECREASE else (change > 0))
        if directional and rule.pct and pct_change is not None and pct_change <= rule.pct:
            directional = False
        return _ev(rule, FIRES if directional else NOT_FIRING, **base)
    if kind == ANY_CHANGE_PCT:
        if not changed:
            return _ev(rule, NOT_FIRING, **base)
        threshold = rule.pct or Decimal(0)
        if threshold == 0 or pct_change is None:
            return _ev(rule, FIRES, **base)
        return _ev(rule, FIRES if pct_change > threshold else NOT_FIRING, **base)
    if kind == ABS_THRESHOLD:
        assert rule.op in _OPS and rule.threshold is not None
        if rule.target == "DELTA":
            return _ev(rule, FIRES if _OPS[rule.op](abs(change), rule.threshold) else NOT_FIRING, **base)
        if value_unit and normalize_unit(value_unit) != normalize_unit(rule.threshold_unit):
            return _ev(rule, UNDETERMINED, reason="unit_mismatch", value_unit=value_unit, threshold_unit=rule.threshold_unit, **base)
        if later > 0 and rule.threshold > 0:
            ratio = later / rule.threshold
            if ratio > _MAGNITUDE_GUARD or ratio < 1 / _MAGNITUDE_GUARD:
                return _ev(rule, UNDETERMINED, reason="magnitude_out_of_range", **base)
        return _ev(rule, FIRES if _OPS[rule.op](later, rule.threshold) else NOT_FIRING, **base)
    # ENUM_DOWNGRADE on a plain number (a catalog/mechanism mismatch): cannot be decided from a rank.
    return _ev(rule, UNDETERMINED, reason="enum_rule_on_numeric_value", **base)


_LEGACY_REBAR = {"I": 240, "II": 300, "III": 400, "IV": 600, "V": 800, "VI": 1000}
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def grade_number(canonical: str) -> Decimal | None:
    """Numeric strength of a canonical grade (B35 -> 35, C345K -> 345, A500C -> 500, A-III -> 400, EI60 -> 60)."""
    text = str(canonical or "").strip().upper()
    legacy = re.fullmatch(r"[AА]-(VI|IV|V|III|II|I)", text)
    if legacy:
        return Decimal(_LEGACY_REBAR[legacy.group(1)])
    match = _NUMBER_RE.search(text)
    return Decimal(match.group(0).replace(",", ".")) if match else None


def evaluate_enum(rule: TriggerRule, expected: str, later: str, *, stage: str | None = None) -> TriggerEvaluation:
    base: dict[str, Any] = {"expected": expected, "later": later}
    if stage:
        base["stage"] = stage
    if rule.kind == NOT_EVALUABLE:
        return _ev(rule, NOT_EVALUABLE_STATUS, **base)
    if expected == later:
        return _ev(rule, NOT_FIRING, **base)
    if rule.kind == ENUM_DOWNGRADE:
        if rule.numeric_order:
            left, right = grade_number(expected), grade_number(later)
            if left is None or right is None:
                return _ev(rule, UNDETERMINED, reason="grade_not_numeric", **base)
            return _ev(rule, FIRES if right < left else NOT_FIRING, **base)
        order = rule.order or ()
        if expected in order and later in order:
            return _ev(rule, FIRES if order.index(later) > order.index(expected) else NOT_FIRING, **base)
        return _ev(rule, UNDETERMINED, reason="value_not_in_order", **base)
    if rule.kind in (ANY_CHANGE_PCT, COUNT_CHANGE):
        return _ev(rule, FIRES, **base)
    return _ev(rule, UNDETERMINED, reason="rule_kind_needs_a_number", **base)


def evaluate_components(
    rule: TriggerRule,
    labels: Sequence[str],
    expected: Sequence[Decimal],
    later: Sequence[Decimal],
    *,
    stage: str | None = None,
) -> TriggerEvaluation:
    """Compound (N-field) value: the trigger fires when it fires on any component. An ABS_THRESHOLD is only
    applied to the component whose unit label is the threshold's unit (a width bound never judges a count)."""
    if rule.kind == NOT_EVALUABLE:
        return _ev(rule, NOT_EVALUABLE_STATUS, stage=stage)
    per_component = []
    for label, left, right in zip(labels, expected, later):
        if rule.kind == ABS_THRESHOLD and rule.target == "VALUE" and normalize_unit(label) != normalize_unit(rule.threshold_unit):
            continue
        sub = evaluate_numeric(rule, left, right, value_unit=label if rule.kind == ABS_THRESHOLD else None)
        per_component.append({"label": label, "status": sub.status, "expected": str(left), "later": str(right)})
    if not per_component:
        return _ev(rule, UNDETERMINED, reason="no_component_matches_threshold_unit", stage=stage)
    statuses = {c["status"] for c in per_component}
    status = FIRES if FIRES in statuses else (UNDETERMINED if UNDETERMINED in statuses else NOT_FIRING)
    return _ev(rule, status, components=per_component, stage=stage)


def aggregate(rule: TriggerRule, evaluations: Iterable[TriggerEvaluation]) -> TriggerEvaluation:
    """Group-level verdict over the per-stage (PD->RD, PD->ID) evaluations."""
    items = list(evaluations)
    if rule.kind == NOT_EVALUABLE or any(item.status == NOT_EVALUABLE_STATUS for item in items):
        return TriggerEvaluation(status=NOT_EVALUABLE_STATUS, rule=rule, detail={})
    statuses = [item.status for item in items]
    if FIRES in statuses:
        status = FIRES
    elif UNDETERMINED in statuses:
        status = UNDETERMINED
    else:
        status = NOT_FIRING
    return TriggerEvaluation(status=status, rule=rule, detail={"per_stage": [item.detail for item in items]})


# ---------------------------------------------------------------------------------------------
# Review table export
# ---------------------------------------------------------------------------------------------
def policy_rows() -> list[dict[str, Any]]:
    return [TRIGGER_POLICY[code].to_row() for code in TRIGGER_POLICY]


def kind_counts() -> dict[str, int]:
    counts = {kind: 0 for kind in TRIGGER_KINDS}
    for rule in TRIGGER_POLICY.values():
        counts[rule.kind] += 1
    return counts


def verify_against_catalog(catalog_rows: Iterable[dict[str, Any]]) -> list[str]:
    """Drift check: the embedded verbatim trigger text must equal the catalog's, code for code."""
    problems: list[str] = []
    seen: set[str] = set()
    for row in catalog_rows:
        code = str(row.get("parameter_code") or "")
        seen.add(code)
        rule = TRIGGER_POLICY.get(code)
        if rule is None:
            problems.append(f"{code}: not in the trigger policy table")
        elif rule.source_text != str(row.get("trigger") or ""):
            problems.append(f"{code}: trigger text drifted from the catalog")
    problems.extend(f"{code}: in the table but not in the catalog" for code in sorted(set(TRIGGER_POLICY) - seen))
    return problems


def render_markdown(rows: Sequence[dict[str, Any]] | None = None) -> str:
    rows = list(rows if rows is not None else policy_rows())
    lines = [
        "| Код | Тип триггера | needs_review | Правило-пак | Комментарий | Исходный текст каталога |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        cells = [
            row["code"], row["label"], "да" if row["needs_review"] else "", "да" if row["applies_to_rule_pack"] else "",
            row["note"], row["source_text"],
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines) + "\n"
