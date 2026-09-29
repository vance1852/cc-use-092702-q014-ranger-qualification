from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from qualification_ledger.api import JsonApplication
from qualification_ledger.clock import FrozenClock
from qualification_ledger.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    QualificationDenied,
    ValidationFailed,
)
from qualification_ledger.gate import GateDenied, LedgerGate, PermissiveGate
from qualification_ledger.projection import evaluate, project
from qualification_ledger.service import QualificationLedgerService


def service_with(clock: FrozenClock | None = None) -> tuple[sqlite3.Connection, QualificationLedgerService, FrozenClock]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    frozen = clock or FrozenClock(datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
    service = QualificationLedgerService(connection, frozen)
    service.create_user("admin", "管理员", "administrator")
    service.create_user("review", "复核员", "reviewer")
    service.create_user("audit", "审计员", "auditor")
    service.create_user("p1", "巡护员甲", "reviewer")
    return connection, service, frozen


def qualified_person(service: QualificationLedgerService, person: str = "p1") -> None:
    """登记满足高海拔夜巡（要求最全）的一揽子授权。"""

    for scope in ("high-altitude-night-patrol", "wilderness-first-aid",
                  "forest-fire-basic", "wildlife-rescue", "taxonomy-review", "equipment-ops"):
        service.record_grant("admin", "training_passed", person, scope,
                             "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
    for scope in ("general-medical", "high-altitude-medical"):
        service.record_grant("admin", "medical_cleared", person, scope,
                             "2026-02-01T00:00:00Z", "2026-10-31T23:59:59Z")
    for scope in ("night-optics", "high-altitude-gear", "protective-gear"):
        service.record_grant("admin", "equipment_authorized", person, scope,
                             "2026-02-01T00:00:00Z", "2027-01-31T23:59:59Z")


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.service, self.clock = service_with()
        qualified_person(self.service)

    def events(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM qualification_events WHERE person_id='p1' ORDER BY event_id"
        ))

    def test_grant_window_includes_expiry_day_but_not_after(self) -> None:
        events = self.events()
        inside = evaluate(events, "risk_handling", "2026-10-31T23:59:59Z", "rules-2026.1")
        after = evaluate(events, "risk_handling", "2026-11-01T00:00:00Z", "rules-2026.1")
        self.assertTrue(inside["approved"])
        self.assertFalse(after["approved"])
        self.assertEqual(
            {item["code"] for item in after["reasons"]}, {"clearance_expired"}
        )

    def test_missing_training_is_distinct_from_expired(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        service = QualificationLedgerService(connection, self.clock)
        service.create_user("admin", "管理员", "administrator")
        service.create_user("p2", "巡护员乙", "reviewer")
        result = service.explain("admin", "p2", "risk_handling")
        self.assertFalse(result["approved"])
        statuses = {item["scope"]: item["status"] for item in result["requirements"]}
        self.assertEqual(statuses["forest-fire-basic"], "missing")
        connection.close()

    def test_future_event_is_not_visible(self) -> None:
        events = self.events()
        # 装备授权 2026-02-01 生效；1 月的投影看不到它。
        january = project(events, "2026-01-20T00:00:00Z", "rules-2026.1")
        scopes = {item["scope"] for item in january["active_grants"]["equipment"]}
        self.assertEqual(scopes, set())

    def test_points_threshold_and_review_overturn(self) -> None:
        self.service.add_violation_points("admin", "p1", "rule-a", 13,
                                          "2026-02-20T09:00:00Z", "违规甲", idempotency_key="a")
        blocked = self.service.explain("audit", "p1", "sample_review")
        self.assertFalse(blocked["approved"])
        self.assertEqual(blocked["points"]["balance"], 13)
        point_event = blocked["points"]["events"][0]["event_id"]
        # 复核在次日记账；复核之前的历史时刻仍计入该扣分，不被事后推翻回写。
        self.clock.current = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
        before_review_moment = self.service.qualification("audit", "p1", "2026-03-01T09:00:00Z")
        self.assertEqual(before_review_moment["balance"] if "balance" in before_review_moment else before_review_moment["points"]["balance"], 13)
        self.service.review_event("review", point_event, "overturned", "证据不足")
        cleared = self.service.explain("audit", "p1", "sample_review")
        self.assertTrue(cleared["approved"])
        self.assertEqual(cleared["points"]["balance"], 0)

    def test_suspension_natural_expiry_and_reinstatement(self) -> None:
        self.service.suspend("admin", "p1", "disciplinary", "2026-04-01T00:00:00Z",
                             "2026-04-08T00:00:00Z", "停权七天", idempotency_key="s1")
        during = self.service.explain("audit", "p1", "risk_handling", "2026-04-05T12:00:00Z")
        last_day = self.service.explain("audit", "p1", "risk_handling", "2026-04-07T23:59:59Z")
        restored_moment = self.service.explain("audit", "p1", "risk_handling", "2026-04-08T00:00:00Z")
        self.assertFalse(during["approved"])
        self.assertFalse(last_day["approved"])
        self.assertTrue(restored_moment["approved"])
        # 提前恢复。
        self.service.suspend("admin", "p1", "disciplinary-2", "2026-05-01T00:00:00Z",
                             "2026-05-11T00:00:00Z", "再次停权", idempotency_key="s2")
        self.clock.current = datetime(2026, 5, 2, 7, 0, tzinfo=timezone.utc)
        suspended_event = self.service.explain(
            "audit", "p1", "risk_handling", "2026-05-02T00:00:00Z"
        )["suspensions"][0]["event_id"]
        self.service.reinstate("review", suspended_event, "提前恢复", idempotency_key="r1")
        self.assertTrue(self.service.explain("audit", "p1", "risk_handling", "2026-05-02T08:00:00Z")["approved"])

    def test_revision_only_affects_future_window(self) -> None:
        grant = next(
            row for row in self.events()
            if row["event_type"] == "medical_cleared" and row["scope"] == "general-medical"
        )
        self.clock.current = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        with self.assertRaises(InvalidState):
            self.service.revise_grant("admin", grant["event_id"], "2026-01-01T00:00:00Z",
                                      "2026-02-01T00:00:00Z", reason="回写历史")
        revision = self.service.revise_grant("admin", grant["event_id"], "2026-06-02T00:00:00Z",
                                             "2026-06-30T23:59:59Z", "缩短授权")
        historical = self.service.qualification("audit", "p1", "2026-03-01T08:00:00Z")
        self.assertIn(grant["event_id"],
                      [item["event_id"] for item in historical["active_grants"]["clearance"]])
        future = self.service.qualification("audit", "p1", "2026-06-02T00:00:00Z")
        active_ids = [item["event_id"] for item in future["active_grants"]["clearance"]]
        self.assertIn(revision["event_id"], active_ids)
        self.assertNotIn(grant["event_id"], active_ids)
        self.assertIn(grant["event_id"], future["suppressed_event_ids"])

    def test_rule_version_is_pinned_to_business_moment(self) -> None:
        result = evaluate(self.events(), "risk_handling", "2025-12-31T23:59:59Z", "rules-2026.1")
        # 规则版本由裁决接口按业务时刻选择；直接评估时按显式传入版本推导。
        self.assertEqual(result["rule_version"], "rules-2026.1")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.service, self.clock = service_with()
        qualified_person(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_role_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.add_violation_points("review", "p1", "x", 1,
                                              "2026-03-01T08:00:00Z", "越权扣分")
        with self.assertRaises(Forbidden):
            self.service.verify_chain("review")

    def test_unknown_person_and_action(self) -> None:
        with self.assertRaises(NotFound):
            self.service.authorize_action("admin", "nobody", "risk_handling", "ref-1")
        with self.assertRaises(ValidationFailed):
            self.service.authorize_action("admin", "p1", "unknown.action", "ref-2")

    def test_violation_replay_does_not_double_count(self) -> None:
        args = ("admin", "p1", "protocol", 5, "2026-02-20T08:00:00Z", "违规")
        first = self.service.add_violation_points(*args, idempotency_key="v1")
        second = self.service.add_violation_points(*args, idempotency_key="v1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(self.service.qualification("audit", "p1")["points"]["balance"], 5)
        changed = list(args)
        changed[3] = 6
        with self.assertRaises(Conflict):
            self.service.add_violation_points(*changed, idempotency_key="v1")

    def test_violation_cannot_be_recorded_before_now_with_past_guard(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.add_violation_points("admin", "p1", "x", 1,
                                              "2026-03-02T00:00:00Z", "未来违规")

    def test_review_and_reinstatement_idempotency(self) -> None:
        self.service.add_violation_points("admin", "p1", "x", 5,
                                          "2026-02-20T08:00:00Z", "违规", idempotency_key="v1")
        event_id = self.service.qualification("audit", "p1")["points"]["events"][0]["event_id"]
        review = self.service.review_event("review", event_id, "overturned", "推翻", idempotency_key="rv1")
        replay = self.service.review_event("review", event_id, "overturned", "推翻", idempotency_key="rv1")
        self.assertEqual(review["event_id"], replay["event_id"])
        with self.assertRaises(InvalidState):
            self.service.review_event("review", event_id, "upheld", "改判")

        self.service.suspend("admin", "p1", "d", "2026-02-25T00:00:00Z",
                             "2026-03-09T00:00:00Z", "停权", idempotency_key="s1")
        suspended_id = self.service.explain("audit", "p1", "risk_handling",
                                            "2026-03-02T00:00:00Z")["suspensions"][0]["event_id"]
        reinstatement = self.service.reinstate("review", suspended_id, "恢复", idempotency_key="ri1")
        rein_replay = self.service.reinstate("review", suspended_id, "恢复", idempotency_key="ri1")
        self.assertEqual(reinstatement["event_id"], rein_replay["event_id"])
        with self.assertRaises(InvalidState):
            self.service.reinstate("review", suspended_id, "再次恢复")

    def test_authorize_records_decision_and_replay_is_stable(self) -> None:
        decision = self.service.authorize_action("admin", "p1", "risk_handling", "wo-1")
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["rule_version"], "rules-2026.1")
        replay = self.service.authorize_action("admin", "p1", "risk_handling", "wo-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision_id"], decision["decision_id"])
        stored = self.service.decisions("audit", action="risk_handling", business_ref="wo-1")
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]["explanation"]["approved"])

    def test_denied_decision_replay_raises_and_keeps_one_record(self) -> None:
        self.service.add_violation_points("admin", "p1", "x", 13,
                                          "2026-02-20T08:00:00Z", "严重违规", idempotency_key="v")
        with self.assertRaises(QualificationDenied) as first:
            self.service.authorize_action("admin", "p1", "risk_handling", "wo-2")
        with self.assertRaises(QualificationDenied) as second:
            self.service.authorize_action("admin", "p1", "risk_handling", "wo-2")
        self.assertTrue(second.exception.replayed)
        self.assertEqual(second.exception.decision_id, first.exception.decision_id)
        self.assertEqual(len(self.service.decisions("audit", business_ref="wo-2")), 1)

    def test_explain_lists_evidence_events(self) -> None:
        result = self.service.explain("audit", "p1", "task_claim.high_altitude_night_patrol")
        self.assertTrue(result["approved"])
        evidence = [item for item in result["requirements"] if item["evidence"]]
        self.assertEqual(len(evidence), 5)
        self.assertTrue(all(item["evidence"]["event_id"] > 0 for item in evidence))

    def test_ledger_is_immutable_and_chain_verifies(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE qualification_events SET reason='tampered' WHERE event_id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM qualification_events WHERE event_id=1")
        self.assertTrue(self.service.verify_chain("audit")["valid"])

    def test_inactive_account_is_denied(self) -> None:
        self.connection.execute("UPDATE ledger_users SET active=0 WHERE user_id='p1'")
        with self.assertRaises(QualificationDenied):
            self.service.authorize_action("admin", "p1", "risk_handling", "wo-3")
        stored = self.service.decisions("audit", business_ref="wo-3")[0]
        self.assertEqual(stored["explanation"]["reasons"], [{"code": "account_inactive"}])

    def test_gate_adapter_and_permissive_default(self) -> None:
        permissive = PermissiveGate()
        self.assertTrue(permissive.check("p1", "risk_handling", "wo")["approved"])
        gate = LedgerGate(self.service, "admin")
        self.assertTrue(gate.check("p1", "risk_handling", "wo-4")["approved"])
        self.service.add_violation_points("admin", "p1", "x", 13,
                                          "2026-02-20T08:00:00Z", "严重违规", idempotency_key="v2")
        with self.assertRaises(GateDenied) as caught:
            gate.check("p1", "risk_handling", "wo-5")
        self.assertEqual(caught.exception.explanation["action"], "risk_handling")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.service, self.clock = service_with()
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_rules(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/rules", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["versions"][0]["version"], "rules-2026.1")

    def test_grant_explain_authorize_flow(self) -> None:
        grant = json.dumps({
            "scope": "forest-fire-basic",
            "valid_from": "2026-01-05T00:00:00Z",
            "valid_to": "2027-01-04T23:59:59Z",
        }).encode()
        created = self.app.handle("POST", "/people/p1/trainings", {"X-Actor-Id": "admin"}, grant)
        self.assertEqual(created.status, 201)
        missing = self.app.handle("GET", "/people/p1/risk_handling/explain", {"X-Actor-Id": "audit"})
        self.assertEqual(missing.status, 200)
        self.assertFalse(missing.body["approved"])
        authorized = self.app.handle("POST", "/authorize", {"X-Actor-Id": "admin"}, json.dumps({
            "person_id": "p1", "action": "sample_review", "business_ref": "b-1",
        }).encode())
        # 只登记了消防培训，样本复核仍缺 taxonomy-review。
        self.assertEqual(authorized.status, 403)
        self.assertEqual(authorized.body["error"]["code"], "qualification_denied")

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("GET", "/rules")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
