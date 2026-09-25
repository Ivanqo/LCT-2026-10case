"""Labels: LOS3A (Лосевская 3А) -- DEV object (live-tagger bugs and the 9.1
forensic fixes PZ-001/002/004 were developed against it).  Rows re-used from the
blind SILVER pass-1 verdict rows (values + pages independently read from the
PDFs via probe.py) plus fresh reads for the planned attempts."""
from __future__ import annotations

from .corpus_labels import P, V

_L = "LOS3A"
# ---- TEP table (PD ПЗ 000003 p14-15, RD АР2 000069 p11): blind pass-1 rows -----
V(_L, "PZ-001", "PD", "1076.49", 3, 14, "Площадь застройки (по СП 54.13330.2022 прил. А.1.1. с учетом выступающих частей...) 1076,49 м2",
  sup=((3, 14, "1061.49", "same table, footprint WITHOUT balconies"),), prov="TEP_EXT_PASS1")
V(_L, "PZ-001", "RD", "1076.49", 69, 11, "Площадь застройки (по СП 54.13330.2022 прил. А.1.1. с учетом выступающих частей балконов) м² 1076,49", prov="TEP_EXT_PASS1")
V(_L, "PZ-002", "PD", "25036.27", 3, 14, "Площадь здания (по СП 54.13330.2016, прил. А.1.2) 25036,27 м2",
  sup=((3, 15, "16867.90", "'Площадь помещений здания' (СП 118.13330.2012 Г.5): a DIFFERENT quantity"),))
V(_L, "PZ-002", "RD", "25036.27", 69, 11, "Площадь здания м² 25036,27", alt=((70, 10, "25036,27"),))
V(_L, "PZ-004", "PD", "88264.00", 3, 14, "Строительный объем в т.ч. 88264,00 м3")
V(_L, "PZ-004", "RD", "88264.00", 69, 11, "Строительный объем в т.ч. м³ 88264,00")
V(_L, "PZ-005", "PD", "13962.18", 3, 14, "ниже отм. 0.000 13962,18 м3", note="sub-row of the 'Строительный объем' row")
V(_L, "PZ-005", "RD", "13962.18", 69, 11, "Ниже отм. 0.000 м³ 13962,18")
V(_L, "PZ-006", "PD", "74301.82", 3, 14, "выше отм. 0.000 74301,82 м3", note="sub-row of the 'Строительный объем' row")
V(_L, "PZ-006", "RD", "74301.82", 69, 11, "Выше отм. 0.000 м³ 74301,82")
V(_L, "PZ-008", "PD", "82.8", 3, 14, "Высота + 82,8 м", alt=((7, 7, "82,8"),), conf="MEDIUM",
  note="RD ТЭП states 83,54 for the same top mark (pass-1 verdict: same physical mark, different datum) -- each stage's stated value is recorded as printed")
V(_L, "PZ-008", "RD", "83.54", 69, 11, "Высота объекта м 83,54", conf="MEDIUM")
V(_L, "PZ-010", "PD", "231", 3, 15, "Количество квартир, в т.ч.: 231", prov="TEP_EXT_PASS1")
V(_L, "PZ-010", "RD", "231", 69, 11, "Количество квартир, в т.ч.: Шт. 231", prov="TEP_EXT_PASS1")
V(_L, "PZ-012", "PD", "72", 3, 15, "30 Кол-во машиномест в подземной автостоянке 72 шт.", prov="TEP_EXT_PASS1")
V(_L, "PZ-012", "RD", "72", 69, 11, "15 Кол-во машиномест в подземной автостоянке шт. 72", alt=((72, 10, "72"), (74, 6, "72")), prov="TEP_EXT_PASS1")
V(_L, "PZ-019", "PD", "21.03", 7, 8, "5 Процент застройки % 21,03", alt=((7, 18, "21.03"), (43, 69, "21,03")), prov="TEP_CORE")
P(_L, "PZ-019", "ABSENT", "RD has no coverage-percent row", stages="RD", prov="TEP_CORE")
V(_L, "SPZU-027", "PD", "1283.06", 7, 8, "4 Площадь озеленения м2 1283,06 62,55", alt=((43, 69, "1283,06"),), conf="MEDIUM",
  note="second number 62,55 is the 'additional landscaping' column", prov="SPZU_CORE")
P(_L, "SPZU-027", "ABSENT", "RD ГП (000050) shows only per-row gazon/planting quantities (62,55 м2 ...), no total", stages="RD", prov="SPZU_CORE")
# ---- enum ----
V(_L, "PZ-022", "PD", "I", 3, 17, "Степень огнестойкости I (первая)", alt=((9, 8, "I"), (47, 129, "I")),
  note="lightning-protection boilerplate 'Степень огнестойкости здания - II' in ИОС1.1 (PD) and МЗ (RD) is a different, copied statement (pass 1)")
V(_L, "PZ-022", "RD", "I", 69, 11, "Объект проектирования относится к 1 степени огнестойкости", alt=((72, 10, "1"),))
V(_L, "PZ-023", "PD", "C0", 3, 18, "Класс конструктивной пожарной опасности – С0", alt=((9, 8, "С0"), (47, 129, "C0")))
V(_L, "PZ-023", "RD", "СО", 69, 11, "Класс конструктивной пожарной опасности - СО", alt=((72, 10, "СО"),), note="print uses Cyrillic 'О' (letter) instead of the digit 0")
V(_L, "PZ-021", "PD", "B", 3, 19, "класс энергоэффективности «В».", any_of=((5, 30, "А", "ЭЭ section says class А"),), conf="MEDIUM",
  note="PD internally inconsistent: ПЗ 'В' vs ЭЭ 'А'")
