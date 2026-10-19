"""p95: the finished JSON body of GET /case10/protocols/current is cached per process, and never stale."""
from __future__ import annotations

import unittest

from app.api.routes_case10 import _compact_json_response
from app.db.models import Protocol
from app.domain import protocol_body_cache as cache
from app.domain.v3_pipeline import latest_protocol, protocol_to_dict
import tests.test_case10_api as _api_tests

Case10ApiTests = _api_tests.Case10ApiTests


class ProtocolBodyCacheTests(unittest.TestCase):
    setUp = Case10ApiTests.setUp
    tearDown = Case10ApiTests.tearDown

    def _seed(self):
        seeded = self.client.post(f"/api/case10/projects/{self.project.id}/synthetic-dataset")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        return seeded.json()["process_id"]

    def _url(self, process_id):
        return f"/api/case10/protocols/current?project_id={self.project.id}&process_id={process_id}"

    def test_body_is_byte_identical_to_the_uncached_response_and_second_request_hits(self):
        process_id = self._seed()
        cache.clear()
        before = cache.stats()
        first = self.client.get(self._url(process_id))
        second = self.client.get(self._url(process_id))
        after = cache.stats()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(after["hits"] - before["hits"], 1)
        self.assertEqual(after["misses"] - before["misses"], 1)
        protocol = latest_protocol(self.db, project_id=self.project.id, organization_id=self.user.organization_id,
                                   process_id=process_id)
        self.assertEqual(first.content, _compact_json_response(protocol_to_dict(protocol)).body)
        self.assertEqual(first.headers["content-type"], "application/json")

    def test_inspector_decision_and_finalize_are_reflected_immediately(self):
        process_id = self._seed()
        groups = self.client.get(f"/api/case10/evidence-groups?project_id={self.project.id}&process_id={process_id}").json()
        candidate = next(row for row in groups if row["finding_status"] == "CANDIDATE")
        cached = self.client.get(self._url(process_id)).json()  # fills the cache
        self.assertEqual(self.client.get(self._url(process_id)).json(), cached)  # hit
        decision = self.client.post(f"/api/case10/evidence-groups/{candidate['id']}/decisions",
                                    json={"decision": "Confirm", "comment": "verified"})
        self.assertEqual(decision.status_code, 200, decision.text)
        after = self.client.get(self._url(process_id)).json()
        self.assertEqual(after["version"], cached["version"] + 1)
        finding = next(row for row in after["payload"]["findings"] if row["id"] == candidate["id"])
        self.assertEqual(finding["finding_status"], "CONFIRMED_VIOLATION")
        stale = next(row for row in cached["payload"]["findings"] if row["id"] == candidate["id"])
        self.assertEqual(stale["finding_status"], "CANDIDATE")

    def test_in_place_payload_update_in_this_process_invalidates(self):
        process_id = self._seed()
        first = self.client.get(self._url(process_id)).json()
        protocol = latest_protocol(self.db, project_id=self.project.id, organization_id=self.user.organization_id,
                                   process_id=process_id)
        protocol.payload_json = {**(protocol.payload_json or {}), "iais_rin_sync": {"state": "SYNCED"}}
        self.db.add(protocol)
        self.db.commit()
        second = self.client.get(self._url(process_id)).json()
        self.assertNotIn("iais_rin_sync", first["payload"])
        self.assertEqual(second["payload"]["iais_rin_sync"], {"state": "SYNCED"})

    def test_cache_is_bounded_and_scoped_by_request(self):
        process_id = self._seed()
        cache.clear()
        for index in range(cache.MAX_ENTRIES + 5):
            cache.put(("k", index), b"x")
        self.assertEqual(cache.stats()["entries"], cache.MAX_ENTRIES)
        self.assertIsNone(cache.get(("k", 0)))
        self.assertEqual(cache.get(("k", cache.MAX_ENTRIES + 4)), b"x")
        identity_a = cache.protocol_identity(self.db, project_id=self.project.id,
                                             organization_id=self.user.organization_id, process_id=process_id)
        identity_other_org = cache.protocol_identity(self.db, project_id=self.project.id,
                                                     organization_id=self.user.organization_id + 1, process_id=process_id)
        self.assertIsNotNone(identity_a)
        self.assertIsNone(identity_other_org)
        self.assertIsNone(cache.protocol_identity(self.db, project_id=self.project.id,
                                                  organization_id=self.user.organization_id, process_id="missing"))
        self.assertEqual(self.client.get(self._url("missing")).json(), None)
        self.assertGreaterEqual(self.db.query(Protocol).count(), 1)


if __name__ == "__main__":
    unittest.main()
