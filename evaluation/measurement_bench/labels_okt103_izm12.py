"""Labels: OKT103 and IZM12 (the two CLEAN holdout objects).  See corpus_labels.py
for the vocabulary; read from the original PDFs before any extractor run."""
from __future__ import annotations

from .corpus_labels import P, V

# =================================================================== OKT103
# Clean holdout: RD folder holds ONLY КЖ drawing sets (no АР/ГП/ТЭП), PD has the
# full ПЗ TEP table (23.009-ПЗ p14-15, duplicated in ПЗ_корр.ГИП).  The pipeline
# produced 0 comparable groups here in pass 1 -> exactly the "system is silent"
# case the corpus must contain.
_O = "OKT103"
V(_O, "PZ-001", "PD", "3326.0", 149, 14, "3 Площадь застройки, в том числе: м2 3 326,0", alt=((150, 14, "3 326,0"), (148, 16, "3326")), prov="TEP_CORE")
P(_O, "PZ-001", "ABSENT", "RD is КЖ only; no TEP", stages="RD", prov="TEP_CORE")
V(_O, "PZ-002", "PD", "16605.70", 149, 15, "8 Общая площадь здания, в том числе: м2 16 605,70", alt=((150, 15, "16 605,70"), (149, 116, "16605.7")))
P(_O, "PZ-002", "ABSENT", "RD is КЖ only", stages="RD")
V(_O, "PZ-004", "PD", "73080.00", 149, 14, "5 Строительный объем, в том числе: м3 73 080,00", alt=((150, 14, "73 080,00"),), prov="TEP_CORE")
V(_O, "PZ-005", "PD", "13300", 149, 14, "5.2 - подземная часть м3 13 300", alt=((150, 14, "13 300"),), prov="TEP_CORE")
V(_O, "PZ-006", "PD", "59780", 149, 14, "5.1 - наземная часть м3 59 780", alt=((150, 14, "59 780"),))
V(_O, "PZ-008", "PD", "63.96", 149, 14, "6.3 Предельная высота здания м 63,96 66,0", alt=((138, 7, "63,96"),), prov="TEP_CORE",
  sup=((149, 14, "66,0", "GPZU limit column, not the design value"),), note="table has two columns: design 63,96 | ГПЗУ 66,0")
V(_O, "PZ-010", "PD", "135", 149, 15, "12 Количество квартир, в том числе: шт. 135", alt=((150, 15, "135"), (159, 32, "135")))
V(_O, "PZ-012", "PD", "64", 138, 7, "автостоянка рассчитана на 64 машино-места постоянного хранения легковых автомобилей жителей", alt=((158, 12, "64"),), conf="MEDIUM",
  note="underground car park (-1 floor); TEP table has no separate row")
V(_O, "ZU-124", "PD", "A", 159, 36, "31 Класс энергосбережения А", note="ЭЭ section")
V(_O, "PZ-021", "PD", "A", 149, 23, "класса энергетической эффективности не ниже «А» (в соответствии с приказом Минстроя", conf="MEDIUM",
  note="'не ниже А' -- a requirement statement; ЭЭ says class A")
V(_O, "PZ-022", "PD", "I", 149, 6, "- Жилые секции (Корпуса 1 и 2) высотой более 50 м (не более 75 м) – I (первая);", alt=((138, 5, "I"), (142, 32, "I")),
  note="underground car park also I")
P(_O, "PZ-022", "ABSENT", "RD is КЖ only", stages="RD")
V(_O, "PZ-023", "PD", "C0", 149, 6, "Класс конструктивной пожарной опасности - С0.", alt=((138, 5, "С0"), (142, 16, "С0")))
P(_O, "PZ-023", "ABSENT", "RD is КЖ only", stages="RD")
V(_O, "KR-057", "PD", "A500C", 142, 17, "материал стен - бетон В30; арматура А500С, А240 по ГОСТ 34028-2016.", any_of=((142, 17, "А240", "secondary/transverse"),), conf="MEDIUM",
  note="two classes listed together; А500С is the working reinforcement")
