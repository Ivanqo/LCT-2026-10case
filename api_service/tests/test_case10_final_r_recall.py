"""Final R: CHANGE_SHEET and LAYER_COMPOSITION (TZ 9.5 free hypotheses).

Every detector has a positive case, a negative case, a de-duplication case and an incremental case. Pages are synthetic word
lists (no PDF); the scan layer and the PD page reader are replaced by stubs, the embedding model is not used."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import DocumentVersion, EvidenceGroup, InspectionProcess, MatrixVersion, Suspicion
from app.db.session import Base
from app.domain.free_hypotheses import base, change_detect, change_sheet, layer_composition as lc, layer_detect, store
from app.domain.free_hypotheses.base import merge_duplicates


def word(text, x0, y0, x1=None, y1=None):
    return {"text": text, "bbox": [x0, y0, x1 if x1 is not None else x0 + 6 * len(text), y1 if y1 is not None else y0 + 8]}


def line(text, x0, y0):
    out, x = [], x0
    for token in text.split():
        out.append(word(token, x, y0))
        x += 6 * len(token) + 4
    return out


def register_page(entries, *, with_header=True):
    words = []
    if with_header:
        words += [word("Изм.", 60, 100), word("Лист", 110, 100), word("Содержание", 190, 100), word("изменения", 250, 100), word("Код", 440, 100), word("Примечание", 490, 100)]
    y = 130
    for izm, sheet, text in entries:
        parts = text.split("|")
        top = y - 6 * (len(parts) - 1)
        words += [word(izm, 70, y), word(sheet, 120, y), word("1", 446, y)]
        for index, part in enumerate(parts):
            words += line(part, 180, top + 12 * index)
        y += 12 * len(parts) + 14
    return {"words": words, "width": 595.0, "height": 842.0, "page": 1}


def doc(doc_id, stage, name=None):
    return SimpleNamespace(id=doc_id, dataset_file_id=name or f"F{doc_id:03d}", document_code=f"AR-{doc_id}", filename=f"{doc_id}.pdf", file_hash=f"{doc_id:064x}",
                           content_hash=f"{doc_id:064x}", discipline="AR", revision="0", approval_status="UNKNOWN", dataset_stage=stage,
                           doc_stage={"PD": "project", "RD": "working", "ID": "as_built"}[stage])


def gate_for(*docs, tags=frozenset({"АР"})):
    return SimpleNamespace(facts={d.id: SimpleNamespace(tags=tags, markers=(), generation=None, section_key=None) for d in docs},
                           drop_from_generic_candidates=lambda _id: False)


# ---------------------------------------------------------------- CHANGE_SHEET
class ChangeRegisterParserTests(unittest.TestCase):
    def test_entries_are_read_with_revision_sheet_text_and_box(self):
        page = register_page([("6", "19", "В квартире 2.4.1 подвинулась дверь|в помещении СУ."), ("6", "20", "Удалена спецификация откосов витражей.")])
        entries = change_sheet.parse_change_register(page, page=3)
        self.assertEqual([(e["izm"], e["sheet"]) for e in entries], [("6", "19"), ("6", "20")])
        self.assertEqual(entries[0]["text"], "В квартире 2.4.1 подвинулась дверь в помещении СУ.")
        self.assertTrue(entries[0]["bbox"] and entries[0]["page"] == 3)

    def test_page_without_the_register_header_gives_nothing(self):
        self.assertEqual(change_sheet.parse_change_register(register_page([("1", "2", "Удалена спецификация откосов витражей.")], with_header=False)), [])

    def test_kinds_ids_and_subject(self):
        self.assertEqual(change_sheet.entry_kind("Металлический бронированный лист исключeн"), change_sheet.KIND_REMOVED)   # Latin e inside
        self.assertEqual(change_sheet.entry_kind("В квартире 2.4.1 подвинулась дверь"), change_sheet.KIND_MOVED)
        self.assertEqual(change_sheet.entry_kind("Добавлена перемычка ПР-49"), change_sheet.KIND_ADDED)
        self.assertIsNone(change_sheet.entry_kind("Указаны для МГН тактильные указатели"))
        self.assertEqual(change_sheet.entry_ids("В квартире 2.4.1 перемычка ПР-49, ГОСТ 1.2"), ["2.4.1", "ПР-49"])
        self.assertNotIn("исклю", change_sheet.entry_subject_stems("Металлический бронированный лист исключен"))


def pd_page_words(text, x0=100, y0=200):
    return line(text, x0, y0) + line("Прочий текст чертежа без связи", 100, 400)


class ChangeDetectTests(unittest.TestCase):
    def setUp(self):
        self.rd = doc(2, "RD")
        self.pd = doc(1, "PD")
        self.gate = gate_for(self.rd, self.pd)

    def run_detect(self, entry_text, pd_text, *, ids_pages=None, rd_docs=None):
        entries = change_sheet.parse_change_register(register_page([("1", "6", entry_text)]), page=4)
        stems_of_pd = {s: [7] for s in change_sheet.stems(pd_text)}
        df = {s: 60 for s in change_sheet.stems(entry_text)}                     # the entry's other words are common in the PD
        df.update({s: 1 for s in stems_of_pd})
        index = {"pages_total": 100, "df": df, "inv": stems_of_pd, "ids": ids_pages or {}}
        rd_scan = {"changes": {"entries": entries, "low_quality": []}}

        def fake_scan(documents, kinds):
            return {int(d.id): (rd_scan if "changes" in kinds else {"index": index}) for d in documents}

        snapshot = {7: {"words": pd_page_words(pd_text), "width": 842.0, "height": 595.0}}
        with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", side_effect=fake_scan), \
                mock.patch("app.domain.dataset_sources.extract_original_pages", return_value=snapshot):
            return change_detect.detect(rd_docs or [self.pd, self.rd], self.gate, object_id="OBJ")

    def test_removed_element_still_present_in_the_pd_is_a_hypothesis_with_both_sources(self):
        found, stats = self.run_detect("Металлический бронированный лист предусмотренный|в конструкции покрытия исключен", "Бронированный стальной лист 6")
        self.assertEqual(len(found), 1, stats)
        hypothesis = found[0]
        self.assertEqual(hypothesis.method, base.METHOD_CHANGE_SHEET)
        self.assertEqual({r.stage for r in hypothesis.evidence}, {"PD", "RD"})
        self.assertTrue(all(r.bbox_pdf and r.page for r in hypothesis.evidence))
        self.assertIn("параметр Матрицы не определён", hypothesis.description)
        self.assertEqual(hypothesis.details["evidence_required"], ["PD", "RD"])

    def test_identifier_entry_is_matched_by_the_identifier(self):
        found, _ = self.run_detect("В квартире 2.4.1 подвинулась дверь|в помещении СУ.", "Квартира 2.4.1 план", ids_pages={"2.4.1": [7]})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].details["matched_by"], "identifier")

    def test_no_pd_counterpart_means_no_hypothesis(self):
        found, stats = self.run_detect("Металлический бронированный лист предусмотренный|в конструкции покрытия исключен", "Водосточная воронка с обогревом")
        self.assertEqual(found, [])
        self.assertEqual(stats.get("entries_without_pd_match"), 1)

    def test_added_entries_are_not_used(self):
        found, stats = self.run_detect("Добавлен металлический бронированный лист в конструкции покрытия", "Бронированный стальной лист 6")
        self.assertEqual(found, [])
        self.assertEqual(stats["entries_added_kind"], 1)

    def test_same_claim_from_two_rd_editions_is_one_hypothesis(self):
        first, _ = self.run_detect("Металлический бронированный лист предусмотренный|в конструкции покрытия исключен", "Бронированный стальной лист 6")
        second, _ = self.run_detect("Металлический бронированный лист предусмотренный|в конструкции покрытия исключен", "Бронированный стальной лист 6")
        merged, dropped = merge_duplicates(first + second)
        self.assertEqual((len(merged), dropped), (1, 1))


# ---------------------------------------------------------------- LAYER_COMPOSITION
def stack_page(layers, x0=500, y0=100, gap=14):
    words = []
    for index, (name, mm) in enumerate(layers):
        y = y0 + index * gap
        words += line(name, x0, y)
        words.append(word(str(mm), x0 + 300, y))
    words += line("Разрез 1-1 М1:50 условные обозначения чертежа", 100, 600)
    return {"words": words, "width": 1190.0, "height": 842.0, "page": 1}


ROOF_PD = [("Кровельная мембрана", 2), ("Утеплитель минеральная вата", 250), ("Пароизоляция", 1), ("Бронированный стальной лист", 6), ("Бетон монолитный", 200)]
ROOF_RD = [row for row in ROOF_PD if row[0] != "Бронированный стальной лист"]


def composition_of(layers, label=None, page_text=""):
    return {"page": 1, "label": label, "width": 1190.0, "height": 842.0, "bbox": [500, 100, 800, 200], "page_stems": change_sheet.stems(page_text),
            "layers": [{"name": n, "mm": str(mm), "bbox": [500, 100 + 14 * i, 780, 108 + 14 * i]} for i, (n, mm) in enumerate(layers)]}


class LayerParserTests(unittest.TestCase):
    def test_a_stack_of_layers_with_thickness_is_one_composition(self):
        comps = lc.parse_compositions(stack_page(ROOF_PD))
        self.assertEqual(len(comps), 1)
        self.assertEqual([l["mm"] for l in comps[0]["layers"]], ["2", "250", "1", "6", "200"])
        self.assertEqual(comps[0]["layers"][3]["name"], "Бронированный стальной лист")

    def test_thickness_with_unit_in_a_far_cell_and_inline_thickness_text(self):
        names = [("Стяжка из цем.-пес. раствора М150", 50), ("Технониколь XPS CARBON PROF", 50), ("Ж/б плита перекрытия", 180), ("Утеплитель плиты из минеральной ваты", 100)]
        words = []
        for index, (name, mm) in enumerate(names):
            y = 100 + 14 * index
            words += line(name, 40, y)
            words += [word("-", 380, y, 384, y + 8), word(str(mm), 390, y), word("мм", 410, y)]
        words += line("Штукатурка декоративная по стеклосетке t=20мм", 40, 156) + line("Прочий текст чертежа без связи", 40, 400)
        comps = lc.parse_compositions({"words": words, "width": 600.0, "height": 800.0, "page": 1})
        self.assertEqual(len(comps), 1)
        self.assertEqual([l["mm"] for l in comps[0]["layers"]][:4], ["50", "50", "180", "100"])

    def test_grain_size_and_a_capitalised_line_without_thickness_are_not_thickness_or_name_continuation(self):
        names = [("Тротуарная плитка", 80), ("Цементно-песчаная смесь", 40), ("Керамзитовый гравий", 40)]
        words = []
        for index, (name, mm) in enumerate(names):
            words += line(name, 40, 100 + 14 * index) + [word("-", 380, 100 + 14 * index, 384, 108 + 14 * index), word(str(mm), 390, 100 + 14 * index), word("мм", 410, 100 + 14 * index)]
        words += line("Мембрана PLANTER standart", 40, 142) + line("Гравий фр. 5-20 мм.", 40, 156) + line("Прочий текст чертежа без связи", 40, 400)
        comps = lc.parse_compositions({"words": words, "width": 600.0, "height": 800.0, "page": 1})
        self.assertEqual([(l["name"], l["mm"]) for l in comps[0]["layers"]][:3], [("Тротуарная плитка", "80"), ("Цементно-песчаная смесь", "40"), ("Керамзитовый гравий", "40")])
        self.assertEqual(lc._EXPLICIT_RE.findall("Гравий фр. 5-20 мм."), [])

    def test_a_column_of_rooms_with_numbers_is_not_a_composition(self):
        rooms = [("Тамбур", 2), ("Коридор", 3), ("Санузел", 4), ("Кабинет", 5)]
        self.assertEqual(lc.parse_compositions(stack_page(rooms)), [])


class LayerCompareTests(unittest.TestCase):
    def compare(self, pd, rd):
        return lc.compare_compositions(composition_of(pd), composition_of(rd), semantic_min=0.8, use_semantic=False)

    def test_removed_layer(self):
        result = self.compare(ROOF_PD, ROOF_RD)
        self.assertEqual([d["kind"] for d in result["diffs"]], [lc.KIND_REMOVED])
        self.assertTrue(lc.same_construction(result, composition_of(ROOF_PD), composition_of(ROOF_RD)))

    def test_thickness_change_after_alignment(self):
        rd = [(n, 300 if n.startswith("Утеплитель") else mm) for n, mm in ROOF_PD]
        result = self.compare(ROOF_PD, rd)
        self.assertEqual([(d["kind"], d["pd_mm"], d["rd_mm"]) for d in result["diffs"]], [(lc.KIND_THICKNESS, "250", "300")])

    def test_replaced_material(self):
        rd = [("Гравий керамзитовый", mm) if n.startswith("Бетон") else (n, mm) for n, mm in ROOF_PD]
        result = self.compare(ROOF_PD, rd)
        self.assertEqual([d["kind"] for d in result["diffs"]], [lc.KIND_MATERIAL])

    def test_identical_and_reworded_stacks_have_no_difference(self):
        reworded = [(n.replace("Кровельная мембрана", "Кровельная мембрана ПВХ"), mm) for n, mm in ROOF_PD]
        self.assertEqual(self.compare(ROOF_PD, ROOF_PD)["diffs"], [])
        self.assertEqual(self.compare(ROOF_PD, reworded)["diffs"], [])

    def test_a_different_construction_is_not_paired(self):
        floor = [("Плитка керамическая", 10), ("Клей плиточный", 5), ("Стяжка цементно-песчаная", 50), ("Гидроизоляция", 3)]
        result = self.compare(ROOF_PD, floor)
        self.assertFalse(lc.same_construction(result, composition_of(ROOF_PD), composition_of(floor)))


class LayerDetectTests(unittest.TestCase):
    def setUp(self):
        self.pd, self.rd = doc(1, "PD"), doc(2, "RD")
        self.gate = gate_for(self.pd, self.rd)

    def run_detect(self, pd_layers, rd_layers, *, pd_label=None, rd_label=None, extra_pd=()):
        scans = {1: {"layers": {"compositions": [composition_of(pd_layers, pd_label)] + [composition_of(e) for e in extra_pd]}},
                 2: {"layers": {"compositions": [composition_of(rd_layers, rd_label)]}}}
        with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", return_value=scans), \
                mock.patch("app.domain.semantic_similarity.is_enabled", return_value=False):
            return layer_detect.detect([self.pd, self.rd], self.gate, object_id="OBJ")

    def test_removed_layer_is_a_hypothesis_with_both_sources(self):
        found, stats = self.run_detect(ROOF_PD, ROOF_RD, pd_label="П-1", rd_label="П-1")
        self.assertEqual(len(found), 1, stats)
        hypothesis = found[0]
        self.assertEqual(hypothesis.method, base.METHOD_LAYERS)
        self.assertEqual(hypothesis.details["layer_diff"], lc.KIND_REMOVED)
        self.assertEqual({r.stage for r in hypothesis.evidence}, {"PD", "RD"})
        self.assertIn("Бронированный стальной лист", hypothesis.expected)
        self.assertIsNone(hypothesis.actual)

    def test_identical_stack_anywhere_in_the_pd_means_nothing_to_report(self):
        found, stats = self.run_detect(ROOF_PD, ROOF_RD, extra_pd=[ROOF_RD])
        self.assertEqual(found, [])
        self.assertEqual(stats["rd_identical_to_pd"], 1)

    def test_different_labels_are_different_constructions(self):
        found, _ = self.run_detect(ROOF_PD, ROOF_RD, pd_label="П-1", rd_label="П-2")
        self.assertEqual(found, [])

    def test_a_layer_still_named_on_the_other_page_is_not_a_removed_layer(self):
        scans = {1: {"layers": {"compositions": [composition_of(ROOF_PD)]}},
                 2: {"layers": {"compositions": [composition_of(ROOF_RD, page_text="Бронированный стальной лист предусмотрен по месту")]}}}
        with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", return_value=scans),                 mock.patch("app.domain.semantic_similarity.is_enabled", return_value=False):
            found, stats = layer_detect.detect([self.pd, self.rd], self.gate, object_id="OBJ")
        self.assertEqual(found, [])
        self.assertEqual(stats["layers_named_elsewhere_on_other_page"], 1)

    def test_same_claim_twice_is_one_hypothesis(self):
        first, _ = self.run_detect(ROOF_PD, ROOF_RD)
        second, _ = self.run_detect(ROOF_PD, ROOF_RD)
        merged, dropped = merge_duplicates(first + second)
        self.assertEqual((len(merged), dropped), (1, 1))


# ---------------------------------------------------------------- incremental behaviour through the shared store
class IncrementalTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.matrix = MatrixVersion(organization_id=1, project_id=1, version="official-132-v1")
        self.db.add(self.matrix)
        self.db.flush()
        self.process = InspectionProcess(id="proc-r", project_id=1, organization_id=1, object_id="OBJ", matrix_version="official-132-v1",
                                         dataset_version="synthetic-v1", upload_scenario="FULL", status="RUNNING")
        self.db.add(self.process)
        for doc_id in (1, 2):
            self.db.add(DocumentVersion(id=doc_id, project_id=1, organization_id=1, filename=f"{doc_id}.pdf"))
        self.db.flush()
        self.sentinel = store.ensure_free_search_param(self.db, self.process, [])
        self.pd, self.rd = doc(1, "PD"), doc(2, "RD")
        self.gate = gate_for(self.pd, self.rd)

    def candidates(self, rd_layers):
        scans = {1: {"layers": {"compositions": [composition_of(ROOF_PD)]}}, 2: {"layers": {"compositions": [composition_of(rd_layers)]}}}
        with mock.patch("app.domain.free_hypotheses.doc_scan.scan_documents", return_value=scans), \
                mock.patch("app.domain.semantic_similarity.is_enabled", return_value=False):
            return layer_detect.detect([self.pd, self.rd], self.gate, object_id="OBJ")[0]

    def run_store(self, found):
        stats = store.store_candidates(self.db, self.process, self.sentinel, found)
        stale = store.reconcile_stale(self.db, self.process, self.sentinel, stats["keys"], ran_methods=(base.METHOD_LAYERS,))
        self.db.flush()
        return stats, stale

    def test_rerun_writes_nothing_and_a_vanished_hypothesis_is_removed(self):
        self.run_store(self.candidates(ROOF_RD))
        stats, _ = self.run_store(self.candidates(ROOF_RD))
        self.assertEqual((stats["created"], stats["unchanged"]), (0, 1))
        _, stale = self.run_store(self.candidates(ROOF_PD))                       # RD now equals PD
        self.assertEqual(stale["removed"], 1)
        self.assertEqual(self.db.query(Suspicion).count(), 0)

    def test_inspector_review_survives_a_changed_rerun(self):
        self.run_store(self.candidates(ROOF_RD))
        self.db.query(Suspicion).one().inspector_status = "REJECTED"
        self.db.flush()
        rd_changed = [(n, mm + 1 if n.startswith("Утеплитель") else mm) for n, mm in ROOF_RD]
        stats, _ = self.run_store(self.candidates(rd_changed))
        self.assertEqual(self.db.query(Suspicion).filter(Suspicion.inspector_status == "REJECTED").count(), 1)
        self.assertGreaterEqual(stats["flagged"] + stats["unchanged"] + stats["created"], 1)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- the scan layer on a real (generated) PDF, with its cache
ARIAL = "C:/Windows/Fonts/arial.ttf"


@unittest.skipUnless(__import__("os").path.isfile(ARIAL), "needs a Cyrillic system font to draw the test pages")
class ScanLayerTests(unittest.TestCase):
    def build_pdf(self, directory):
        import hashlib
        import fitz

        pdf = fitz.open()
        register = pdf.new_page(width=595, height=842)
        for w in register_page([("1", "6", "Металлический бронированный лист предусмотренный|в конструкции покрытия исключен"), ("1", "7", "Добавлен обогрев воронок")])["words"]:
            register.insert_text((w["bbox"][0], w["bbox"][3]), w["text"], fontsize=8, fontfile=ARIAL, fontname="arl")
        section = pdf.new_page(width=1190, height=842)
        for w in stack_page(ROOF_PD)["words"]:
            section.insert_text((w["bbox"][0], w["bbox"][3]), w["text"], fontsize=8, fontfile=ARIAL, fontname="arl")
        data = pdf.tobytes()
        (directory / "a.pdf").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return SimpleNamespace(id=5, dataset_metadata={"document_manifest": {"relative_path": "a.pdf"}}, file_hash=digest, content_hash=digest), digest

    def test_scan_reads_register_layers_and_index_and_caches_by_sha(self):
        import os
        import tempfile
        from pathlib import Path

        from app.domain import dataset_sources
        from app.domain.free_hypotheses import doc_scan

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
            directory = Path(tmp)
            document, digest = self.build_pdf(directory)
            env = {"CASE10_ORIGINALS_ROOT": tmp, "CASE10_FREE_HYPOTHESES_CACHE_DIR": cache}
            with mock.patch.dict(os.environ, env):
                dataset_sources._original_document_bytes.cache_clear()
                dataset_sources._original_page_snapshots.cache_clear()
                result = doc_scan.scan_documents([document], ["changes", "layers", "index"])[5]
                self.assertEqual(len(result["changes"]["entries"]), 2, result["changes"])
                self.assertEqual(result["changes"]["entries"][0]["sheet"], "6")
                self.assertEqual(len(result["layers"]["compositions"]), 1)
                self.assertEqual(len(result["layers"]["compositions"][0]["layers"]), 5)
                self.assertIn("брони", result["index"]["df"])
                self.assertTrue(list(Path(cache, "pages").glob(f"{digest}.json")))
                # second call is served from the cache: the PDF is not opened any more
                with mock.patch("app.domain.free_hypotheses.doc_scan._scan_uncached", side_effect=AssertionError("cache miss")):
                    again = doc_scan.scan_documents([document], ["changes", "layers", "index"])[5]
                self.assertEqual(again["changes"]["entries"], result["changes"]["entries"])
