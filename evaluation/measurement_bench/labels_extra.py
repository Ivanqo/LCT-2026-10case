"""Second labelling pass (still before any extractor run): extra holdout triples
(ID stage, building-level PZ-015, guard/evacuation-height/fire-road statements,
KR core) and the planned NOV/TYU attempts that the first pass had not yet
covered.  Provenance is recomputed by build_corpus.py, not trusted from here."""
from __future__ import annotations

from .corpus_labels import P, V

# ---------------------------------------------------------------- PZ-015 (building category) ----
V("ALT79B", "PZ-015", "PD", "II", 24, 6, "−Категория электроснабжения: II", any_of=((24, 8, "I", "fire-fighting loads are category I in the same section"),),
  conf="MEDIUM", prov="PZ015_REVISIT", note="building-level category II; fire equipment I (any_of)")
V("POL17", "PZ-015", "PD", "II", 35, 8, "Категория надежности электроснабжения II.", any_of=((35, 8, "I", "'относятся к 1-ой категории' for specific loads"),),
  conf="MEDIUM", prov="PZ015_REVISIT")
V("IZM12", "PZ-015", "PD", "II", 27, 7, "объекта по II категории надежности от двух независимых источников", any_of=((27, 10, "I", "ИТП loads"),),
  conf="MEDIUM", prov="PZ015_REVISIT")

# ---------------------------------------------------------------- ID stage ----
V("POL17", "KR-057", "ID", "A500", 1, 1305, "Ф12 А500 25.03.2025", alt=((1, 1305, "А500"),), conf="MEDIUM", prov="ID_SEARCH",
  note="ID certificate/journal lines print 'А500' without the С suffix (PD/RD say А500С)")
V("IZM12", "KR-057", "ID", "A500C", 1, 3, "Ø10 мм А500С 30.05.2025", alt=((1, 3, "А500С"),), prov="ID_SEARCH")
V("POL17", "KR-056", "ID", "C255", 10, 44, "1 120 Двутавр 40Б1 11 1 1120001 100 в 1 22915 С255 1 24 76,296", conf="MEDIUM", prov="ID_SEARCH",
  note="ID material journal row of the КМ set")

# ---------------------------------------------------------------- statement-type scalars ----
V("ALT79B", "AR-049", "PD", "1200", 15, 9, "Высота ограждения лестничных маршей - 1200мм;", any_of=((15, 9, "600", "roof guard 'не менее 600мм'"),), conf="MEDIUM", prov="AR049_CORE",
  note="printed in mm (catalog unit is m): the number is recorded as printed, no unit conversion")
V("POL17", "AR-049", "PD", "0.9", 31, 7, "Лестницы оборудованы металлическим ограждением с поручнями высотой 0,9м", conf="MEDIUM", prov="AR049_CORE")
V("IZM12", "AR-049", "PD", "1.2", 13, 19, "ограждения балконов и лоджий выполнить непрерывным с поручнями высотой 1,2 м.", conf="MEDIUM", prov="AR049_CORE")
V("DOO25", "AR-049", "PD", "1.2", 793, 9, "высотой 1,2 м с поручнями с обеих сторон на высоте 0,9 и 0,5 м.", alt=((875, 10, "1,2"),), conf="MEDIUM", prov="AR049_CORE",
  note="guard 1,2 m; handrails at 0,9 and 0,5 m")
V("OKT103", "AR-042", "PD", "1.9", 156, 43, "6.33 Высота эвакуационных выходов в свету предусмотрена, не менее 1,9 м", conf="MEDIUM", prov="AR042_CORE")
V("DOO25", "AR-042", "PD", "1.9", 822, 10, "Высота эвакуационных выходов в свету составляет не менее 1,9 м", conf="MEDIUM", prov="AR042_CORE")
V("IZM12", "SPZU-030", "PD", "6", 62, 16, "8.2.2. Ширина проездов для пожарной техники составляет не менее 6 м", conf="MEDIUM", prov="SPZU030_CORE")
V("ALT79B", "SPZU-030", "PD", "4.2", 12, 13, "Ширина проездов для пожарной техники составляет не менее 4,2 метров", any_of=((66, 13, "3.5", "ПБ: не менее 3,5 м"),), conf="MEDIUM", prov="SPZU030_CORE")

# ---------------------------------------------------------------- KR / SPZU extras ----
V("POL17", "SPZU-027", "PD", "2356.4", 73, 9, "5. Площадь озеленения кв.м. 2356.4", alt=((65, 20, "2356,4"),), conf="MEDIUM", prov="SPZU_CORE",
  note="ПЗУ1 МГЭ; design generation of this ПЗУ not established")
