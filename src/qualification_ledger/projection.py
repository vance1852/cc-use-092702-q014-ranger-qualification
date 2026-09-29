"""资格投影：在给定业务时刻从不可变事件推导资格状态。

投影是纯函数：输入某人的事件行和业务时刻 T，输出 T 时刻的资格结论。
只采用 recorded_at <= T 的事件，因此事件在记账之后不能改变任何历史时刻
（包括历史排班事实）的结论；修订事件从其自身 valid_from 起生效，
更早的区间仍由被修订事件支撑。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .rules import ActionRule, action_rule


GRANT_TYPES = ("training_passed", "medical_cleared", "equipment_authorized")
CATEGORY = {
    "training_passed": "training",
    "medical_cleared": "clearance",
    "equipment_authorized": "equipment",
}


def _active(valid_from: str, valid_to: str | None, as_of: str) -> bool:
    if valid_from > as_of:
        return False
    return valid_to is None or valid_to >= as_of


def project(events: Sequence[Mapping[str, Any]], as_of: str, rule_version: str) -> dict[str, Any]:
    """计算 as_of 时刻的资格投影。

    事件行至少包含 storage.SCHEMA 中 qualification_events 的全部列。
    """

    visible = [event for event in events if event["recorded_at"] <= as_of]

    # 修订事件（同类资格事件且指向原事件）从自身 valid_from 起取代原事件；
    # 尚未到生效点的修订不影响投影，原事件继续支撑当前区间。
    suppressed: set[int] = set()
    for event in visible:
        amended = event["amends_event_id"]
        if (
            amended is not None
            and event["event_type"] in GRANT_TYPES
            and event["valid_from"] <= as_of
        ):
            suppressed.add(amended)

    grants: dict[str, list[dict[str, Any]]] = {
        "training": [],
        "clearance": [],
        "equipment": [],
    }
    for event in visible:
        if event["event_type"] not in GRANT_TYPES:
            continue
        if event["event_id"] in suppressed:
            continue
        if not _active(event["valid_from"], event["valid_to"], as_of):
            continue
        grants[CATEGORY[event["event_type"]]].append(
            {
                "scope": event["scope"],
                "event_id": event["event_id"],
                "valid_from": event["valid_from"],
                "valid_to": event["valid_to"],
            }
        )

    reviews = {
        event["amends_event_id"]: event
        for event in visible
        if event["event_type"] == "review" and event["amends_event_id"] is not None
    }
    reinstatements = [
        event
        for event in visible
        if event["event_type"] == "reinstatement" and event["amends_event_id"] is not None
    ]

    # 违规扣分：复核推翻的扣分项自复核时刻起不再计入，历史余额保持不变。
    point_events: list[dict[str, Any]] = []
    for event in visible:
        if event["event_type"] != "violation_points" or event["valid_from"] > as_of:
            continue
        review = reviews.get(event["event_id"])
        if review is not None and review["scope"] == "overturned" and review["valid_from"] <= as_of:
            continue
        point_events.append(
            {
                "event_id": event["event_id"],
                "scope": event["scope"],
                "points": int(event["points"]),
                "valid_from": event["valid_from"],
                "reason": event["reason"],
            }
        )
    points_balance = sum(item["points"] for item in point_events)

    # 临时停权：valid_to 是预计恢复时刻（该时刻起已恢复，不含端点）；
    # 复核推翻或恢复事件同样在其发生时刻立即结束停权。
    suspensions: list[dict[str, Any]] = []
    for event in visible:
        if event["event_type"] != "suspension" or event["valid_from"] > as_of:
            continue
        interrupts: list[str] = []
        if event["valid_to"] is not None:
            interrupts.append(event["valid_to"])
        review = reviews.get(event["event_id"])
        if review is not None and review["scope"] == "overturned" and review["valid_from"] <= as_of:
            interrupts.append(review["valid_from"])
        reinstated_by = None
        for reinstatement in reinstatements:
            if (
                reinstatement["amends_event_id"] == event["event_id"]
                and reinstatement["valid_from"] <= as_of
            ):
                interrupts.append(reinstatement["valid_from"])
                reinstated_by = reinstatement["event_id"]
        active = all(moment > as_of for moment in interrupts)
        if active:
            suspensions.append(
                {
                    "event_id": event["event_id"],
                    "scope": event["scope"],
                    "valid_from": event["valid_from"],
                    "reason": event["reason"],
                    "reinstated_by_event_id": reinstated_by,
                }
            )

    return {
        "as_of": as_of,
        "rule_version": rule_version,
        "active_grants": grants,
        "points": {"balance": points_balance, "events": point_events},
        "active_suspensions": suspensions,
        "considered_event_ids": [event["event_id"] for event in visible],
        "suppressed_event_ids": sorted(suppressed),
    }

def evaluate(
    events: Sequence[Mapping[str, Any]], action: str, as_of: str, rule_version: str
) -> dict[str, Any]:
    """在投影之上套用某规则版本的动作准入要求，给出可解释结论。"""

    rule: ActionRule = action_rule(rule_version, action)
    projection = project(events, as_of, rule_version)
    requirements: list[dict[str, Any]] = []

    def requirement(category: str, scope: str) -> None:
        entries = projection["active_grants"][category]
        match = next((item for item in entries if item["scope"] == scope), None)
        if match is not None:
            status = "satisfied"
        else:
            status = _inactive_status(events, category, scope, as_of)
        requirements.append(
            {
                "category": category,
                "scope": scope,
                "status": status,
                "evidence": None if match is None else {key: match[key] for key in ("event_id", "valid_from", "valid_to")},
            }
        )

    for scope in sorted(rule.trainings):
        requirement("training", scope)
    for scope in sorted(rule.clearances):
        requirement("clearance", scope)
    for scope in sorted(rule.equipment):
        requirement("equipment", scope)

    reasons: list[dict[str, Any]] = []
    for item in requirements:
        if item["status"] != "satisfied":
            reasons.append(
                {
                    "code": f"{item['category']}_{item['status']}",
                    "scope": item["scope"],
                }
            )

    balance = projection["points"]["balance"]
    if balance > rule.max_points:
        reasons.append(
            {
                "code": "points_balance_exceeded",
                "balance": balance,
                "limit": rule.max_points,
                "evidence_event_ids": [item["event_id"] for item in projection["points"]["events"]],
            }
        )

    if projection["active_suspensions"]:
        reasons.append(
            {
                "code": "active_suspension",
                "evidence_event_ids": [item["event_id"] for item in projection["active_suspensions"]],
            }
        )

    return {
        "action": action,
        "approved": not reasons,
        "rule_version": rule_version,
        "business_at": as_of,
        "requirements": requirements,
        "points": projection["points"],
        "suspensions": projection["active_suspensions"],
        "reasons": reasons,
        "considered_event_ids": projection["considered_event_ids"],
        "suppressed_event_ids": projection["suppressed_event_ids"],
    }


def _inactive_status(
    events: Sequence[Mapping[str, Any]], category: str, scope: str, as_of: str
) -> str:
    """区分从未取得授权与授权已过期，便于排班员提前发现。"""

    wanted_type = next(name for name, value in CATEGORY.items() if value == category)
    for event in events:
        if (
            event["event_type"] == wanted_type
            and event["scope"] == scope
            and event["recorded_at"] <= as_of
            and event["valid_from"] <= as_of
            and event["valid_to"] is not None
            and event["valid_to"] < as_of
        ):
            return "expired"
    return "missing"
