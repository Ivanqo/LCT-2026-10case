"""Labels: COMPOUND-family triples (tuple values, catalog unit order).  A search of
all 8 objects (pump Q/H/N, fan L/P, ВПВ jets, МАФ, window totals, material
consumption) found only a handful of unambiguous tuples: real documents rarely
state a compound parameter as one scalar tuple.  Small n is itself a finding."""
from __future__ import annotations

from .corpus_labels import P, V

V("POL17", "IOS2-073", "PD", ["10.02", "55.7", "2.2"], 39, 13, "с характеристиками: Q=10,02 м3/ч; H=55,7 м; N=2,2 кВт х 3",
  conf="MEDIUM", prov="COMPOUND_SEARCH",
  note="old-generation ИОС2.1 Корр.6 (2 working + 1 reserve pumps); unit order м³/ч / м / кВт")
V("POL17", "IOS2-073", "RD", ["16", "16.0", "2.2"], 103, 27, "Q =16 м³/ч, напор Н=16,0 м), мощность N=2,2 кВт ГОСТ 20763-85* шт. 3", conf="MEDIUM", prov="COMPOUND_SEARCH",
  note="RD ВП pump set (3 pcs); differs from the old-generation PD station")
V("ALT79B", "PPM-113", "PD", ["5.8", "2"], 31, 13, "ВПВ - - 5,8 2 струи 2,9 л/с", any_of=((31, 13, ["2.9", "2"], "per-jet flow 2,9 with 2 jets"),),
  conf="MEDIUM", prov="COMPOUND_SEARCH", note="total flow 5,8 л/с, 2 jets of 2,9 л/с; unit order л/с / шт.")
V("ALT79B", "IOS2-073", "PD", ["3.51", "32.0", "0.75"], 31, 41, "Q=3,51 м3/час Н=32,0 м Р=2х0,75 кВт",
  any_of=((31, 41, ["90.0", "40.0", "18.5"], "second station (fire pump) in the same section"),), conf="MEDIUM", prov="COMPOUND_SEARCH",
  note="several pump groups in ИОС2.1; either tuple is a legitimate reading")
for _obj, _code, _why in (
    ("LOS3A", "PPM-112", "9 fans with different (L, P) pairs -- multi-instance, no scalar tuple"),
    ("IZM12", "PPM-112", "fan sets per system (L=6470/P=500, L=7230/P=550, ...)"),
    ("OKT103", "KR-067", "consumption per drawing set only"),
    ("DOO25", "IOS2-073", "pump Q=9,83 м³/ч, H=5,18 м without power in the same sentence"),
    ("LOS3A", "IOS2-073", "several pump groups (fire Q=167/H=9/N=11 and jockey Q=3,6/H=12/N=0,55)"),
):
    P(_obj, _code, "NOT_SCALAR", _why, prov="COMPOUND_SEARCH")
