"""不可变资格事件账本的领域规则测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from qualification_ledger import (
    COMPETENCY_FOREST_FIRE,
    COMPETENCY_HIGH_ALTITUDE_NIGHT,
    COMPETENCY_SPECIMEN_REVIEW,
    COMPETENCY_WILDLIFE_RESCUE,
    FrozenClock,
    ImmutableLedgerError,
    QualificationConflict,
    QualificationDenied,
    QualificationLedger,
)


def ledger_at(year: int, month: int = 9, day: int = 24, hour: int = 8):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(year, month, day, hour, tzinfo=timezone.utc))
    return connection, QualificationLedger(connection, clock)


class LedgerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection, self.ledger = ledger_at(2026, 9, 24)

    def tearDown(self) -> None:
        self.connection.close()

    def grant(self, user: str, competency: str, *, training=(), medical=(), equipment=(),
              valid_from="2026-09-20T00:00:00Z"):
        for scope in training:
            self.ledger.record_training(user_id=user, competency_code=competency, scope=scope,
                                        valid_from=valid_from, idempotency_key=f"{user}-tr-{scope}", recorded_by="admin")
        for scope in medical:
            self.ledger.record_medical(user_id=user, competency_code=competency, scope=scope,
                                       valid_from=valid_from, idempotency_key=f"{user}-med-{scope}", recorded_by="admin")
        for scope in equipment:
            self.ledger.record_equipment(user_id=user, competency_code=competency, scope=scope,
                                         valid_from=valid_from, idempotency_key=f"{user}-eq-{scope}", recorded_by="admin")


class DifferentRequirementsTests(LedgerTestBase):
    def test_three_business_lines_have_distinct_requirements(self):
        fire, rescue, night = "fire-1", "rescue-1", "night-1"
        self.grant(fire, COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))
        self.grant(rescue, COMPETENCY_WILDLIFE_RESCUE,
                   training=("wildlife-rescue-basic",), medical=("field-rescue",),
                   equipment=("rescue-kit",))
        self.grant(night, COMPETENCY_HIGH_ALTITUDE_NIGHT,
                   training=("high-altitude-night-basic",), medical=("high-altitude",),
                   equipment=("night-vision", "oxygen-kit"))
        for user, competency, action in (
            (fire, COMPETENCY_FOREST_FIRE, "risk.dispose"),
            (rescue, COMPETENCY_WILDLIFE_RESCUE, "risk.dispose"),
            (night, COMPETENCY_HIGH_ALTITUDE_NIGHT, "risk.dispose"),
        ):
            result = self.ledger.authorize(action=action, user_id=user,
                                           required_competencies=frozenset({competency}))
            self.assertTrue(result.allowed, result.explanation)

    def test_specimen_review_only_needs_training(self):
        self.grant("stat-1", COMPETENCY_SPECIMEN_REVIEW, training=("taxonomy-review-basic",))
        result = self.ledger.authorize(action="specimen.review", user_id="stat-1")
        self.assertTrue(result.allowed)

    def test_forest_fire_requires_both_equipment_scopes(self):
        # 只有一套装备：缺氧气面罩，不能执行森林消防。
        self.grant("fire-2", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit",))
        result = self.ledger.authorize(action="risk.dispose", user_id="fire-2")
        self.assertFalse(result.allowed)
        missing = result.explanation["competencies"][COMPETENCY_FOREST_FIRE]["missing_requirements"]
        self.assertEqual([item["requirement"] for item in missing], ["equipment"])
        self.assertIn("breathing-apparatus", missing[0]["expected"])

    def test_wrong_medical_kind_is_rejected_at_recording(self):
        # 高海拔体检不属于森林消防的授权范围，写入账本时即被拒绝，
        # 因而不可能被误用来满足火场体检要求。
        from qualification_ledger import QualificationValidationFailed
        with self.assertRaises(QualificationValidationFailed):
            self.ledger.record_medical(user_id="fire-3", competency_code=COMPETENCY_FOREST_FIRE,
                                       scope="high-altitude", valid_from="2026-09-20T00:00:00Z",
                                       idempotency_key="bad-med", recorded_by="admin")

    def test_unknown_scope_is_rejected_at_recording(self):
        from qualification_ledger import QualificationValidationFailed
        with self.assertRaises(QualificationValidationFailed):
            self.ledger.record_equipment(user_id="fire-1", competency_code=COMPETENCY_FOREST_FIRE,
                                         scope="scuba-gear", valid_from="2026-09-20T00:00:00Z",
                                         idempotency_key="bad", recorded_by="admin")


class ExpiryAndProjectionTests(LedgerTestBase):
    def test_medical_expiry_blocks_key_action_just_before_mission(self):
        # 培训与装备在 2026-09-20 仍有效；体检登记于 2025-09-20，一年有效期
        # 已在 2026-09-20 到期——排班员在任务前核对即可发现。
        self.grant("fire-4", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=(),
                   equipment=("fire-suit", "breathing-apparatus"),
                   valid_from="2026-09-20T00:00:00Z")
        self.ledger.record_medical(user_id="fire-4", competency_code=COMPETENCY_FOREST_FIRE,
                                   scope="fire-ground", valid_from="2025-09-20T00:00:00Z",
                                   idempotency_key="fire-4-med-old", recorded_by="admin")
        result = self.ledger.authorize(action="risk.dispose", user_id="fire-4",
                                       business_moment="2026-09-24T08:00:00Z", persist=False)
        self.assertFalse(result.allowed)
        view = result.explanation["competencies"][COMPETENCY_FOREST_FIRE]
        self.assertEqual(view["state"], "expired")
        self.assertEqual(view["missing_requirements"][0]["requirement"], "medical")

    def test_future_grant_does_not_apply_before_its_valid_from(self):
        self.ledger.record_training(user_id="stat-2", competency_code=COMPETENCY_SPECIMEN_REVIEW,
                                    scope="taxonomy-review-basic", valid_from="2026-10-01T00:00:00Z",
                                    idempotency_key="future", recorded_by="admin")
        before = self.ledger.authorize(action="specimen.review", user_id="stat-2",
                                       business_moment="2026-09-24T08:00:00Z", persist=False)
        after = self.ledger.authorize(action="specimen.review", user_id="stat-2",
                                      business_moment="2026-10-02T00:00:00Z", persist=False)
        self.assertFalse(before.allowed)
        self.assertTrue(after.allowed)

    def test_later_suspension_does_not_rewrite_historical_projection(self):
        self.grant("fire-5", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))
        self.ledger.suspend(user_id="fire-5", scope=COMPETENCY_FOREST_FIRE,
                            reason="调查期间停权", valid_from="2026-10-10T00:00:00Z",
                            valid_until="2026-10-20T00:00:00Z",
                            idempotency_key="susp-1", recorded_by="admin")
        historical = self.ledger.projection("fire-5", "2026-10-01T00:00:00Z")
        future = self.ledger.projection("fire-5", "2026-10-15T00:00:00Z")
        self.assertTrue(historical.granted(COMPETENCY_FOREST_FIRE))
        self.assertEqual(future.competencies[COMPETENCY_FOREST_FIRE]["state"], "suspended")

    def test_reinstatement_lifts_suspension_for_future_only(self):
        self.grant("fire-6", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))
        suspension = self.ledger.suspend(user_id="fire-6", reason="停权复查",
                                         valid_from="2026-10-01T00:00:00Z",
                                         valid_until="2026-11-01T00:00:00Z",
                                         idempotency_key="susp-2", recorded_by="admin")
        self.ledger.reinstate(user_id="fire-6", suspension_event_id=suspension["event_id"],
                              reason="复查通过恢复", valid_from="2026-10-05T00:00:00Z",
                              idempotency_key="rein-1", recorded_by="admin")
        during = self.ledger.projection("fire-6", "2026-10-03T00:00:00Z")
        after = self.ledger.projection("fire-6", "2026-10-06T00:00:00Z")
        self.assertFalse(during.granted(COMPETENCY_FOREST_FIRE))
        self.assertTrue(after.granted(COMPETENCY_FOREST_FIRE))

    def test_double_reinstatement_is_rejected(self):
        self.ledger.suspend(user_id="fire-7", reason="停权",
                            valid_from="2026-10-01T00:00:00Z", valid_until="2026-11-01T00:00:00Z",
                            idempotency_key="susp-3", recorded_by="admin")
        first = self.ledger.reinstate(user_id="fire-7", suspension_event_id=1,
                                      reason="恢复", valid_from="2026-10-05T00:00:00Z",
                                      idempotency_key="rein-2", recorded_by="admin")
        self.assertEqual(first["event_id"], 2)
        with self.assertRaises(QualificationConflict):
            self.ledger.reinstate(user_id="fire-7", suspension_event_id=1,
                                  reason="再次恢复", valid_from="2026-10-06T00:00:00Z",
                                  idempotency_key="rein-3", recorded_by="admin")


class DemeritAndReviewTests(LedgerTestBase):
    def _qualified(self, user="fire-8"):
        self.grant(user, COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))

    def test_idempotent_demerit_replay_does_not_double_count(self):
        self._qualified()
        ids = set()
        for _ in range(3):
            event = self.ledger.record_demerit(user_id="fire-8", points=5, reason="违规进入火区",
                                               valid_from="2026-09-22T00:00:00Z",
                                               idempotency_key="dem-1", recorded_by="admin")
            ids.add(event["event_id"])
        self.assertEqual(ids, {5})  # 4 授权事件之后，扣分事件 id 为 5
        projection = self.ledger.projection("fire-8", "2026-09-24T00:00:00Z")
        self.assertEqual(projection.demerits["raw_points"], 5)
        self.assertEqual(len(self.ledger.events("fire-8")), 5)  # 4 授权 + 1 扣分

    def test_threshold_suspends_and_review_remits_for_future(self):
        self._qualified()
        self.ledger.record_demerit(user_id="fire-8", points=7, reason="违规一",
                                   valid_from="2026-09-21T00:00:00Z",
                                   idempotency_key="dem-a", recorded_by="admin")
        self.ledger.record_demerit(user_id="fire-8", points=5, reason="违规二",
                                   valid_from="2026-09-22T00:00:00Z",
                                   idempotency_key="dem-b", recorded_by="admin")
        blocked = self.ledger.projection("fire-8", "2026-09-23T00:00:00Z")
        self.assertEqual(blocked.demerits["balance"], 12)
        self.assertEqual(blocked.competencies[COMPETENCY_FOREST_FIRE]["state"], "demerit_suspended")
        with self.assertRaises(QualificationDenied):
            self.ledger.require_authorized(action="risk.dispose", user_id="fire-8",
                                           business_moment="2026-09-23T00:00:00Z")
        # 9-25 复核减免 4 分：只影响复核生效之后。
        self.ledger.review(user_id="fire-8", remit_points=4, reason="申诉部分成立",
                           valid_from="2026-09-25T00:00:00Z",
                           idempotency_key="rev-1", recorded_by="admin")
        restored = self.ledger.projection("fire-8", "2026-09-26T00:00:00Z")
        self.assertEqual(restored.demerits["balance"], 8)
        self.assertTrue(restored.granted(COMPETENCY_FOREST_FIRE))
        # 复核事件晚于历史时刻，历史停权结论不变。
        historical = self.ledger.projection("fire-8", "2026-09-23T00:00:00Z")
        self.assertEqual(historical.demerits["balance"], 12)
        self.assertFalse(historical.granted(COMPETENCY_FOREST_FIRE))

    def test_idempotent_review_replay_does_not_remit_twice(self):
        self._qualified()
        self.ledger.record_demerit(user_id="fire-8", points=12, reason="违规",
                                   valid_from="2026-09-22T00:00:00Z",
                                   idempotency_key="dem-x", recorded_by="admin")
        for _ in range(2):
            self.ledger.review(user_id="fire-8", remit_points=4, reason="减免",
                               valid_from="2026-09-25T00:00:00Z",
                               idempotency_key="rev-x", recorded_by="admin")
        projection = self.ledger.projection("fire-8", "2026-09-26T00:00:00Z")
        self.assertEqual(projection.demerits["remitted_points"], 4)
        self.assertEqual(projection.demerits["balance"], 8)


class RulesVersionTests(LedgerTestBase):
    def test_future_rule_version_only_applies_after_effective_date(self):
        # 10 分：旧版（阈值 12）放行；2027 版（阈值 8）停权。
        self.ledger.record_demerit(user_id="fire-9", points=10, reason="违规",
                                   valid_from="2026-09-20T00:00:00Z",
                                   idempotency_key="dem", recorded_by="admin")
        self.grant("fire-9", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))
        old = self.ledger.projection("fire-9", "2026-12-31T00:00:00Z")
        new = self.ledger.projection("fire-9", "2027-01-01T00:00:00Z")
        self.assertEqual(old.rules_version, "rules-2026-09-01")
        self.assertTrue(old.granted(COMPETENCY_FOREST_FIRE))
        self.assertEqual(new.rules_version, "rules-2027-01-01")
        self.assertEqual(new.competencies[COMPETENCY_FOREST_FIRE]["state"], "demerit_suspended")


class ImmutabilityAndExplanationTests(LedgerTestBase):
    def test_update_and_delete_are_blocked(self):
        event = self.ledger.record_demerit(user_id="fire-10", points=3, reason="违规",
                                           valid_from="2026-09-22T00:00:00Z",
                                           idempotency_key="dem", recorded_by="admin")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE qual_events SET points=1 WHERE event_id=?", (event["event_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM qual_events WHERE event_id=?", (event["event_id"],))

    def test_action_checks_are_immutable(self):
        self.ledger.authorize(action="specimen.review", user_id="ghost")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE qual_action_checks SET allowed=1 WHERE check_id=1")

    def test_explanation_lists_effective_events_and_rules_version(self):
        self.grant("fire-11", COMPETENCY_FOREST_FIRE,
                   training=("forest-fire-basic",), medical=("fire-ground",),
                   equipment=("fire-suit", "breathing-apparatus"))
        result = self.ledger.require_authorized(action="risk.dispose", user_id="fire-11",
                                                 idempotency_key="chk-1")
        explanation = result.explanation
        self.assertEqual(explanation["rules_version"], "rules-2026-09-01")
        effective = explanation["competencies"][COMPETENCY_FOREST_FIRE]["effective_events"]
        scopes = {(item["event_type"], item["scope"]) for item in effective}
        self.assertEqual(scopes, {
            ("training_passed", "forest-fire-basic"),
            ("medical_passed", "fire-ground"),
            ("equipment_authorized", "fire-suit"),
            ("equipment_authorized", "breathing-apparatus"),
        })
        self.assertEqual(len(explanation["considered_event_ids"]), 4)

    def test_authorization_idempotency_returns_same_check(self):
        self.grant("stat-3", COMPETENCY_SPECIMEN_REVIEW, training=("taxonomy-review-basic",))
        first = self.ledger.authorize(action="specimen.review", user_id="stat-3",
                                      idempotency_key="chk-x")
        second = self.ledger.authorize(action="specimen.review", user_id="stat-3",
                                       idempotency_key="chk-x")
        self.assertEqual(first.allowed, second.allowed)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM qual_action_checks").fetchone()[0], 1)

    def test_hash_chain_detects_tampering(self):
        self.ledger.record_demerit(user_id="fire-12", points=3, reason="违规",
                                   valid_from="2026-09-22T00:00:00Z",
                                   idempotency_key="dem", recorded_by="admin")
        self.assertTrue(self.ledger.verify_chain()["valid"])
        # 触发器阻止 UPDATE；直接绕过触发器验证哈希链自检仍能发现不一致。
        self.connection.execute("DROP TRIGGER qual_events_no_update")
        self.connection.execute("UPDATE qual_events SET reason='tampered' WHERE event_id=1")
        self.assertFalse(self.ledger.verify_chain()["valid"])


if __name__ == "__main__":
    unittest.main()