V(_O, "KR-057", "RD", "A500C", 166, 2, "арматурную сталь класса А500С, А240 принять по ГОСТ 34028-2016.", any_of=((166, 2, "А240", "secondary"),), conf="MEDIUM")
V(_O, "KR-056", "PD", "C255", 142, 19, "выполнена из стали С255 по ГОСТ 27772.", any_of=((142, 19, "С245", "I14Б2 beams"),), conf="MEDIUM")
V(_O, "KR-056", "RD", "C245", 166, 2, "конструкции А500С А500С С245", conf="MEDIUM")
V(_O, "KR-059", "PD", "180", 142, 16, "Толщины плит типовых этажей 180 мм.", alt=((143, 10, "180"),), prov="KR_CORE")
V(_O, "KR-059", "RD", "180", 167, 3, "t=180 - толщина плиты перекрытия", alt=((167, 4, "180"),), prov="KR_CORE")
V(_O, "KR-061", "PD", "180", 142, 18, "Толщина стен 180мм, 200 мм.", any_of=((142, 18, "200", "second thickness"),), conf="MEDIUM", prov="KR_CORE")
V(_O, "SPZU-027", "PD", "192", 148, 16, "6 Площадь озеленения*. м2 192", any_of=((148, 16, "393.7", "roof-garden over parking"),), conf="MEDIUM",
  note="ground greening 192 vs roof 393,7")
V(_O, "AR-049", "PD", "1.2", 147, 11, "- высота ограждения не менее 1,2 м.", alt=((138, 9, "1,2"),), conf="MEDIUM", note="minimum required guard height, stated design value")
P(_O, "SPZU-025", "NOT_SCALAR", "asphalt named in surface list, no area")
P(_O, "SPZU-028", "ABSENT", "no playground area statement")
P(_O, "ZU-128", "NOT_SCALAR", "insulation layers listed with density, thickness in drawings only")
P(_O, "POD-097", "ABSENT", "no scrap-storage area")
P(_O, "KR-054", "ABSENT", "no grid step statement")
P(_O, "POS-084", "NOT_SCALAR", "'временные дороги и проезды из плит - 501 м2' is an area")
P(_O, "ODI-117", "NOT_SCALAR", "'не менее 0,9 м' for a lift cabin door only")
P(_O, "PZ-019", "ABSENT", "'максимальный процент застройки – без ограничений'")
P(_O, "PPM-110", "NOT_SCALAR", "prose")
P(_O, "ODI-120", "NOT_SCALAR", "prose")
P(_O, "ZU-129", "NOT_SCALAR", "prose")
P(_O, "SPZU-037", "NOT_SCALAR", "9 guest м/м vs 64 resident spaces in different sentences")
P(_O, "SPZU-038", "NOT_SCALAR", "'9 м/м (в том числе 1 для МГН М4)' -- count of a sub-group")
P(_O, "PZ-011", "NOT_SCALAR", "apartment mix table, not a scalar")
P(_O, "PZ-015", "ABSENT", "no building category statement")
P(_O, "PPM-107", "ABSENT", "no finishing-class statement")
P(_O, "AR-050", "NOT_SCALAR", "finishing per room type")
P(_O, "IOS1-070", "NOT_SCALAR", "prose")
P(_O, "IOS4-077", "NOT_SCALAR", "prose")
P(_O, "PPM-112", "NOT_SCALAR", "prose")
P(_O, "IOS2-073", "NOT_SCALAR", "prose")
P(_O, "KR-067", "NOT_SCALAR", "steel consumption per drawing set only")
P(_O, "PPM-113", "NOT_SCALAR", "'внутреннее пожаротушение 60 л/с' has no jet count pair")

# ==================================================================== IZM12
# Clean holdout: RD = 9 КЖ sets whose pages are ~98% image-only (no text layer),
# so every RD triple is out of reach for a text-layer reader AND for the
# pipeline's text extractors; recorded as NO_TEXT_LAYER, not silently dropped.
_I = "IZM12"
V(_I, "PZ-001", "PD", "819.6", 10, 10, "Площадь застройки м2 819,6", alt=((19, 41, "819,6"),), prov="TEP_CORE")
V(_I, "PZ-002", "PD", "15462.8", 10, 10, "Площадь жилого здания м2 15 462,8", alt=((10, 32, "15462.8"),), conf="MEDIUM",
  note="label 'площадь жилого здания'; ПЗ p32 calls it 'общая площадь объекта'")
