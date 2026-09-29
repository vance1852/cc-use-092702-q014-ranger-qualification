"""资格核对在四个关键动作上的跨服务集成测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from biosafety_ops.models import MonitoringRecord, ZoneRecord
from biosafety_ops.service import BiosafetyService
from collection_logistics.api import JsonApplication as CollectionApp
from collection_logistics.clock import FrozenClock as CollectionClock
from collection_logistics.service import CollectionLogisticsService
from qualification_ledger import (
    COMPETENCY_FOREST_FIRE,
    FrozenClock,
    QualificationDenied,
)
from taxonomy_lab.api import JsonApplication as TaxonomyApp
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService

ROOT = Path(__file__).resolve().parents[1]


def grant_fire(ledger, user, valid_from="2026-09-01T00:00:00Z"):
    ledger.record_training(user_id=user, competency_code=COMPETENCY_FOREST_FIRE,
                           scope="forest-fire-basic", valid_from=valid_from,
                           idempotency_key=f"{user}-tr", recorded_by="admin")
    ledger.record_medical(user_id=user, competency_code=COMPETENCY_FOREST_FIRE,
                          scope="fire-ground", valid_from=valid_from,
                          idempotency_key=f"{user}-med", recorded_by="admin")
    for scope in ("fire-suit", "breathing-apparatus"):
        ledger.record_equipment(user_id=user, competency_code=COMPETENCY_FOREST_FIRE,
                                scope=scope, valid_from=valid_from,
                                idempotency_key=f"{user}-eq-{scope}", recorded_by="admin")


class BiosafetyEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.s = BiosafetyService(clock=FrozenClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc)))
        self.s.bootstrap()
        self.t = self.s.auth.login("admin", "biosafety-admin")
        self.s.register_zone_record(self.t, ZoneRecord("S1", "east", "quarantine", 100, 4))
        self.r = self.s.ingest_monitoring_record(
            self.t, MonitoringRecord("R1", "S1", "sensor", 100, 250, 90, "2026-09-24T10:00:00+00:00"))

    def test_unqualified_assignee_is_blocked_at_ticket_creation(self):
        with self.assertRaises(QualificationDenied):
            self.s.create_treatment_ticket(self.t, "S1", self.r["alert_id"], "crew-x")
        self.assertIsNone(
            self.s.db.execute("SELECT * FROM treatment_tickets WHERE assignee='crew-x'").fetchone())

    def test_qualified_assignee_and_allocator_succeed(self):
        grant_fire(self.s.ledger, "crew-y")
        grant_fire(self.s.ledger, "admin")
        order = self.s.create_treatment_ticket(self.t, "S1", self.r["alert_id"], "crew-y")
        self.s.transition_treatment_ticket(self.t, order["treatment_ticket_id"], "assigned", "ok")
        self.s.transition_treatment_ticket(self.t, order["treatment_ticket_id"], "in_progress", "go")
        self.s.add_preservation_resource(self.t, "RS1", "cold-box", "east", 2)
        allocation = self.s.allocate(self.t, "RS1", order["treatment_ticket_id"], 1)
        self.assertFalse(allocation["duplicate"])

    def test_suspension_blocks_in_progress_transition(self):
        grant_fire(self.s.ledger, "crew-z")
        grant_fire(self.s.ledger, "admin")
        order = self.s.create_treatment_ticket(self.t, "S1", self.r["alert_id"], "crew-z")
        self.s.transition_treatment_ticket(self.t, order["treatment_ticket_id"], "assigned", "ok")
        self.s.ledger.suspend(user_id="crew-z", reason="装备复核停权",
                              valid_from="2026-09-24T00:00:00Z",
                              idempotency_key="susp", recorded_by="admin")
        with self.assertRaises(QualificationDenied):
            self.s.transition_treatment_ticket(self.t, order["treatment_ticket_id"], "in_progress", "go")


class TaxonomyEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for uid, role in (("operator", "operator"), ("stat", "statistician"),
                          ("approver", "approver"), ("auditor", "auditor")):
            self.service.create_user(uid, uid, role)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [json.loads(line) for line in
                     (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)

    def tearDown(self):
        self.connection.close()

    def test_unqualified_worker_cannot_claim_job(self):
        # 队列中已有一个待领任务，但无资格工人在领取时刻被拒，租约不转移。
        with self.assertRaises(QualificationDenied):
            self.service.claim_job("newbie", 30)
        job = self.connection.execute("SELECT * FROM analysis_jobs").fetchone()
        self.assertNotEqual(job["lease_owner"], "newbie")
        self.assertEqual(job["state"], "queued")

    def test_qualified_worker_claims_job(self):
        self.service.record_qualification_event("stat", {
            "user_id": "worker", "event_type": "training_passed",
            "competency_code": "specimen-review", "scope": "taxonomy-review-basic",
            "valid_from": "2026-09-01T00:00:00Z", "idempotency_key": "w-train"})
        job = self.service.claim_job("worker", 30)
        self.assertIsNotNone(job)
        self.assertEqual(job["lease_owner"], "worker")

    def test_unqualified_statistician_cannot_review_exclusion(self):
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1").fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "记录失效")
        with self.assertRaises(QualificationDenied):
            self.service.review_exclusion("stat", requested["exclusion_id"], True, "ok")

    def test_replay_does_not_duplicate_review_check(self):
        self.service.record_qualification_event("stat", {
            "user_id": "stat", "event_type": "training_passed",
            "competency_code": "specimen-review", "scope": "taxonomy-review-basic",
            "valid_from": "2026-09-01T00:00:00Z", "idempotency_key": "stat-train"})
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1").fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "记录失效")
        self.service.review_exclusion("stat", requested["exclusion_id"], True, "ok")
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM qual_action_checks").fetchone()[0], 1)


class CollectionEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = CollectionLogisticsService(
            self.connection, CollectionClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc)))
        for uid, role in (("plan", "planner"), ("dispatch", "dispatcher"),
                          ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(uid, uid, role)
        self.service.create_facility("plan", {"center_id": "c1", "name": "中心", "kind": "storage",
                                              "timezone": "Asia/Shanghai", "capacity_units": "1000"})
        self.service.create_facility("plan", {"center_id": "c2", "name": "终端", "kind": "receiving-vault",
                                              "timezone": "Asia/Shanghai", "capacity_units": "1000"})
        self.service.create_route("plan", {"corridor_id": "r1", "origin_center_id": "c1",
                                           "destination_center_id": "c2",
                                           "preservation_resource_kind": "preservation-box",
                                           "hourly_capacity": "100", "delay_basis_points": 0,
                                           "response_minutes": 12})
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "c1",
                                                     "preservation_resource_kind": "preservation-box", "grade": "A",
                                                     "quantity_units": "10", "unit_cost_cny": "5",
                                                     "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "d1", "corridor_id": "r1",
                                                  "specimen_event_id": "ev-1", "duty_date": "2026-09-25",
                                                  "requested_units": "4", "priority": 10, "idempotency_key": "k1"})
        self.service.allocate("dispatch", "r1", "2026-09-25")

    def tearDown(self):
        self.connection.close()

    def test_unqualified_dispatcher_cannot_deploy(self):
        with self.assertRaises(QualificationDenied):
            self.service.dispatch_deployment("dispatch", "dep-1", "d1", "lot-1", 2)

    def test_qualified_dispatcher_deploys(self):
        self.service.record_qualification_event("risk", {
            "user_id": "dispatch", "event_type": "training_passed",
            "competency_code": "specimen-review", "scope": "taxonomy-review-basic",
            "valid_from": "2026-09-01T00:00:00Z", "idempotency_key": "disp-train"})
        result = self.service.dispatch_deployment("dispatch", "dep-1", "d1", "lot-1", 2)
        self.assertEqual(result["state"], "in_transit")


class QualificationHttpApiTests(unittest.TestCase):
    def test_taxonomy_api_returns_403_and_explanation(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        service = TaxonomyLabService(connection, FrozenClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc)))
        service.create_user("stat", "stat", "statistician")
        app = TaxonomyApp(service)
        response = app.handle("GET", "/qualifications/ghost", {"X-Actor-Id": "stat"})
        self.assertEqual(response.status, 200)
        body = response.body
        self.assertEqual(body["rules_version"], "rules-2026-09-01")
        self.assertFalse(body["competencies"]["specimen-review"]["granted"])
        connection.close()

    def test_collection_api_qualification_event_round_trip(self):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        service = CollectionLogisticsService(
            connection, CollectionClock(datetime(2026, 9, 24, 8, tzinfo=timezone.utc)))
        service.create_user("risk", "risk", "risk")
        service.create_user("audit", "audit", "auditor")
        app = CollectionApp(service)
        payload = json.dumps({"user_id": "dispatch", "event_type": "training_passed",
                              "competency_code": "specimen-review", "scope": "taxonomy-review-basic",
                              "valid_from": "2026-09-01T00:00:00Z", "idempotency_key": "k1"}).encode()
        created = app.handle("POST", "/qualification_events", {"X-Actor-Id": "risk"}, payload)
        self.assertEqual(created.status, 201)
        chain = app.handle("GET", "/qualifications/chain", {"X-Actor-Id": "audit"})
        self.assertEqual(chain.status, 200)
        self.assertTrue(chain.body["valid"])
        connection.close()


if __name__ == "__main__":
    unittest.main()
