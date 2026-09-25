"""Labels: the two PUBLIC objects (Novoslobodskaya = NOV, Tyumenskaya = TYU).
DEV objects: every checkpoint 28-42 was developed against them.  File ids are
the organiser's F-ids (participant manifest).  Tyumenskaya has no real RD (only
the mixed RD/ID ОВ/ИТП sets); Novoslobodskaya's RD is КЖ-only, so most triples
are PD-stage.  Public gold (15 rows) is NOT used as truth here -- values are
re-read from the PDFs."""
from __future__ import annotations

from .corpus_labels import P, V

# ================================================================= NOV
_N = "NOV"
V(_N, "PZ-001", "PD", "1225.8", "F0101", 9, "2 Площадь застройки, в т.ч.: кв. м 1225,8", prov="TEP_CORE")
V(_N, "PZ-002", "PD", "17140.2", "F0101", 9, "3 Площадь жилого здания, в т. ч.: кв. м - 17 140,2", conf="MEDIUM", prov="TEP_CORE",
  note="ТЭП label is 'Площадь жилого здания' (no row called 'Общая площадь здания')")
V(_N, "PZ-004", "PD", "69201.0", "F0101", 9, "9 Строительный объем, в т.ч.: кв. м. 69 201,0", prov="TEP_CORE")
V(_N, "PZ-005", "PD", "16454.6", "F0101", 9, "подземная часть, в т.ч.: куб. м 16 454,6", prov="TEP_CORE")
V(_N, "PZ-006", "PD", "52731.4", "F0101", 9, "наземная часть, в т.ч.: куб. м 52 731,4", prov="TEP_CORE")
V(_N, "PZ-008", "PD", "74.5", "F0101", 9, "6 Высота объекта капитального строительства м 74,5 74,5", alt=(("F0102", 9, "74,5"),),
  sup=(("F0101", 9, "233.7", "row 5 'абсолютная высота' is an absolute mark, not the height"),), prov="TEP_CORE")
V(_N, "PZ-010", "PD", "92", "F0101", 10, "12 Количество квартир, в т.ч.: 16 47 21 8 92", prov="TEP_CORE",
  note="'Всего' column of the by-type row; table lists 1-,2-,3-,4-room counts then the total")
V(_N, "PZ-012", "PD", "64", "F0101", 10, "14 Количество машино-мест в подземной автостоянке м/м 64", prov="TEP_CORE")
V(_N, "PZ-022", "PD", "II", "F0101", 12, "Комплекс запроектирован II степени огнестойкости С0 класса конструктивной пожарной опасности.", alt=(("F0106", 16, "II"), ("F0106", 57, "II")),
  sup=(("F0102", 85, "I", "built-in car park is degree I"),))
V(_N, "PZ-022", "RD", "I", "F0140", 3, "соответствующих l степени огнестойкости и С0 конструктивной пожарной опасности", alt=(("F0141", 3, "I"),), conf="MEDIUM",
  note="RD KЖ sets read 'I' (OCR-like letter l) while PD says II -- a real PD/RD wording difference")
V(_N, "PZ-023", "PD", "C0", "F0101", 12, "Комплекс запроектирован II степени огнестойкости С0 класса конструктивной пожарной опасности.", alt=(("F0102", 86, "С0"),))
V(_N, "PZ-023", "RD", "C0", "F0140", 3, "соответствующих l степени огнестойкости и С0 конструктивной пожарной опасности")
V(_N, "KR-057", "PD", "A500C", "F0105", 14, "основная рабочая арматура А500С, конструктивная А240 по ГОСТ 34028-2016.", any_of=(("F0105", 13, "А400", "earlier passage of the same КР1 says А400"),),
  conf="MEDIUM", note="F0105 p13 says 'основная рабочая арматура А400' (design of the pit shoring), p14 А500С")
V(_N, "KR-057", "RD", "A500C", "F0140", 3, "1 освидетельствование грунтов основания элемента А500С А500С С245 С245 итого", conf="MEDIUM")
V(_N, "KR-056", "PD", "C245", "F0105", 14, "(сталь класса C245 ГОСТ 27772-2015)", alt=(("F0105", 25, "C245"),))
V(_N, "KR-056", "RD", "C245", "F0140", 6, "мн-1 С245 ГОСТ 27772-2015 27.2", alt=(("F0140", 3, "С245"),))
V(_N, "KR-059", "PD", "200", "F0106", 52, "Перекрытия надземных этажей корпуса (кроме 16 этажа) - 200мм", any_of=(("F0106", 53, "1000", "foundation slab (different element)"),),
  conf="MEDIUM", prov="KR_CORE")
V(_N, "KR-059", "RD", "250", "F0142", 4, "t=250 - толщина плиты перекрытия", conf="MEDIUM", prov="KR_CORE",
  note="RD sheet states 250 for its floor; PD says 200 for the same typical floors (a real PD/RD difference, not verified against a change note)")
V(_N, "KR-061", "PD", "200", "F0106", 52, "толщина стен в надземной части корпусов составляет 200, 250, 300, 350мм.", any_of=(("F0106", 52, "250", ""), ("F0106", 52, "300", ""), ("F0106", 52, "350", "")),
  conf="MEDIUM", prov="KR_CORE")