V(_L, "ZU-124", "PD", "A", 5, 30, "класс энергосбережения здания - А согласно таблице №15 СП", alt=((5, 39, "А"),), any_of=((3, 19, "В", "ПЗ section says class В"),), conf="MEDIUM",
  note="PD internally inconsistent: ЭЭ 'А' vs ПЗ 'В'")
V(_L, "KR-057", "PD", "A500C", 10, 18, "плитный фундамент толщина 1600 мм В25, W6, F150 класса A500С", any_of=((10, 18, "А240", "secondary"),), conf="MEDIUM")
V(_L, "KR-057", "RD", "A500C", 51, 5, "10.Арматуру применять класса А500С, А240 по ГОСТ", any_of=((51, 5, "А240", "secondary"),), conf="MEDIUM")
V(_L, "KR-056", "PD", "C255", 10, 28, "(спаренный двутавр 35Б2 по ГОСТ Р 57837-2017, сталь С255 ГОСТ 27772-2015)", conf="MEDIUM")
V(_L, "KR-056", "RD", "C255", 60, 3, "3.1 Марку стали применять С255 по", any_of=((53, 6, "С245", "КЖ table 'А240 А500С С245'"),), conf="MEDIUM")
V(_L, "PZ-015", "PD", "II", 11, 6, "электроприемники объекта в целом относятся к потребителям 2-й категории электроснабжения", conf="MEDIUM",
  note="printed '2-й' / 'второй' = II; fire equipment and elevators are category I in the same sections")
P(_L, "PZ-015", "NOT_SCALAR", "RD: category I for individual telecom/fire loads only", stages="RD")
# ---- table count / earth mass ----
V(_L, "SPZU-024", "PD", "1491", 7, 22, "Итого перерабатываемого грунта 393 | 1491 (site); parking 2088 | 2088", any_of=((7, 22, "393", "site насыпь"), (7, 22, "2088", "parking")),
  prov="PASS1", conf="MEDIUM", note="pass-1: main total = site excavation (выемка)")
V(_L, "SPZU-024", "RD", "1350", 50, 13, "Итого перерабатываемого грунта 380 | 1350 (site); parking 1993 | 1993", any_of=((50, 13, "380", "site насыпь"), (50, 13, "1993", "parking"), (50, 13, "144", "extra landscaping")),
  prov="PASS1", conf="MEDIUM")
P(_L, "PZ-011", "NOT_SCALAR", "apartment mix 1к 44 / 2к 150 / 3к 37 -- a triple, not a row count", stages="PD,RD", prov="TEP_EXT_PASS1")
P(_L, "PZ-003", "NOT_SCALAR", "not searched")
P(_L, "SPZU-037", "NOT_SCALAR", "PD: 'требуется 114 м/мест, из них 94 постоянного хранения' -- several counts")
P(_L, "SPZU-038", "NOT_SCALAR", "'5 м/мест для МГН' is a sub-group of a computed 114")
P(_L, "PPM-110", "NOT_SCALAR", "prose / voice announcer text")
P(_L, "ZU-129", "NOT_SCALAR", "prose")
P(_L, "ODI-120", "NOT_SCALAR", "prose")
P(_L, "ODI-116", "NOT_SCALAR", "room tables (corridor areas), no width scalar")
P(_L, "KR-060", "NOT_SCALAR", "columns 450х450, 350х850, 500х500")
P(_L, "POS-083", "NOT_SCALAR", "prose")
P(_L, "IOS3-074", "ABSENT", "no sewer diameters stated as a scalar")
P(_L, "KR-064", "ABSENT", "no lift-shaft dimensions in text")
P(_L, "AR-041", "NOT_SCALAR", "multi-instance parameter (pass 1 NEEDS_REVIEW)")
P(_L, "IOS2-071", "NOT_SCALAR", "RD revision remarks 'изменен диаметр стояка Т32-1 с 65 на 50' -- per-riser")
P(_L, "SPZU-036", "NOT_SCALAR", "several heights (1,2 ОДИ; 2,0 construction fence; 0,9 stair guard)")
P(_L, "POD-097", "ABSENT", "no statement")
P(_L, "PPM-107", "NOT_SCALAR", "КМ0/КМ1 by room type")
P(_L, "AR-050", "NOT_SCALAR", "finishing per room type")
P(_L, "IOS5-080", "NOT_SCALAR", "prose")
P(_L, "POS-088", "ABSENT", "no capacities of temporary utilities")
P(_L, "IOS1-070", "NOT_SCALAR", "prose")
P(_L, "ODI-121", "NOT_SCALAR", "'5 м/мест для МГН' without dimensions pair")
P(_L, "IOS4-077", "NOT_SCALAR", "radiator schedule per room")
P(_L, "KR-067", "NOT_SCALAR", "pass 1: per-set steel schedules only, no PD baseline")
P(_L, "PZ-020", "ABSENT", "'плотность застройки ... 0,25' is a different quantity/unit")