V(_I, "PZ-004", "PD", "58465.4", 10, 10, "Строительный объем здания (общий) м3 58 465,4", alt=((19, 41, "58 465,4"),))
V(_I, "PZ-005", "PD", "9947.4", 10, 10, "В том числе подземная часть м3 9 947,4", alt=((19, 41, "9 947,4"),))
V(_I, "PZ-006", "PD", "48518.0", 10, 10, "В том числе надземная часть м3 48 518,0", prov="TEP_CORE")
V(_I, "PZ-008", "PD", "80.75", 10, 10, "Предельная высота (высота объекта) м 80,75", sup=((10, 10, "80,50", "верхняя отметка"),), prov="TEP_CORE")
V(_I, "PZ-010", "PD", "161", 10, 10, "Общее количество квартир 161", alt=((15, 54, "161"), (19, 41, "161")))
V(_I, "PZ-012", "PD", "43", 10, 10, "Количество машиномест в подземной стоянке 43")
V(_I, "PZ-022", "PD", "I", 10, 12, "степень огнестойкости здания – I.", alt=((18, 11, "I"), (24, 39, "I")))
V(_I, "PZ-023", "PD", "C0", 10, 12, "класс конструктивной пожарной опасности здания – С0.", alt=((24, 39, "СО"), (62, 12, "С0")))
V(_I, "KR-057", "PD", "A500C", 24, 19, "армированные стержнями арматуры А500C и А240. Толщина стен 200 мм.", any_of=((24, 19, "А240", "secondary"),), conf="MEDIUM")
V(_I, "KR-056", "PD", "C245", 24, 22, "устройство фахверков перегородок из стальных (сталь С245) профилей.", any_of=((24, 22, "С235", "стальные скобы"),), conf="MEDIUM")
V(_I, "KR-059", "PD", "200", 24, 21, "армированные стержнями арматуры А500C и А240. Толщина плит составляет 200 мм.", any_of=((24, 19, "180", "other slab zones"),), conf="MEDIUM",
  prov="KR_CORE")
V(_I, "KR-061", "PD", "200", 24, 19, "Толщина стен 200 мм.", any_of=((24, 35, "400", "outer walls of the underground part"),), conf="MEDIUM", prov="KR_CORE")
V(_I, "SPZU-027", "PD", "620", 20, 13, "площадь озеленения м2 620", alt=((58, 9, "620"),), any_of=((20, 13, "42.5", "second greening row"),), conf="MEDIUM")
for _c in ("PZ-001", "PZ-002", "PZ-004", "PZ-005", "PZ-006", "PZ-008", "PZ-010", "PZ-012", "PZ-022", "PZ-023", "KR-057", "KR-056", "KR-059", "KR-061", "SPZU-027"):
    P(_I, _c, "NO_TEXT_LAYER", "IZM12 RD: 9 КЖ sets, 320 pages, ~98% without a text layer -> RD value unreadable without OCR", stages="RD", prov="AUTO")
P(_I, "IOS1-069", "NOT_SCALAR", "cable schedule")
P(_I, "POD-090", "ABSENT", "no hazard-zone text")
P(_I, "KR-062", "ABSENT", "no statement")
P(_I, "POD-097", "ABSENT", "no statement")
P(_I, "KR-060", "NOT_SCALAR", "several column sections 200х900, 200х1200")
P(_I, "PZ-019", "ABSENT", "'установлена' placeholders in ГПЗУ table")
P(_I, "IOS3-074", "NOT_SCALAR", "'выпусками ДУ100-..' one of many")
P(_I, "ZU-129", "NOT_SCALAR", "prose")
P(_I, "ODI-120", "NOT_SCALAR", "prose")
P(_I, "PPM-110", "NOT_SCALAR", "prose")
P(_I, "PZ-011", "NOT_SCALAR", "mix 46 / 92 / 23", stages="PD")
P(_I, "SPZU-038", "NOT_SCALAR", "'4 м/мест для инвалидов-колясочников' in МОДИ, other sections differ")
P(_I, "SPZU-037", "NOT_SCALAR", "no scalar for surface parking")
P(_I, "PPM-107", "NOT_SCALAR", "КМ0..КМ5 by room")
P(_I, "AR-050", "NOT_SCALAR", "finishing per room type")
P(_I, "ZU-124", "ABSENT", "no class letter stated")
P(_I, "PZ-021", "ABSENT", "ПЗ p15 defers the class to the operation-stage надзор conclusion")
P(_I, "KR-067", "ABSENT", "no statement")
P(_I, "IOS5-080", "NOT_SCALAR", "prose")
P(_I, "SPZU-029", "NOT_SCALAR", "МАФ statement text only")
P(_I, "POS-088", "ABSENT", "no statement")
P(_I, "IOS1-070", "NOT_SCALAR", "prose")
P(_I, "PPM-113", "NOT_SCALAR", "prose")