V(_N, "SPZU-027", "PD", "153.9", "F0126", 10, "3.2 площадь озеленения, в т.ч.: 153,9 11,0", conf="MEDIUM", note="second number 11,0 is a share/other column", prov="SPZU_CORE")
P(_N, "PZ-019", "ABSENT", "no coverage-percent row")
P(_N, "PZ-020", "ABSENT", "'плотность застройки тыс.кв.м/га 99,98' is a different quantity")
P(_N, "PZ-003", "NOT_SCALAR", "'минимальная расчетная площадь' are norm values for rooms")
P(_N, "PZ-021", "ABSENT", "template blank '_______'")
P(_N, "ZU-124", "ABSENT", "no class letter")
P(_N, "PZ-015", "NOT_SCALAR", "'категория ... насосных станций II' only")
P(_N, "SPZU-025", "NOT_SCALAR", "'на объекте отсутствуют площадки с асфальтобетонным покрытием'")
P(_N, "SPZU-026", "NOT_SCALAR", "no total tile area")
P(_N, "PZ-011", "NOT_SCALAR", "mix 16/47/21/8", stages="PD")

# ================================================================= TYU
_T = "TYU"
V(_T, "PZ-001", "PD", "4650.91", "F0150", 26, "2. Площадь застройки м ² 4650,91", any_of=(("F0154", 13, "4629.70", "ПЗУ table states 4629,70"),), conf="MEDIUM", prov="TEP_CORE",
  note="ПЗ (F0150) 4650,91 vs ПЗУ (F0154/F0155) 4629,70 -- two PD figures")
V(_T, "PZ-002", "PD", "11618.27", "F0150", 26, "Общая площадь здания, в т.ч.: м ² 11618,27", prov="TEP_CORE")
V(_T, "PZ-002", "RD", "11030.3", "F0201", 14, "общая площадь здания s=11030,3 кв.м;", alt=(("F0202", 14, "11030,3"),), conf="MEDIUM", prov="TEP_CORE",
  note="RD ОВ general data restates 11030,3 vs ПЗ 11618,27 (real cross-stage difference, RD is a mixed RD/ID set)")
V(_T, "PZ-003", "PD", "8010.1", "F0150", 26, "9. Расчётная площадь здания м ² 8010,1", any_of=(("F0152", 52, "8526.1", "ЭЭ: расчетная площадь общественных помещений"),), conf="MEDIUM", prov="TEP_CORE")
V(_T, "PZ-004", "PD", "60997.93", "F0150", 26, "Строительный объём здания, в т.ч.: м ³ 60997,93", sup=(("F0152", 52, "66623.14", "ЭЭ строительный объем"),), prov="TEP_CORE")
V(_T, "PZ-004", "RD", "60997.93", "F0201", 14, "строительный объем здания v=60997,93 куб.м;", alt=(("F0202", 14, "60997,93"),), prov="TEP_CORE")
V(_T, "PZ-005", "PD", "10066.8", "F0150", 26, "11. - подземная часть 10066,8", prov="TEP_CORE",
  sup=(("F0150", 26, "504.0", "'ниже отм. 0,000 504,0' is an AREA row of 'Общая площадь здания'"),))
V(_T, "PZ-006", "PD", "50931.13", "F0150", 26, "- надземная часть 50931,13", prov="TEP_CORE",
  sup=(("F0150", 26, "11114.27", "'выше отм. 0,000 11114,27' is an AREA row"),))
V(_T, "PZ-008", "PD", "15.814", "F0150", 26, "Высота здания м 15,814", any_of=(("F0154", 11, "16.1", "ПЗУ 'предельная высота 35 | 16,1'"),), conf="MEDIUM", prov="TEP_CORE")
V(_T, "SPZU-027", "PD", "3158.02", "F0154", 13, "4 площадь озеленения, в том числе: м2 3158,02", alt=(("F0155", 13, "3158,02"),), prov="SPZU_CORE")
V(_T, "SPZU-025", "PD", "1270.80", "F0154", 13, "3.1 площадь асфальтобетонного покрытия проездов м2 1270,80", any_of=(("F0154", 13, "355.23", "отмостка"),), conf="MEDIUM", prov="SPZU_CORE")
V(_T, "PZ-022", "PD", "I", "F0148", 68, "Объект предусмотрен I степени огнестойкости. Класс конструктивной пожарной опасности Объекта С0.", alt=(("F0148", 70, "I"),))
V(_T, "PZ-023", "PD", "C0", "F0148", 68, "Класс конструктивной пожарной опасности Объекта С0.", alt=(("F0148", 372, "С0"), ("F0154", 19, "С0")))
V(_T, "KR-057", "PD", "A500C", "F0158", 19, "армирование железобетонных конструкций выполняется из арматуры А500С по ГОСТ Р 52544-2006 и А240 по ГОСТ 5781-82.",
  any_of=(("F0158", 19, "А240", "secondary"),), conf="MEDIUM")
V(_T, "KR-056", "PD", "C345", "F0158", 20, "верхние пояса ферм – труба 240х120х9 по ГОСТ 32931-2015 из стали С345", any_of=(("F0158", 19, "С245", "лестницы"),), conf="MEDIUM")
V(_T, "ZU-124", "PD", "A", "F0152", 66, "31 Класс энергосбережения А", conf="MEDIUM", note="F0152 p51 also states a requirement 'не ниже «С»'")
P(_T, "PZ-019", "ABSENT", "no coverage-percent row")
P(_T, "PZ-020", "ABSENT", "'плотность застройки 9,646 тыс.кв.м/га' is a different quantity")
P(_T, "PZ-010", "NOT_APPLICABLE", "school, no apartments")
P(_T, "PZ-012", "NOT_APPLICABLE", "no underground parking ('парковочные места не предусматриваются')")
P(_T, "PZ-021", "ABSENT", "template blank '_______'")
P(_T, "PZ-015", "NOT_SCALAR", "'ко II категории надежности' for one connection facility")
P(_T, "KR-059", "ABSENT", "no slab thickness statement")
P(_T, "KR-061", "NOT_SCALAR", "steel-pipe wall thickness hits only")
P(_T, "SPZU-026", "NOT_SCALAR", "no tile-area row found (only the road-code text)")
