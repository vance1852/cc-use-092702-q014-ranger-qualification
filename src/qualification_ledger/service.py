"""资格事件账本应用服务。

职责：
- 只追加地登记培训通过、体检、装备授权、违规扣分、临时停权、复核、恢复；
- 同一幂等键重放返回同一事件，不重复扣分或恢复；
- 在关键业务动作发生时按注入时钟的业务时刻核对资格，并落库一条
  不可变的核对事实（含解释：由哪些有效事件和哪个规则版本推导）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import (
    QualificationConflict,
    QualificationDenied,
    QualificationNotFound,
    QualificationValidationFailed,
)
from .projection import Projection, project
from .rules import (
    ACTION_COMPETENCIES,
    EVENT_DEMERIT,
    EVENT_EQUIPMENT,
    EVENT_MEDICAL,
    EVENT_REINSTATEMENT,
    EVENT_REVIEW,
    EVENT_SUSPENSION,
    EVENT_TRAINING,
    GRANT_EVENTS,
    GLOBAL_SCOPE,
    select_rules,
)
from .store import GENESIS_HASH, attach_schema, transaction


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class QualificationResult:
    allowed: bool
    action: str
    user_id: str
    business_moment: str
    rules_version: str
    granted_competencies: tuple[str, ...]
    denied_competencies: tuple[str, ...]
    explanation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "user_id": self.user_id,
            "business_moment": self.business_moment,
            "rules_version": self.rules_version,
            "granted_competencies": list(self.granted_competencies),
            "denied_competencies": list(self.denied_competencies),
            "explanation": self.explanation,
        }


class QualificationLedger:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        attach_schema(connection)

    def set_clock(self, clock) -> None:
        self.clock = clock

    # ------------------------------------------------------------------ #
    # 事件登记
    # ------------------------------------------------------------------ #
    def _now_text(self) -> str:
        return utc_text(self.clock.now())

    @staticmethod
    def _validate_time(value: str, field: str) -> str:
        try:
            return utc_text(parse_utc(value, field))
        except ValueError as exc:
            raise QualificationValidationFailed(str(exc)) from exc

    def record_event(
        self,
        *,
        user_id: str,
        event_type: str,
        idempotency_key: str,
        recorded_by: str,
        competency_code: str | None = None,
        scope: str = GLOBAL_SCOPE,
        valid_from: str | None = None,
        valid_until: str | None = None,
        points: int = 0,
        reason: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一个资格事件；幂等键重放返回原事件，不产生新记录。"""

        if not user_id.strip():
            raise QualificationValidationFailed("user_id 不能为空")
        if not idempotency_key.strip():
            raise QualificationValidationFailed("idempotency_key 不能为空")
        if event_type not in {*GRANT_EVENTS, EVENT_DEMERIT, EVENT_SUSPENSION, EVENT_REVIEW, EVENT_REINSTATEMENT}:
            raise QualificationValidationFailed(f"未知事件类型: {event_type}")

        body_payload = dict(payload or {})
        now_text = self._now_text()
        start_text = self._validate_time(valid_from or now_text, "valid_from")
        until_text = None if valid_until is None else self._validate_time(valid_until, "valid_until")
        if until_text is not None and until_text <= start_text:
            raise QualificationValidationFailed("valid_until 必须晚于 valid_from")
        if event_type in GRANT_EVENTS and not competency_code:
            raise QualificationValidationFailed("授权事件必须指定 competency_code")
        if event_type == EVENT_DEMERIT and points <= 0:
            raise QualificationValidationFailed("扣分事件 points 必须为正整数")
        if event_type == EVENT_SUSPENSION and not reason.strip():
            raise QualificationValidationFailed("临时停权必须填写原因")
        if event_type == EVENT_REINSTATEMENT:
            target = body_payload.get("supersedes_event_id")
            if not isinstance(target, int):
                raise QualificationValidationFailed("恢复事件必须在 payload.supersedes_event_id 指明停权事件")

        # 登记时依据事件生效时刻的规则版本校验授权范围与有效期上限。
        book = select_rules(start_text)
        if competency_code and event_type in GRANT_EVENTS:
            requirement = book.requirements.get(competency_code)
            if requirement is None:
                raise QualificationValidationFailed(f"规则 {book.version} 未定义资格 {competency_code}")
            allowed_scopes = {
                EVENT_TRAINING: requirement.training_codes,
                EVENT_MEDICAL: requirement.medical_kinds,
                EVENT_EQUIPMENT: requirement.equipment_scopes,
            }[event_type]
            if not allowed_scopes:
                raise QualificationValidationFailed(f"{requirement.label} 不要求 {event_type} 授权")
            if scope not in allowed_scopes:
                raise QualificationValidationFailed(
                    f"scope {scope} 不在规则 {book.version} 的 {requirement.label}/{event_type} 授权范围内"
                )
            max_days = book.validity_days[event_type]
            latest_end = utc_text(parse_utc(start_text) + timedelta(days=max_days))
            if until_text is None or until_text > latest_end:
                until_text = latest_end

        # 幂等重放：同一用户 + 幂等键必须对应同一事件。
        existing = self.connection.execute(
            "SELECT * FROM qual_events WHERE user_id=? AND idempotency_key=?",
            (user_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["event_type"] != event_type:
                raise QualificationConflict("幂等键对应不同资格事件类型")
            return self._event_dict(existing, replayed=True)

        supersedes = body_payload.get("supersedes_event_id")
        rules_version = body_payload.get("rules_version") or book.version
        stored_payload = dict(body_payload)
        stored_payload.setdefault("supersedes_event_id", supersedes if isinstance(supersedes, int) else None)

        try:
            with transaction(self.connection):
                head = self.connection.execute(
                    "SELECT event_hash FROM qual_events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
                previous_hash = GENESIS_HASH if head is None else head["event_hash"]
                chain_body = {
                    "user_id": user_id,
                    "event_type": event_type,
                    "competency_code": competency_code,
                    "scope": scope,
                    "valid_from": start_text,
                    "valid_until": until_text,
                    "points": points,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "recorded_by": recorded_by,
                    "payload": stored_payload,
                    "previous_hash": previous_hash,
                }
                event_hash = hashlib.sha256(_canonical(chain_body).encode("utf-8")).hexdigest()
                cursor = self.connection.execute(
                    "INSERT INTO qual_events(user_id,event_type,competency_code,scope,valid_from,valid_until,"
                    "points,reason,idempotency_key,recorded_by,recorded_at,supersedes_event_id,rules_version,"
                    "payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, event_type, competency_code, scope, start_text, until_text,
                        points, reason, idempotency_key, recorded_by, now_text,
                        supersedes if isinstance(supersedes, int) else None,
                        rules_version, _canonical(stored_payload), previous_hash, event_hash,
                    ),
                )
                event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise QualificationConflict("资格事件幂等键并发冲突") from exc
        row = self.connection.execute("SELECT * FROM qual_events WHERE event_id=?", (event_id,)).fetchone()
        return self._event_dict(row, replayed=False)

    def record_training(self, **kwargs: Any) -> dict[str, Any]:
        return self.record_event(event_type=EVENT_TRAINING, **kwargs)

    def record_medical(self, **kwargs: Any) -> dict[str, Any]:
        return self.record_event(event_type=EVENT_MEDICAL, **kwargs)

    def record_equipment(self, **kwargs: Any) -> dict[str, Any]:
        return self.record_event(event_type=EVENT_EQUIPMENT, **kwargs)

    def record_demerit(self, *, user_id: str, points: int, reason: str, idempotency_key: str,
                       recorded_by: str, valid_from: str | None = None, competency_code: str | None = None) -> dict[str, Any]:
        return self.record_event(
            user_id=user_id, event_type=EVENT_DEMERIT, points=points, reason=reason,
            idempotency_key=idempotency_key, recorded_by=recorded_by,
            valid_from=valid_from, competency_code=competency_code,
        )

    def suspend(self, *, user_id: str, reason: str, idempotency_key: str, recorded_by: str,
                scope: str = GLOBAL_SCOPE, valid_from: str | None = None, valid_until: str | None = None) -> dict[str, Any]:
        return self.record_event(
            user_id=user_id, event_type=EVENT_SUSPENSION, reason=reason, scope=scope,
            idempotency_key=idempotency_key, recorded_by=recorded_by,
            valid_from=valid_from, valid_until=valid_until,
        )

    def review(self, *, user_id: str, idempotency_key: str, recorded_by: str, reason: str = "",
               remit_points: int = 0, valid_from: str | None = None) -> dict[str, Any]:
        """登记复核结论；remit_points 为经复核减免的扣分。"""

        if remit_points < 0:
            raise QualificationValidationFailed("remit_points 不能为负")
        return self.record_event(
            user_id=user_id, event_type=EVENT_REVIEW, reason=reason,
            idempotency_key=idempotency_key, recorded_by=recorded_by, valid_from=valid_from,
            payload={"remit_points": remit_points},
        )

    def reinstate(self, *, user_id: str, suspension_event_id: int, idempotency_key: str,
                  recorded_by: str, reason: str, valid_from: str | None = None) -> dict[str, Any]:
        """恢复被临时停权的资格；同一停权事件只能恢复一次。"""

        target = self.connection.execute(
            "SELECT * FROM qual_events WHERE event_id=? AND user_id=?",
            (suspension_event_id, user_id),
        ).fetchone()
        if target is None:
            raise QualificationNotFound("待恢复的停权事件不存在")
        if target["event_type"] != EVENT_SUSPENSION:
            raise QualificationValidationFailed("只能针对临时停权事件登记恢复")
        duplicate = self.connection.execute(
            "SELECT event_id FROM qual_events WHERE user_id=? AND event_type=? "
            "AND json_extract(payload_json,'$.supersedes_event_id')=?",
            (user_id, EVENT_REINSTATEMENT, suspension_event_id),
        ).fetchone()
        if duplicate is not None:
            raise QualificationConflict("该停权事件已经恢复，重复恢复不会再次生效")
        return self.record_event(
            user_id=user_id, event_type=EVENT_REINSTATEMENT, reason=reason,
            idempotency_key=idempotency_key, recorded_by=recorded_by, valid_from=valid_from,
            payload={"supersedes_event_id": suspension_event_id},
        )

    # ------------------------------------------------------------------ #
    # 查询与投影
    # ------------------------------------------------------------------ #
    def _user_rows(self, user_id: str) -> Sequence[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM qual_events WHERE user_id=? ORDER BY valid_from,event_id", (user_id,)
        ).fetchall()
        return rows

    def projection(self, user_id: str, as_of: str | None = None) -> Projection:
        moment = self._validate_time(as_of or self._now_text(), "as_of")
        return project(user_id, moment, self._user_rows(user_id))

    def qualification(self, user_id: str, competency: str, as_of: str | None = None) -> dict[str, Any]:
        projection = self.projection(user_id, as_of)
        if competency not in projection.competencies:
            raise QualificationNotFound(f"资格不存在: {competency}")
        return {
            "user_id": user_id,
            "competency": competency,
            "as_of": projection.as_of,
            "rules_version": projection.rules_version,
            **projection.competencies[competency],
            "demerits": projection.demerits,
        }

    def events(self, user_id: str) -> list[dict[str, Any]]:
        return [self._event_dict(row) for row in self._user_rows(user_id)]

    # ------------------------------------------------------------------ #
    # 关键动作资格核对
    # ------------------------------------------------------------------ #
    def authorize(
        self,
        *,
        action: str,
        user_id: str,
        required_competencies: Sequence[str] | frozenset[str] | None = None,
        business_moment: str | None = None,
        context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        persist: bool = True,
    ) -> QualificationResult:
        """在关键动作发生时核对资格。

        - 必需资格默认取 ACTION_COMPETENCIES[action]，可显式覆盖；
        - business_moment 默认取注入时钟的当前时刻；
        - 核对结论（含解释）作为不可变事实落库；相同 idempotency_key
          重放返回原结论，不重复记录。
        """

        required = frozenset(required_competencies or ACTION_COMPETENCIES.get(action, frozenset()))
        if not required:
            raise QualificationValidationFailed(f"动作 {action} 未声明所需资格")
        moment_text = self._validate_time(business_moment or self._now_text(), "business_moment")

        if idempotency_key:
            prior = self.connection.execute(
                "SELECT * FROM qual_action_checks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prior is not None:
                return self._result_from_check(prior)

        projection = self.project_raw(user_id, moment_text)
        granted = tuple(sorted(code for code in required if projection.granted(code)))
        denied = tuple(sorted(required - set(granted)))
        explanation = self._explain(projection, required)
        result = QualificationResult(
            allowed=not denied,
            action=action,
            user_id=user_id,
            business_moment=moment_text,
            rules_version=projection.rules_version,
            granted_competencies=granted,
            denied_competencies=denied,
            explanation=explanation,
        )
        if persist:
            with transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO qual_action_checks(action,user_id,business_moment,required_competencies_json,"
                    "context_json,allowed,rules_version,explanation_json,idempotency_key,checked_by,checked_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        action, user_id, moment_text, _canonical(sorted(required)),
                        _canonical(dict(context or {})), 1 if result.allowed else 0,
                        projection.rules_version, _canonical(explanation),
                        idempotency_key, actor_id or user_id, self._now_text(),
                    ),
                )
        return result

    def require_authorized(self, **kwargs: Any) -> QualificationResult:
        """核对并在不通过时抛出 QualificationDenied（供关键动作调用）。"""

        result = self.authorize(**kwargs)
        if not result.allowed:
            raise QualificationDenied(self._denial_message(result))
        return result

    def project_raw(self, user_id: str, moment_text: str) -> Projection:
        return project(user_id, moment_text, self._user_rows(user_id))

    @staticmethod
    def _explain(projection: Projection, required: frozenset[str]) -> dict[str, Any]:
        basis: dict[str, Any] = {}
        for code in sorted(required):
            view = projection.competencies[code]
            basis[code] = {
                "state": view["state"],
                "granted": view["granted"],
                "effective_events": view["effective_events"],
                "missing_requirements": view["missing_requirements"],
                "active_suspensions": view["active_suspensions"],
            }
        return {
            "rules_version": projection.rules_version,
            "as_of": projection.as_of,
            "demerits": {
                "balance": projection.demerits["balance"],
                "threshold": projection.demerits["threshold"],
                "window_days": projection.demerits["window_days"],
                "blocked": projection.demerits["blocked"],
                "events": projection.demerits["events"],
                "reviews": projection.demerits["reviews"],
            },
            "competencies": basis,
            "considered_event_ids": list(projection.event_ids),
        }

    @staticmethod
    def _denial_message(result: QualificationResult) -> str:
        parts = []
        for code in result.denied_competencies:
            view = result.explanation["competencies"][code]
            state = view["state"]
            if state in ("missing", "expired"):
                detail = "缺少或已过期: " + ",".join(
                    f"{item['requirement']}({item['expected']})" for item in view["missing_requirements"]
                )
            elif state in ("suspended", "demerit_suspended"):
                detail = f"当前处于{state}状态"
            else:
                detail = state
            parts.append(f"{code}: {detail}")
        return "资格核对不满足动作 " + result.action + "：" + "；".join(parts)

    # ------------------------------------------------------------------ #
    # 哈希链与序列化
    # ------------------------------------------------------------------ #
    def verify_chain(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT * FROM qual_events ORDER BY event_id").fetchall()
        previous_hash = GENESIS_HASH
        valid = True
        for row in rows:
            if row["previous_hash"] != previous_hash:
                valid = False
                break
            chain_body = {
                "user_id": row["user_id"],
                "event_type": row["event_type"],
                "competency_code": row["competency_code"],
                "scope": row["scope"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "points": row["points"],
                "reason": row["reason"],
                "idempotency_key": row["idempotency_key"],
                "recorded_by": row["recorded_by"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(_canonical(chain_body).encode("utf-8")).hexdigest()
            if calculated != row["event_hash"]:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    @staticmethod
    def _event_dict(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        data = {
            "event_id": row["event_id"],
            "user_id": row["user_id"],
            "event_type": row["event_type"],
            "competency_code": row["competency_code"],
            "scope": row["scope"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "points": row["points"],
            "reason": row["reason"],
            "idempotency_key": row["idempotency_key"],
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
            "rules_version": row["rules_version"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "event_hash": row["event_hash"],
        }
        if replayed:
            data["replayed"] = True
        return data

    @staticmethod
    def _result_from_check(row: sqlite3.Row) -> QualificationResult:
        explanation = json.loads(row["explanation_json"])
        required = json.loads(row["required_competencies_json"])
        denied = [code for code, view in explanation["competencies"].items() if not view["granted"]]
        granted = [code for code in required if code not in denied]
        return QualificationResult(
            allowed=bool(row["allowed"]),
            action=row["action"],
            user_id=row["user_id"],
            business_moment=row["business_moment"],
            rules_version=row["rules_version"],
            granted_competencies=tuple(granted),
            denied_competencies=tuple(denied),
            explanation=explanation,
        )