V("POL17", "KR-059", "RD", "180", 114, 5, "t=180 толщина плиты площадки", alt=((114, 6, "180"),), conf="MEDIUM", prov="KR_CORE",
  note="RD КЖ0.3.1 stair-platform slab thickness; other slabs may differ")
V("DOO25", "KR-056", "PD", "C245", 794, 22, "ограждение представляет собой металлическую конструкцию из стали класса С245", any_of=((794, 13, "С235", "профили"),), conf="MEDIUM", prov="KR_CORE")
V("TYU", "SPZU-038", "PD", "1", "F0151", 15, "проектом предусмотрено 1 место для МГН М4.", alt=(("F0151", 16, "1"),), prov="SPZU_CORE")

# ---------------------------------------------------------------- planned NOV attempts (dev) ----
for _c, _st, _why in (
    ("AR-046", "NOT_SCALAR", "window blocks described by type in ПЗ prose"), ("AR-047", "ABSENT", "vestibule dimensions only as a requirement heading"),
    ("AR-050", "NOT_SCALAR", "finishing per room type"), ("IOS2-073", "NOT_SCALAR", "pump stations without a (Q, H, N) tuple in the text layer"),
    ("IOS4-076", "ABSENT", "no heating riser diameters in text"), ("KR-064", "ABSENT", "no lift-shaft dimension statement"),
    ("KR-067", "NOT_SCALAR", "per-drawing-set 'сводная ведомость расхода' (stair sets), no building total"),
    ("ODI-119", "NOT_SCALAR", "universal cabins per room in tables"), ("ODI-120", "NOT_SCALAR", "prose"),
    ("POD-090", "ABSENT", "no statement"), ("PPM-104", "ABSENT", "no statement"), ("PPM-105", "ABSENT", "no statement"),
    ("PPM-107", "NOT_SCALAR", "КМ classes per room / cable"), ("PPM-108", "NOT_SCALAR", "prose"), ("PPM-110", "NOT_SCALAR", "prose"),
    ("SPZU-029", "ABSENT", "МАФ mentioned only as a materials requirement"), ("SPZU-037", "NOT_SCALAR", "251 prose hits, count stated as 'уточнить проектом'"),
    ("SPZU-038", "NOT_SCALAR", "'27 машино-место для инвалидов' is a drawing legend"), ("ZU-125", "ABSENT", "'утеплитель 200 мм' is for a bridge planter, not walls"),
    ("ZU-128", "ABSENT", "no statement"), ("ZU-129", "NOT_SCALAR", "prose"),
):
    P("NOV", _c, _st, _why)

# ---------------------------------------------------------------- planned TYU attempts (dev) ----
for _c, _st, _why in (
    ("AR-046", "NOT_SCALAR", "window schedule by type"), ("AR-049", "NOT_SCALAR", "stair/porch guard heights in requirement text, several values"),
    ("AR-050", "NOT_SCALAR", "finishing per room type"), ("AR-051", "ABSENT", "КЕО appears only as a report title"), ("IOS2-071", "ABSENT", "no statement"),
    ("KR-060", "CONFLICT", "'пилоны сечением 1000мм, толщиной 250мм' (КР) vs 'пилоны 1000×200' (ПЗ)"), ("KR-067", "ABSENT", "no consumption table in text"),
    ("ODI-119", "NOT_SCALAR", "universal cabins per floor / room list"), ("ODI-120", "NOT_SCALAR", "prose"), ("POS-084", "NOT_SCALAR", "temporary road described in prose"),
    ("PPM-104", "ABSENT", "no statement"), ("PPM-107", "NOT_SCALAR", "КМ0 per element"), ("PPM-108", "NOT_SCALAR", "detector lists"), ("PPM-110", "NOT_SCALAR", "prose"),
    ("PPM-113", "ABSENT", "no jet/flow pair"), ("PZ-011", "NOT_APPLICABLE", "school, no apartments"), ("SPZU-029", "NOT_SCALAR", "'парковые МАФ' legend text"),
    ("SPZU-033", "NOT_SCALAR", "'уклон проездов ... не превышает 6° (10.5%)' is a fire-equipment requirement"), ("SPZU-036", "NOT_SCALAR", "fence legend 'ограждение территории'"),
    ("SPZU-037", "ABSENT", "'проектом не предусматриваются парковочные места на территории'"), ("ZU-128", "ABSENT", "no statement"), ("ZU-129", "NOT_SCALAR", "prose"),
):
    P("TYU", _c, _st, _why)
