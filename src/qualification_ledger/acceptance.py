"""资格事件账本离线验收：覆盖三类任务线、有效期、扣分、停权与恢复、
复核、修订只影响未来、关键动作核对留痕、重放幂等与账本防篡改。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState, QualificationDenied
from .service import QualificationLedgerService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc))
    service = QualificationLedgerService(connection, clock)

    for user_id, role in (("admin", "administrator"), ("review", "reviewer"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)

    # 三条任务线的巡护人员：消防、救护、高海拔夜巡。
    for person in ("fire-01", "rescue-01", "night-01"):
        service.create_user(person, person, "reviewer")

    # 1) 培训通过与体检合格（一年有效），夜巡额外需要高海拔体检与装备授权。
    service.record_grant("admin", "training_passed", "fire-01", "forest-fire-basic",
                         "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z", "春季集训通过")
    service.record_grant("admin", "training_passed", "fire-01", "wilderness-first-aid",
                         "2026-01-05T00:00:00Z", "2027-01-04T23:59:59Z")
    service.record_grant("admin", "medical_cleared", "fire-01", "general-medical",
                         "2026-02-01T00:00:00Z", "2026-08-31T23:59:59Z", "年度体检")
    service.record_grant("admin", "training_passed", "rescue-01", "wildlife-rescue",
                         "2026-01-06T00:00:00Z", "2027-01-05T23:59:59Z")
    service.record_grant("admin", "training_passed", "rescue-01", "wilderness-first-aid",
                         "2026-01-06T00:00:00Z", "2027-01-05T23:59:59Z")
    service.record_grant("admin", "medical_cleared", "rescue-01", "general-medical",
                         "2026-02-01T00:00:00Z", "2027-01-31T23:59:59Z")
    service.record_grant("admin", "training_passed", "rescue-01", "taxonomy-review",
                         "2026-01-06T00:00:00Z", "2027-01-05T23:59:59Z", "复核员培训")
    service.record_grant("admin", "training_passed", "rescue-01", "equipment-ops",
                         "2026-01-06T00:00:00Z", "2027-01-05T23:59:59Z", "应急装备操作培训")
    service.record_grant("admin", "equipment_authorized", "rescue-01", "protective-gear",
                         "2026-02-01T00:00:00Z", "2027-01-31T23:59:59Z", "防护装备授权")
    for scope in ("high-altitude-night-patrol", "wilderness-first-aid"):
        service.record_grant("admin", "training_passed", "night-01", scope,
                             "2026-01-07T00:00:00Z", "2027-01-06T23:59:59Z")
    service.record_grant("admin", "medical_cleared", "night-01", "high-altitude-medical",
                         "2026-02-01T00:00:00Z", "2026-10-31T23:59:59Z")
    service.record_grant("admin", "equipment_authorized", "night-01", "night-optics",
                         "2026-02-01T00:00:00Z", "2027-01-31T23:59:59Z")
    service.record_grant("admin", "equipment_authorized", "night-01", "high-altitude-gear",
                         "2026-02-01T00:00:00Z", "2027-01-31T23:59:59Z")

    # 2) 当前时刻三类任务领取都应通过，裁决留痕。
    fire_claim = service.authorize_action("admin", "fire-01", "task_claim.forest_fire", "shift-0301")
    rescue_claim = service.authorize_action("admin", "rescue-01", "task_claim.wildlife_rescue", "shift-0301")
    night_claim = service.authorize_action("admin", "night-01", "task_claim.high_altitude_night_patrol", "shift-0301")

    # 3) 同键重放返回同一裁决，不产生新记录。
    replayed = service.authorize_action("admin", "fire-01", "task_claim.forest_fire", "shift-0301")
    assert replayed["replayed"] and replayed["decision_id"] == fire_claim["decision_id"]

    # 4) 解释结论：列出生效事件与规则版本。
    explanation = service.explain("audit", "night-01", "task_claim.high_altitude_night_patrol")
    assert explanation["approved"] and explanation["rule_version"] == "rules-2026.1"
    assert all(item["status"] == "satisfied" for item in explanation["requirements"])
    assert len(explanation["considered_event_ids"]) == 5

    # 5) 到 9 月，消防员的年度体检已过期：任务开始前即可发现资格缺口。
    clock.current = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    expired = service.explain("audit", "fire-01", "task_claim.forest_fire")
    assert not expired["approved"]
    medical_requirement = next(item for item in expired["requirements"] if item["scope"] == "general-medical")
    assert medical_requirement["status"] == "expired"

    # 6) 历史排班事实不被未来修订回写：先记录 3 月 1 日的投影，
    #    之后把消防培训有效期缩短到 2 月底之前——修订只影响修订时刻之后。
    historical = service.qualification("audit", "fire-01", "2026-03-01T08:00:00Z")
    historical_training = next(
        grant for grant in historical["active_grants"]["training"] if grant["scope"] == "forest-fire-basic"
    )
    fire_training_event = historical_training["event_id"]
    denied_future_amend = None
    try:
        service.revise_grant("admin", fire_training_event, "2026-02-01T00:00:00Z",
                             "2026-12-31T23:59:59Z", reason="尝试回写历史")
    except InvalidState as exc:
        denied_future_amend = str(exc)
    revision = service.revise_grant("admin", fire_training_event, "2026-09-16T00:00:00Z",
                                    "2026-12-31T23:59:59Z", "年度复训前缩短授权")
    historical_after = service.qualification("audit", "fire-01", "2026-03-01T08:00:00Z")
    assert historical_after["active_grants"]["training"] == historical["active_grants"]["training"]

    # 7) 违规扣分累计超限（阈值 12），且同一违规事件重放不重复扣分。
    service.add_violation_points("admin", "rescue-01", "equipment-mishandling", 6,
                                 "2026-04-10T09:00:00Z", "装备操作违规", idempotency_key="vio-1")
    first_vio = service.add_violation_points("admin", "rescue-01", "equipment-mishandling", 6,
                                             "2026-04-10T09:00:00Z", "装备操作违规", idempotency_key="vio-1")
    assert first_vio.get("replayed")
    service.add_violation_points("admin", "rescue-01", "protocol-breach", 7,
                                 "2026-05-02T10:00:00Z", "救护流程违规", idempotency_key="vio-2")
    blocked = service.explain("audit", "rescue-01", "task_claim.wildlife_rescue")
    assert not blocked["approved"]
    assert any(reason["code"] == "points_balance_exceeded" for reason in blocked["reasons"])
    # 此刻发起的资源调拨被拒，裁决快照落账；这就是被保留的历史事实。
    allocation_denied = False
    try:
        service.authorize_action("admin", "rescue-01", "resource_allocation", "alloc-historical",
                                 business_at="2026-09-15T06:00:00Z", idempotency_key="alloc-historical")
    except QualificationDenied:
        allocation_denied = True
    assert allocation_denied
    denied_decision = next(iter(service.decisions("audit", business_ref="alloc-historical")))
    assert denied_decision["approved"] == 0

    # 8) 复核推翻第二项扣分后，余额回到 6，重新满足阈值。
    service.review_event("review", blocked["points"]["events"][1]["event_id"], "overturned",
                         "监控证实流程合规")
    restored = service.explain("audit", "rescue-01", "task_claim.wildlife_rescue")
    assert restored["approved"] and restored["points"]["balance"] == 6
    # 复核结论不可更改。
    try:
        service.review_event("review", blocked["points"]["events"][1]["event_id"], "upheld", "重复复核")
        raise AssertionError("重复复核必须被拒绝")
    except InvalidState:
        pass
    # 复核之后的新调拨核对通过；未来修订没有改变上面的历史被拒裁决。
    allocation = service.authorize_action("admin", "rescue-01", "resource_allocation", "alloc-0916",
                                          business_at="2026-09-16T08:00:00Z", idempotency_key="alloc-0916")
    assert allocation["approved"]
    # 历史被拒裁决重放：仍被拒且 replayed 标记为真，不产生第二条裁决。
    try:
        service.authorize_action("admin", "rescue-01", "resource_allocation", "alloc-historical",
                                 business_at="2026-09-15T06:00:00Z", idempotency_key="alloc-historical")
        raise AssertionError("被拒裁决重放必须仍是被拒")
    except QualificationDenied as exc:
        assert exc.replayed and exc.decision_id == denied_decision["decision_id"]
    assert len(service.decisions("audit", business_ref="alloc-historical")) == 1

    # 9) 临时停权在生效区间内阻止动作；提前恢复后放行；恢复重放不产生第二条恢复事件。
    service.suspend("admin", "night-01", "disciplinary", "2026-09-20T00:00:00Z",
                    "2026-09-30T23:59:59Z", "调查期间暂停夜巡")
    clock.current = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    suspended = service.explain("audit", "night-01", "task_claim.high_altitude_night_patrol")
    assert not suspended["approved"]
    try:
        service.authorize_action("admin", "night-01", "task_claim.high_altitude_night_patrol", "shift-0921",
                                 idempotency_key="claim-0921")
        raise AssertionError("停权期间任务领取必须被拒绝")
    except QualificationDenied:
        pass
    suspension_event = suspended["suspensions"][0]["event_id"]
    clock.advance(minutes=30)
    reinstatement = service.reinstate("review", suspension_event, "调查结束恢复", idempotency_key="rein-1")
    reinstatement_replay = service.reinstate("review", suspension_event, "调查结束恢复", idempotency_key="rein-1")
    assert reinstatement_replay.get("event_id") == reinstatement["event_id"]
    reinstated = service.explain("audit", "night-01", "task_claim.high_altitude_night_patrol")
    assert reinstated["approved"]
    # 恢复事件记账之前的历史被拒裁决重放仍为被拒。
    try:
        service.authorize_action("admin", "night-01", "task_claim.high_altitude_night_patrol", "shift-0921",
                                 business_at="2026-09-21T08:00:00Z", idempotency_key="claim-0921")
        raise AssertionError("恢复不回写历史裁决")
    except QualificationDenied as exc:
        assert exc.replayed

    # 10) 其余关键动作经网关核对：样本复核通过；风险处置在停权时刻（恢复事件记账前）被拒。
    sample = service.authorize_action("admin", "rescue-01", "sample_review", "sample-batch-9",
                                      business_at="2026-09-16T08:00:00Z", idempotency_key="sample-9")
    assert sample["approved"]
    risk_denied = None
    try:
        service.authorize_action("admin", "night-01", "risk_handling", "wo-0921",
                                 business_at="2026-09-21T08:00:00Z", idempotency_key="risk-1")
    except QualificationDenied as exc:
        risk_denied = str(exc)

    # 11) 账本不可变：直接 UPDATE/DELETE 必须被触发器拒绝。
    immutable_checks = {"update_blocked": False, "delete_blocked": False}
    try:
        connection.execute("UPDATE qualification_events SET reason='x' WHERE event_id=1")
    except sqlite3.IntegrityError:
        immutable_checks["update_blocked"] = True
    try:
        connection.execute("DELETE FROM qualification_events WHERE event_id=1")
    except sqlite3.IntegrityError:
        immutable_checks["delete_blocked"] = True

    chain = service.verify_chain("audit")
    decisions = service.decisions("audit")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "claims": {"fire": fire_claim["approved"], "rescue": rescue_claim["approved"], "night": night_claim["approved"]},
        "replay_decision_id": replayed["decision_id"],
        "expired_gap_codes": [item["status"] for item in expired["requirements"] if item["status"] != "satisfied"],
        "revision_event_id": revision["event_id"],
        "amend_rejected": denied_future_amend is not None,
        "historical_training_unchanged": historical_after["active_grants"]["training"] == historical["active_grants"]["training"],
        "points_after_review": restored["points"]["balance"],
        "reinstatement_event_id": reinstatement["event_id"],
        "sample_review_decision_id": sample["decision_id"],
        "risk_denied_while_suspended": risk_denied is not None,
        "allocation_decision_id": allocation["decision_id"],
        "immutable": immutable_checks,
        "decisions_recorded": len(decisions),
        "rule_version": explanation["rule_version"],
        "explanation_requirements": explanation["requirements"],
        "audit": chain,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行巡护人员资格事件账本离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
