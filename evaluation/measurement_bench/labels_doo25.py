"""Labels: DOO25 (kindergarten, Полярная 25 корп. 9).  Holdout (seen in SILVER
pass 1).  Three PD generations (2022, Корр1 2025, Корр2 2025) and two RD
generations (АР2 / АР2 Изм2): truth = the 2022 ПЗ/ПЗУ figures that the RD
ГП2/ПЗУ изм.1 restate; the Корр1 figure for застройка (1391.6) is `sup`.
Only ~28% of DOO25 PDFs have a text layer -- the corpus is text-layer only."""
from __future__ import annotations

from .corpus_labels import P, V

_D = "DOO25"
V(_D, "PZ-001", "PD", "1331.4", 791, 12, "3 Площадь застройки ДОО м2 1 331,4", alt=((792, 8, "1331,4"), (817, 24, "1331,4")),
  sup=((836, 17, "1 391,6", "Корр1 generation"), (874, 15, "1 391,6", "Корр1 generation")), prov="TEP_EXT_PASS1")
V(_D, "PZ-001", "RD", "1331.4", 944, 5, "площадь застройки корп . 9 м2 1331,4", alt=((946, 9, "1331,4"),), prov="TEP_EXT_PASS1")
V(_D, "PZ-002", "PD", "4363.6", 791, 12, "4 Общая площадь здания, в том числе: м2 4 363,6", alt=((827, 3, "4 363,6"),), prov="TEP_CORE")
V(_D, "PZ-004", "PD", "18740.7", 791, 12, "9 Строительный объём здания, м3 18 740,7", alt=((827, 3, "18 740,7"),), prov="TEP_CORE")
V(_D, "PZ-005", "PD", "4117.3", 791, 12, "- подземная часть м3 4 117,3", alt=((836, 17, "4 117,3"),), prov="TEP_CORE")
V(_D, "PZ-019", "PD", "22.6", 792, 8, "Процент застройки = , ∗100 = 22,6%", prov="TEP_CORE", conf="MEDIUM", note="formula line 'Процент застройки = ... = 22,6%'")
V(_D, "PZ-019", "RD", "22.6", 946, 8, "Процент застройки = , ∗100 = 22,6%", alt=((946, 8, "22,6"),), prov="TEP_CORE", conf="MEDIUM",
  note="table row 'Процент застройки 40 | 22,6' has the GPZU limit 40 next to the design value")
V(_D, "SPZU-027", "PD", "1840.6", 792, 8, "5 Площадь озеленения м2 1840,6", alt=((817, 24, "1840,6"),),
  note="PD 1840.6 vs RD 1826.6 (-0.76%) -- both real, a genuine cross-stage difference in the source documents")
V(_D, "SPZU-027", "RD", "1826.6", 946, 9, "5 Площадь озеленения, в т.ч.: м2 1826,6 30,2", alt=((944, 5, "1826,6"),))
V(_D, "SPZU-025", "RD", "40", 946, 9, "площадка из асфальтобетона м2 40", conf="MEDIUM", note="one row of the hard-surface breakdown", prov="SPZU_CORE")
V(_D, "SPZU-026", "RD", "569", 946, 9, "пешеходные тротуары и площадки с покрытием из тротуарной плитки м2 569", conf="MEDIUM",
  note="label wraps onto two lines, value on the first; other tile rows exist (проезды из плитки 1056, отмостка 38)", prov="SPZU_CORE")
V(_D, "PZ-008", "RD", "14.2", 946, 8, "Предельная высота - 14,2", conf="MEDIUM", prov="TEP_CORE")
V(_D, "PZ-022", "PD", "II", 793, 7, "Степень огнестойкости - II", alt=((875, 8, "II"),))
V(_D, "PZ-022", "RD", "II", 898, 4, "-степень огнестойкости - II;", alt=((1734, 5, "II"),))
V(_D, "PZ-023", "PD", "C0", 793, 7, "Класс конструктивной пожарной опасности - С0", alt=((840, 21, "С0"), (875, 8, "С0")))
V(_D, "PZ-023", "RD", "C0", 898, 4, "-класс конструктивной пожарной опасности здания - С0", alt=((1734, 5, "С0"),))
V(_D, "KR-057", "PD", "A500C", 794, 15, "рабочей продольной арматурой класса А500С по ГОСТ 34028-2016, поперечной и соединительной", alt=((794, 14, "А500С"),), conf="MEDIUM",
  note="transverse/connecting bars are А240 (same sentence)")
V(_D, "KR-056", "RD", "C245", 885, 34, "c245 гост27772-2015", conf="MEDIUM", note="one steel grade among C235/Ст3кп/С245 across sets")
V(_D, "KR-059", "PD", "220", 794, 16, "с учётом толщины плиты перекрытия 220 мм и 240 мм", any_of=((794, 16, "240", "покрытие"),), conf="MEDIUM", prov="KR_CORE")
P(_D, "SPZU-024", "NOT_SCALAR", "earth-mass sheet has per-square rows; 'выемка 0 0 0 ...' placeholders; no total row read")
P(_D, "SPZU-030", "ABSENT", "no road-width statement in PD/RD text")
P(_D, "KR-054", "ABSENT", "no grid-step statement")
P(_D, "SPZU-028", "NOT_SCALAR", "RD ПЗУ row layout ambiguous ('523' and '386' near the label); PD ПЗП area value in a different table")
P(_D, "KR-064", "ABSENT", "no lift shaft dimensions in text")
P(_D, "IOS2-071", "ABSENT", "no riser diameters")
P(_D, "AR-041", "NOT_SCALAR", "multi-instance (doors)")
P(_D, "IOS1-069", "NOT_SCALAR", "cable schedules")
P(_D, "ZU-125", "NOT_SCALAR", "multiple insulation layers 40/100/160 мм")
P(_D, "PPM-104", "ABSENT", "no statement")
P(_D, "PZ-010", "NOT_APPLICABLE", "kindergarten, no apartments")
P(_D, "PZ-011", "NOT_APPLICABLE", "kindergarten, no apartments")
P(_D, "PZ-012", "NOT_SCALAR", "'многоуровневая стоянка на 300 машиномест' is ANOTHER building (гр3/гр4), not this building's underground parking")
P(_D, "SPZU-037", "NOT_SCALAR", "300 м/м belongs to neighbouring multi-level car park groups")
P(_D, "SPZU-038", "NOT_SCALAR", "prose 'предусмотрены места для инвалидов'")
P(_D, "PPM-110", "NOT_SCALAR", "prose")
P(_D, "ODI-120", "NOT_SCALAR", "prose")
P(_D, "ZU-129", "NOT_SCALAR", "prose")
P(_D, "PPM-107", "NOT_SCALAR", "КМ0..КМ2 by finishing")
P(_D, "AR-050", "NOT_SCALAR", "finishing per room")
P(_D, "PZ-015", "ABSENT", "not searched beyond plan probe (no distinct statement)")
P(_D, "PZ-021", "ABSENT", "no class letter in text-layer probe")
P(_D, "ZU-124", "ABSENT", "no class letter in text-layer probe")
P(_D, "IOS5-080", "NOT_SCALAR", "prose")
P(_D, "AR-046", "NOT_SCALAR", "window schedule per type")
P(_D, "SPZU-029", "NOT_SCALAR", "МАФ text only in this probe")
P(_D, "AR-045", "NOT_SCALAR", "roof slope per area")
P(_D, "ODI-119", "NOT_SCALAR", "prose")
P(_D, "IOS4-077", "NOT_SCALAR", "prose")
