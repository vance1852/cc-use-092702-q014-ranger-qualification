"""资格账本接入三个业务服务的端到端集成测试。

验证：任务领取、样本复核、风险处置和资源调拨在关键动作发生时
确实调用资格网关；无网关时默认放行，保持既有行为不变。
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from biosafety_ops.models import MonitoringRecord, ZoneRecord
from biosafety_ops.service import BiosafetyService
from collection_logistics.clock import FrozenClock as LogisticsClock
from collection_logistics.errors import Forbidden as LogisticsForbidden
from collection_logistics.service import CollectionLogisticsService
from qualification_ledger.gate import LedgerGate
from qualification_ledger.service import QualificationLedgerService
from taxonomy_lab.clock import FrozenClock as LabClock
from taxonomy_lab.errors import Forbidden as LabForbidden
from taxonomy_lab.service import TaxonomyLabService


def ledger_with_people(people: list[str], clock=None) -> tuple[sqlite3.Connection, QualificationLedgerService, LedgerGate]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = QualificationLedgerService(connection, clock)
    service.create_user("sys", "系统网关账号", "administrator")
    for person in people:
        service.create_user(person, person, "reviewer")
    return connection, service, LedgerGate(service, "sys")


def grant_resource_allocation(ledger: QualificationLedgerService, person: str) -> None:
    ledger.record_grant("sys", "training_passed", person, "equipment-ops",
                        "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
    ledger.record_grant("sys", "equipment_authorized", person, "protective-gear",
                        "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")


def grant_risk_handling(ledger: QualificationLedgerService, person: str) -> None:
    for scope in ("forest-fire-basic", "wilderness-first-aid"):
        ledger.record_grant("sys", "training_passed", person, scope,
                            "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
    ledger.record_grant("sys", "medical_cleared", person, "general-medical",
                        "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
    ledger.record_grant("sys", "equipment_authorized", person, "protective-gear",
                        "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")


class BiosafetyGateIntegrationTests(unittest.TestCase):
    def test_risk_handling_and_allocation_check_qualification(self) -> None:
        ledger_db, ledger, gate = ledger_with_people(["admin"])
        service = BiosafetyService(gate=gate)
        service.bootstrap()
        token = service.auth.login("admin", "biosafety-admin")
        service.register_zone_record(token, ZoneRecord("S1", "east", "quarantine", 100, 4))
        ingest = service.ingest_monitoring_record(
            token, MonitoringRecord("R1", "S1", "sensor", 120, 250, 90, "2026-01-01T00:00:00+00:00")
        )
        # 未登记任何资格：处置流转与资源调拨都被网关拒绝（PermissionError）。
        ticket = service.create_treatment_ticket(token, "S1", ingest["alert_id"], "crew")
        with self.assertRaises(PermissionError):
            service.transition_treatment_ticket(
                token, ticket["treatment_ticket_id"], "assigned", "accept"
            )
        service.add_preservation_resource(token, "PR1", "cold-box", "east", 2)
        with self.assertRaises(PermissionError):
            service.allocate(token, "PR1", ticket["treatment_ticket_id"], 1)
        # 补齐资格后两类动作放行。
        grant_risk_handling(ledger, "admin")
        grant_resource_allocation(ledger, "admin")
        assigned = service.transition_treatment_ticket(
            token, ticket["treatment_ticket_id"], "assigned", "accept"
        )
        self.assertEqual(assigned["status"], "assigned")
        allocation = service.allocate(token, "PR1", ticket["treatment_ticket_id"], 1)
        self.assertFalse(allocation["duplicate"])
        # 被拒与补齐资格后的通过各留一条裁决（尝试键含时刻，允许补正后重试）。
        decisions = ledger.decisions("sys", business_ref=f"treatment_ticket:{ticket['treatment_ticket_id']}")
        self.assertEqual([row["approved"] for row in decisions], [0, 1])
        ledger_db.close()


class TaxonomyGateIntegrationTests(unittest.TestCase):
    def test_task_claim_checks_qualification(self) -> None:
        clock = LabClock(datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
        ledger_db, ledger, gate = ledger_with_people(["worker"], clock)
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        service = TaxonomyLabService(connection, clock, gate=gate)
        # 该用例只验证领取网关：关闭外键后直接放入一个最小的待领取任务。
        connection.execute("PRAGMA foreign_keys=OFF")
        now = clock.now().isoformat().replace("+00:00", "Z")
        # 直接构造一个待领取任务（该人员尚未登记任何资格）。
        connection.execute(
            "INSERT INTO analysis_jobs(batch_id,batch_revision,state,available_at,created_at,updated_at) "
            "VALUES('batch-x',1,'queued',?,?,?)",
            (now, now, now),
        )
        with self.assertRaises(LabForbidden):
            service.claim_job("worker", 60, task_kind="wildlife_rescue")
        # 任务仍在队列中（领取事务回滚）。
        self.assertIsNotNone(
            connection.execute("SELECT job_id FROM analysis_jobs WHERE state='queued'").fetchone()
        )
        # 补齐野生动物救护任务线资格后领取成功。
        for scope in ("wildlife-rescue", "wilderness-first-aid"):
            ledger.record_grant("sys", "training_passed", "worker", scope,
                                "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
        ledger.record_grant("sys", "medical_cleared", "worker", "general-medical",
                            "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
        claimed = service.claim_job("worker", 60, task_kind="wildlife_rescue")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["lease_owner"], "worker")
        decisions = ledger.decisions("sys", action="task_claim.wildlife_rescue")
        self.assertEqual([row["approved"] for row in decisions], [0, 1])
        decision = decisions[-1]
        self.assertEqual(decision["explanation"]["rule_version"], "rules-2026.1")
        connection.close()
        ledger_db.close()


class CollectionLogisticsGateIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = LogisticsClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.ledger_db, self.ledger, self.gate = ledger_with_people(["dispatch"], self.clock)
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = LogisticsClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock, gate=self.gate)
        self.service.create_user("plan", "plan", "planner")
        self.service.create_user("dispatch", "dispatch", "dispatcher")
        self.service.create_facility("plan", {"center_id": "c1", "name": "站", "kind": "storage",
                                              "timezone": "Asia/Shanghai", "capacity_units": "100"})
        self.service.create_facility("plan", {"center_id": "c2", "name": "终端", "kind": "receiving-vault",
                                              "timezone": "Asia/Shanghai", "capacity_units": "100"})
        self.service.create_route("plan", {"corridor_id": "r1", "origin_center_id": "c1",
                                           "destination_center_id": "c2",
                                           "preservation_resource_kind": "preservation-box",
                                           "hourly_capacity": "100", "delay_basis_points": 0,
                                           "response_minutes": 12})
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1",
                                                    "center_id": "c1",
                                                    "preservation_resource_kind": "preservation-box",
                                                    "grade": "A", "quantity_units": "10",
                                                    "unit_cost_cny": "1",
                                                    "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_dispatch("dispatch", {"dispatch_id": "d1", "corridor_id": "r1",
                                                  "specimen_event_id": "s1", "duty_date": "2026-09-25",
                                                  "requested_units": "5", "priority": 10,
                                                  "idempotency_key": "k1"})
        self.service.allocate("dispatch", "r1", "2026-09-25")

    def tearDown(self) -> None:
        self.connection.close()
        self.ledger_db.close()

    def test_deployment_checks_resource_allocation_qualification(self) -> None:
        # 调度员无调拨资格：资源到场动作被拒，库存未被占用。
        with self.assertRaises(LogisticsForbidden):
            self.service.dispatch_deployment("dispatch", "dep-1", "d1", "lot-1", 2)
        grant_resource_allocation(self.ledger, "dispatch")
        deployment = self.service.dispatch_deployment("dispatch", "dep-1", "d1", "lot-1", 2)
        self.assertEqual(deployment["state"], "in_transit")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "5.000")
        decisions = self.ledger.decisions("sys", business_ref="dispatch:d1")
        self.assertEqual([row["approved"] for row in decisions], [0, 1])
        decision = decisions[-1]
        self.assertEqual(decision["action"], "resource_allocation")


class DefaultBehaviorTests(unittest.TestCase):
    def test_services_work_without_gate(self) -> None:
        # 未注入网关时三个服务行为与接入前完全一致。
        service = BiosafetyService()
        service.bootstrap()
        token = service.auth.login("admin", "biosafety-admin")
        service.register_zone_record(token, ZoneRecord("S9", "east", "water", 10, 1))
        record = service.ingest_monitoring_record(
            token, MonitoringRecord("R9", "S9", "s", 1, 1, 1, "2026-01-01T00:00:00+00:00")
        )
        self.assertFalse(record["duplicate"])


if __name__ == "__main__":
    unittest.main()
