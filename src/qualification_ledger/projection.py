"""资格投影：在任意业务时刻折叠不可变事件得到资格结论。

投影是事件账本的纯函数：给定某用户 valid_from <= as_of 的全部事件与
当时适用的规则版本，结论唯一确定。晚于 as_of 生效的修订事件不会参与
计算，因此后续修订只能影响未来有效范围，不会改变历史时刻的投影。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .clock import parse_utc
from .rules import (
    EVENT_DEMERIT,
    EVENT_EQUIPMENT,
    EVENT_MEDICAL,
    EVENT_REINSTATEMENT,
    EVENT_REVIEW,
    EVENT_SUSPENSION,
    EVENT_TRAINING,
    GLOBAL_SCOPE,
    RuleBook,
    select_rules,
)


def _event_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "competency_code": row["competency_code"],
        "scope": row["scope"],
        "valid_from": row["valid_from"],
        "valid_until": row["valid_until"],
        "points": row["points"],
        "reason": row["reason"],
        "recorded_by": row["recorded_by"],
        "recorded_at": row["recorded_at"],
        "rules_version": row["rules_version"],
        "payload": json.loads(row["payload_json"] or "{}"),
    }


def _effective_at(view: Mapping[str, Any], moment) -> bool:
    start = parse_utc(view["valid_from"], "valid_from")
    if start > moment:
        return False
    if view["valid_until"] is None:
        return True
    return parse_utc(view["valid_until"], "valid_until") > moment


@dataclass(frozen=True, slots=True)
class Projection:
    user_id: str
    as_of: str
    rules_version: str
    competencies: dict[str, dict[str, Any]]
    demerits: dict[str, Any]
    suspensions: dict[str, Any]
    event_ids: tuple[int, ...]

    def granted(self, competency: str) -> bool:
        return self.competencies[competency]["granted"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "as_of": self.as_of,
            "rules_version": self.rules_version,
            "competencies": self.competencies,
            "demerits": self.demerits,
            "suspensions": self.suspensions,
            "considered_event_ids": list(self.event_ids),
        }


def project(user_id: str, as_of: str, rows: Sequence[Mapping[str, Any]], book: RuleBook | None = None) -> Projection:
    """根据事件行计算 as_of 时刻的资格投影。

    rows 只需包含该用户 valid_from <= as_of 的事件，按
    (valid_from, event_id) 排序；调用方一般直接传入全量行。
    """

    moment = parse_utc(as_of, "as_of")
    book = book or select_rules(as_of)
    events = [_event_view(row) for row in rows if parse_utc(row["valid_from"], "valid_from") <= moment]
    events.sort(key=lambda item: (item["valid_from"], item["event_id"]))
    by_id = {item["event_id"]: item for item in events}

    # 被后续事件取代（更正）的授权事件，其有效期在取代时刻截断。
    cutoff_by_event: dict[int, Any] = {}
    for item in events:
        target = item["payload"].get("supersedes_event_id")
        if isinstance(target, int) and target in by_id:
            previous = cutoff_by_event.setdefault(target, parse_utc(item["valid_from"], "valid_from"))
            candidate = parse_utc(item["valid_from"], "valid_from")
            if candidate < previous:
                cutoff_by_event[target] = candidate

    def grant_active(item: Mapping[str, Any]) -> bool:
        if not _effective_at(item, moment):
            return False
        cutoff = cutoff_by_event.get(item["event_id"])
        return cutoff is None or cutoff > moment

    # 违规扣分滚动窗口与复核减免。
    window_start = moment - book.timedelta_window
    demerit_events: list[dict[str, Any]] = []
    total_points = 0
    for item in events:
        if item["event_type"] == EVENT_DEMERIT and parse_utc(item["valid_from"]) > window_start:
            total_points += item["points"]
            demerit_events.append({"event_id": item["event_id"], "points": item["points"],
                                   "valid_from": item["valid_from"], "reason": item["reason"]})
    review_events: list[dict[str, Any]] = []
    remitted = 0
    for item in events:
        if item["event_type"] == EVENT_REVIEW:
            remit_points = int(item["payload"].get("remit_points", 0) or 0)
            if remit_points > 0:
                remitted += remit_points
            review_events.append({"event_id": item["event_id"], "valid_from": item["valid_from"],
                                  "reason": item["reason"], "remit_points": remit_points})
    balance = max(0, total_points - remitted)
    demerit_blocked = balance >= book.suspension_threshold

    # 停权区间：恢复事件在其生效时刻截断对应的临时停权。
    active_suspensions: list[dict[str, Any]] = []
    lifted_suspensions: list[dict[str, Any]] = []
    for item in events:
        if item["event_type"] != EVENT_SUSPENSION:
            continue
        end = None if item["valid_until"] is None else parse_utc(item["valid_until"], "valid_until")
        reinstated_at = None
        for later in events:
            if later["event_type"] == EVENT_REINSTATEMENT and later["payload"].get("supersedes_event_id") == item["event_id"]:
                reinstated_at = parse_utc(later["valid_from"], "valid_from")
                end = end if end is not None and end < reinstated_at else reinstated_at
        view = {"event_id": item["event_id"], "scope": item["scope"],
                "valid_from": item["valid_from"], "valid_until": item["valid_until"],
                "reason": item["reason"]}
        started = parse_utc(item["valid_from"]) <= moment
        still_active = started and (end is None or end > moment)
        if still_active:
            active_suspensions.append(view)
        elif reinstated_at is not None and reinstated_at <= moment:
            lifted_suspensions.append({**view, "reinstated_at": _format_lift(reinstated_at)})

    def suspension_hits(code: str) -> list[dict[str, Any]]:
        return [item for item in active_suspensions if item["scope"] in (GLOBAL_SCOPE, code)]

    competencies: dict[str, dict[str, Any]] = {}
    for code, requirement in book.requirements.items():
        effective_events: list[dict[str, Any]] = []
        expired_events: list[dict[str, Any]] = []
        for item in events:
            if item["competency_code"] != code or item["event_type"] not in {
                EVENT_TRAINING, EVENT_MEDICAL, EVENT_EQUIPMENT
            }:
                continue
            entry = {k: item[k] for k in ("event_id", "event_type", "scope", "valid_from", "valid_until")}
            (effective_events if grant_active(item) else expired_events).append(entry)

        def covered(event_type: str, scopes: Iterable[str], require_all: bool) -> tuple[bool, list[str]]:
            wanted = set(scopes)
            held = {item["scope"] for item in effective_events
                    if item["event_type"] == event_type and (item["scope"] == GLOBAL_SCOPE or item["scope"] in wanted)}
            if GLOBAL_SCOPE in held:
                held = set(wanted)
            missing = sorted(wanted - held)
            if not wanted:
                return True, []
            return (not missing) if require_all else bool(held), missing

        training_ok, missing_training = covered(EVENT_TRAINING, requirement.training_codes, False)
        medical_ok, missing_medical = covered(EVENT_MEDICAL, requirement.medical_kinds, False)
        equipment_ok, missing_equipment = covered(EVENT_EQUIPMENT, requirement.equipment_scopes, True)

        missing: list[dict[str, str]] = []
        if not training_ok:
            missing.append({"requirement": "training", "expected": ",".join(sorted(requirement.training_codes))})
        if not medical_ok:
            missing.append({"requirement": "medical", "expected": ",".join(sorted(requirement.medical_kinds))})
        if not equipment_ok:
            missing.append({"requirement": "equipment", "expected": ",".join(sorted(missing_equipment))})

        suspended = suspension_hits(code)
        if suspended:
            state = "suspended"
            granted = False
        elif demerit_blocked:
            state = "demerit_suspended"
            granted = False
        elif not (training_ok and medical_ok and equipment_ok):
            state = "expired" if expired_events else "missing"
            granted = False
        else:
            state = "granted"
            granted = True

        competencies[code] = {
            "label": requirement.label,
            "granted": granted,
            "state": state,
            "effective_events": effective_events,
            "expired_or_superseded_events": expired_events,
            "missing_requirements": missing,
            "active_suspensions": suspended,
        }

    demerits = {
        "balance": balance,
        "raw_points": total_points,
        "remitted_points": remitted,
        "threshold": book.suspension_threshold,
        "window_days": book.demerit_window_days,
        "window_start": _format_lift(window_start),
        "events": demerit_events,
        "reviews": review_events,
        "blocked": demerit_blocked,
    }
    suspensions = {"active": active_suspensions, "lifted": lifted_suspensions}
    return Projection(
        user_id=user_id,
        as_of=as_of,
        rules_version=book.version,
        competencies=competencies,
        demerits=demerits,
        suspensions=suspensions,
        event_ids=tuple(item["event_id"] for item in events),
    )


def _format_lift(value) -> str:
    from .clock import utc_text

    return utc_text(value)
