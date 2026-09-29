"""巡护人员资格事件账本用例。

账本只追加：培训通过、体检合格、装备授权、违规扣分、临时停权、
复核与恢复均为不可变事件；任何修订只能以新事件影响未来区间。
关键动作（任务领取、样本复核、风险处置、资源调拨）在发生时刻经
authorize_action 核对资格，并将裁决连同规则版本一起留痕。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    QualificationDenied,
    ValidationFailed,
)
from .projection import evaluate, project
from .rules import ACTIONS, rules_effective_at
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "administrator": {"event.write", "review.write", "gate.check", "report.read", "audit.read"},
    "reviewer": {"review.write", "gate.check", "report.read"},
    "auditor": {"gate.check", "report.read", "audit.read"},
}

GRANT_TYPES = {
    "training_passed": "培训通过",
    "medical_cleared": "体检合格",
    "equipment_authorized": "装备授权",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class QualificationLedgerService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _person(self, person_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_users WHERE user_id=?", (person_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资格主体不存在")
        return row

    # -- 账号 ----------------------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO ledger_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # -- 不可变事件 ----------------------------------------------------------

    def _valid_range(
        self, valid_from: str, valid_to: str | None, *, allow_future_start: bool
    ) -> tuple[str, str | None]:
        try:
            start = parse_utc(valid_from, "valid_from")
            end = None if valid_to is None else parse_utc(valid_to, "valid_to")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("valid_to 必须晚于 valid_from")
        now = self.clock.now()
        if start > now and not allow_future_start:
            raise ValidationFailed("资格事件的生效时刻不能晚于记账时刻")
        return utc_text(start), None if end is None else utc_text(end)

    def _replay_by_key(self, person_id: str, idempotency_key: str) -> dict[str, Any] | None:
        existing = self.connection.execute(
            "SELECT * FROM qualification_events WHERE person_id=? AND idempotency_key=?",
            (person_id, idempotency_key),
        ).fetchone()
        return None if existing is None else dict(existing) | {"replayed": True}

    def _replay_or_insert(
        self,
        *,
        event_type: str,
        person_id: str,
        scope: str,
        valid_from: str,
        valid_to: str | None,
        points: int,
        reason: str,
        idempotency_key: str | None,
        amends_event_id: int | None,
        review_event_id: int | None,
        rule_version: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        identity = {
            "event_type": event_type,
            "person_id": person_id,
            "scope": scope,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "points": points,
            "amends_event_id": amends_event_id,
            "review_event_id": review_event_id,
        }
        if idempotency_key is not None:
            existing = self.connection.execute(
                "SELECT * FROM qualification_events WHERE person_id=? AND idempotency_key=?",
                (person_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                replay_identity = {
                    "event_type": existing["event_type"],
                    "person_id": existing["person_id"],
                    "scope": existing["scope"],
                    "valid_from": existing["valid_from"],
                    "valid_to": existing["valid_to"],
                    "points": existing["points"],
                    "amends_event_id": existing["amends_event_id"],
                    "review_event_id": existing["review_event_id"],
                }
                if replay_identity != identity:
                    raise Conflict("幂等键对应不同的资格事件内容")
                return dict(existing) | {"replayed": True}

        previous = self.connection.execute(
            "SELECT event_hash FROM qualification_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        recorded_at = self._now()
        body = {
            "event_type": event_type,
            "person_id": person_id,
            "scope": scope,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "points": points,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "amends_event_id": amends_event_id,
            "review_event_id": review_event_id,
            "rule_version": rule_version,
            "actor_id": actor_id,
            "recorded_at": recorded_at,
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO qualification_events(event_type,person_id,scope,valid_from,valid_to,points,reason,"
                    "idempotency_key,amends_event_id,review_event_id,rule_version,actor_id,recorded_at,"
                    "previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_type,
                        person_id,
                        scope,
                        valid_from,
                        valid_to,
                        points,
                        reason,
                        idempotency_key,
                        amends_event_id,
                        review_event_id,
                        rule_version,
                        actor_id,
                        recorded_at,
                        previous_hash,
                        event_hash,
                    ),
                )
                event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资格事件幂等键并发冲突或引用事件不存在") from exc
        return {"event_id": event_id, **body, "event_hash": event_hash, "replayed": False}

    def record_grant(
        self,
        actor_id: str,
        event_type: str,
        person_id: str,
        scope: str,
        valid_from: str,
        valid_to: str | None = None,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        if event_type not in GRANT_TYPES:
            raise ValidationFailed("不是授权类资格事件")
        self._person(person_id)
        scope = self._scope(scope)
        start, end = self._valid_range(valid_from, valid_to, allow_future_start=True)
        return self._replay_or_insert(
            event_type=event_type,
            person_id=person_id,
            scope=scope,
            valid_from=start,
            valid_to=end,
            points=0,
            reason=reason or f"{GRANT_TYPES[event_type]}登记",
            idempotency_key=idempotency_key,
            amends_event_id=None,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    def revise_grant(
        self,
        actor_id: str,
        amends_event_id: int,
        valid_from: str,
        valid_to: str | None = None,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """修订授权的未来有效范围。

        新事件继承原事件类型与 scope，从 valid_from 起取代原事件；
        valid_from 不得早于记账时刻，因此历史区间与历史排班事实不被回写。
        """

        self._require(actor_id, "event.write")
        target = self.connection.execute(
            "SELECT * FROM qualification_events WHERE event_id=?", (amends_event_id,)
        ).fetchone()
        if target is None:
            raise NotFound("被修订事件不存在")
        if target["event_type"] not in GRANT_TYPES:
            raise ValidationFailed("只能修订培训、体检或装备授权事件")
        start, end = self._valid_range(valid_from, valid_to, allow_future_start=True)
        if parse_utc(start) < self.clock.now():
            raise InvalidState("修订只能从当前或未来时刻生效，不能回写历史有效范围")
        return self._replay_or_insert(
            event_type=target["event_type"],
            person_id=target["person_id"],
            scope=target["scope"],
            valid_from=start,
            valid_to=end,
            points=0,
            reason=reason or "授权范围修订",
            idempotency_key=idempotency_key,
            amends_event_id=amends_event_id,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    def add_violation_points(
        self,
        actor_id: str,
        person_id: str,
        scope: str,
        points: int,
        occurred_at: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        self._person(person_id)
        scope = self._scope(scope)
        if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
            raise ValidationFailed("扣分必须是正整数")
        if not reason.strip():
            raise ValidationFailed("违规扣分必须说明原因")
        start, _ = self._valid_range(occurred_at, None, allow_future_start=False)
        return self._replay_or_insert(
            event_type="violation_points",
            person_id=person_id,
            scope=scope,
            valid_from=start,
            valid_to=None,
            points=points,
            reason=reason.strip(),
            idempotency_key=idempotency_key,
            amends_event_id=None,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    def suspend(
        self,
        actor_id: str,
        person_id: str,
        scope: str,
        valid_from: str,
        valid_to: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        self._person(person_id)
        scope = self._scope(scope)
        if not reason.strip():
            raise ValidationFailed("临时停权必须说明原因")
        start, end = self._valid_range(valid_from, valid_to, allow_future_start=True)
        if end is None:
            raise ValidationFailed("临时停权必须给出预计恢复时刻 valid_to")
        return self._replay_or_insert(
            event_type="suspension",
            person_id=person_id,
            scope=scope,
            valid_from=start,
            valid_to=end,
            points=0,
            reason=reason.strip(),
            idempotency_key=idempotency_key,
            amends_event_id=None,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    def review_event(
        self,
        actor_id: str,
        event_id: int,
        outcome: str,
        note: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """对扣分或停权事件进行复核：维持（upheld）或推翻（overturned）。"""

        self._require(actor_id, "review.write")
        if outcome not in {"upheld", "overturned"}:
            raise ValidationFailed("复核结论必须是 upheld 或 overturned")
        target = self.connection.execute(
            "SELECT * FROM qualification_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if target is None:
            raise NotFound("被复核事件不存在")
        if target["event_type"] not in {"violation_points", "suspension"}:
            raise ValidationFailed("只能复核违规扣分或临时停权事件")
        existing_review = self.connection.execute(
            "SELECT event_id FROM qualification_events WHERE event_type='review' AND amends_event_id=?",
            (event_id,),
        ).fetchone()
        if existing_review is not None:
            if idempotency_key is not None:
                replayed = self._replay_by_key(target["person_id"], idempotency_key)
                if replayed is not None:
                    if replayed["event_type"] != "review" or replayed["amends_event_id"] != event_id or replayed["scope"] != outcome:
                        raise Conflict("幂等键对应不同的复核事件内容")
                    return replayed
            raise InvalidState("该事件已经完成复核，复核结论不可更改")
        now = self._now()
        return self._replay_or_insert(
            event_type="review",
            person_id=target["person_id"],
            scope=outcome,
            valid_from=now,
            valid_to=None,
            points=0,
            reason=note.strip() or ("维持原处理" if outcome == "upheld" else "复核推翻原处理"),
            idempotency_key=idempotency_key,
            amends_event_id=event_id,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    def reinstate(
        self,
        actor_id: str,
        suspension_event_id: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """提前恢复被临时停权的资格；恢复在记账时刻立即生效。"""

        self._require(actor_id, "review.write")
        target = self.connection.execute(
            "SELECT * FROM qualification_events WHERE event_id=?", (suspension_event_id,)
        ).fetchone()
        if target is None:
            raise NotFound("停权事件不存在")
        if target["event_type"] != "suspension":
            raise ValidationFailed("只能针对临时停权事件登记恢复")
        if parse_utc(target["valid_from"]) > self.clock.now():
            raise InvalidState("停权尚未开始，不能提前登记恢复")
        existing = self.connection.execute(
            "SELECT event_id FROM qualification_events WHERE event_type='reinstatement' "
            "AND amends_event_id=?",
            (suspension_event_id,),
        ).fetchone()
        if existing is not None:
            if idempotency_key is not None:
                replayed = self._replay_by_key(target["person_id"], idempotency_key)
                if replayed is not None:
                    if replayed["event_type"] != "reinstatement" or replayed["amends_event_id"] != suspension_event_id:
                        raise Conflict("幂等键对应不同的恢复事件内容")
                    return replayed
            raise InvalidState("该停权事件已经恢复，重复恢复不会再次生效")
        now = self._now()
        return self._replay_or_insert(
            event_type="reinstatement",
            person_id=target["person_id"],
            scope=target["scope"],
            valid_from=now,
            valid_to=None,
            points=0,
            reason=reason.strip() or "复核后恢复资格",
            idempotency_key=idempotency_key,
            amends_event_id=suspension_event_id,
            review_event_id=None,
            rule_version=None,
            actor_id=actor_id,
        )

    @staticmethod
    def _scope(scope: object) -> str:
        if not isinstance(scope, str) or not scope.strip():
            raise ValidationFailed("scope 不能为空")
        result = scope.strip()
        if len(result) > 64:
            raise ValidationFailed("scope 不能超过 64 个字符")
        return result

    # -- 投影与解释 ----------------------------------------------------------

    def _events(self, person_id: str) -> list[sqlite3.Row]:
        self._person(person_id)
        return list(
            self.connection.execute(
                "SELECT * FROM qualification_events WHERE person_id=? ORDER BY event_id",
                (person_id,),
            ).fetchall()
        )

    def qualification(
        self, actor_id: str, person_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        moment = self._moment(as_of)
        ruleset = rules_effective_at(moment)
        result = project(self._events(person_id), moment, ruleset.version)
        return {"person_id": person_id, **result}

    def explain(
        self, actor_id: str, person_id: str, action: str, as_of: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        moment = self._moment(as_of)
        if action not in ACTIONS:
            raise ValidationFailed("未知关键动作")
        return self._explain(person_id, action, moment)

    def _explain(self, person_id: str, action: str, moment: str) -> dict[str, Any]:
        ruleset = rules_effective_at(moment)
        result = evaluate(self._events(person_id), action, moment, ruleset.version)
        return {"person_id": person_id, **result}

    def _moment(self, as_of: str | None) -> str:
        if as_of is None:
            return self._now()
        try:
            return utc_text(parse_utc(as_of, "as_of"))
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

    # -- 关键动作准入裁决 ----------------------------------------------------

    def authorize_action(
        self,
        actor_id: str,
        person_id: str,
        action: str,
        business_ref: str,
        business_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """在关键动作发生时刻核对资格并留痕；同键重放不重复扣分或恢复。"""

        self._require(actor_id, "gate.check")
        person = self._person(person_id)
        if action not in ACTIONS:
            raise ValidationFailed("未知关键动作")
        if not isinstance(business_ref, str) or not business_ref.strip():
            raise ValidationFailed("business_ref 不能为空")
        moment = self._moment(business_at)
        key = idempotency_key or f"{action}:{business_ref.strip()}:{person_id}"

        request_digest = hashlib.sha256(
            canonical_json(
                {
                    "person_id": person_id,
                    "action": action,
                    "business_ref": business_ref.strip(),
                    "business_at": moment,
                }
            ).encode("utf-8")
        ).hexdigest()
        stored = self.connection.execute(
            "SELECT * FROM qualification_decisions WHERE person_id=? AND idempotency_key=?",
            (person_id, key),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同的资格核对请求")
            replay = {
                "decision_id": stored["decision_id"],
                "replayed": True,
                **json.loads(stored["explanation_json"]),
            }
            if not stored["approved"]:
                error = QualificationDenied(
                    f"{person_id} 在 {moment} 不具备 {action} 资格（重放既有裁决）："
                    + "；".join(reason["code"] for reason in replay["reasons"])
                )
                error.decision_id = stored["decision_id"]
                error.replayed = True
                error.explanation = replay
                raise error
            return replay

        if not person["active"]:
            explanation = {
                "person_id": person_id,
                "action": action,
                "approved": False,
                "rule_version": rules_effective_at(moment).version,
                "business_at": moment,
                "reasons": [{"code": "account_inactive"}],
                "requirements": [],
                "points": {"balance": 0, "events": []},
                "suspensions": [],
                "considered_event_ids": [],
                "suppressed_event_ids": [],
            }
        else:
            explanation = self._explain(person_id, action, moment)

        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO qualification_decisions(person_id,action,business_ref,idempotency_key,"
                    "business_at,approved,rule_version,request_sha256,explanation_json,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        person_id,
                        action,
                        business_ref.strip(),
                        key,
                        moment,
                        1 if explanation["approved"] else 0,
                        explanation["rule_version"],
                        request_digest,
                        canonical_json(explanation),
                        actor_id,
                        self._now(),
                    ),
                )
                decision_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资格裁决幂等键并发冲突") from exc
        if not explanation["approved"]:
            error = QualificationDenied(
                f"{person_id} 在 {moment} 不具备 {action} 资格："
                + "；".join(reason["code"] for reason in explanation["reasons"])
            )
            error.decision_id = decision_id
            error.explanation = explanation
            raise error
        return {"decision_id": decision_id, "replayed": False, **explanation}

    def decisions(
        self, actor_id: str, action: str | None = None, business_ref: str | None = None
    ) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        sql = "SELECT * FROM qualification_decisions"
        clauses: list[str] = []
        args: list[Any] = []
        if action is not None:
            clauses.append("action=?")
            args.append(action)
        if business_ref is not None:
            clauses.append("business_ref=?")
            args.append(business_ref)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY decision_id"
        return [dict(row) | {"explanation": json.loads(row["explanation_json"])} for row in self.connection.execute(sql, args).fetchall()]

    def events(self, actor_id: str, person_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        return [dict(row) for row in self._events(person_id)]

    def rule_versions(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        from .rules import PUBLISHED_RULES

        return {
            "versions": [
                {"version": ruleset.version, "effective_from": effective_from}
                for effective_from, ruleset in PUBLISHED_RULES
            ],
            "actions": sorted(ACTIONS),
        }

    # -- 账本完整性 ----------------------------------------------------------

    def verify_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM qualification_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "event_type": row["event_type"],
                "person_id": row["person_id"],
                "scope": row["scope"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "points": row["points"],
                "reason": row["reason"],
                "idempotency_key": row["idempotency_key"],
                "amends_event_id": row["amends_event_id"],
                "review_event_id": row["review_event_id"],
                "rule_version": row["rule_version"],
                "actor_id": row["actor_id"],
                "recorded_at": row["recorded_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
