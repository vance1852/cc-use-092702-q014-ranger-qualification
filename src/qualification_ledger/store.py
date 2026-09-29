"""资格事件账本的 SQLite 模式。

事件表只追加：触发器拒绝任何 UPDATE/DELETE，事件之间以 SHA-256
哈希链串联。该模式以 qual_ 为表名前缀，可附加到三个既有业务库。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS qual_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'training_passed','medical_passed','equipment_authorized',
        'demerit','temporary_suspension','review','reinstatement'
    )),
    competency_code TEXT,
    scope TEXT NOT NULL DEFAULT '*',
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0),
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    supersedes_event_id INTEGER REFERENCES qual_events(event_id),
    rules_version TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    UNIQUE(user_id, idempotency_key),
    CHECK(valid_until IS NULL OR valid_until > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_qual_events_user_time
ON qual_events(user_id, valid_from, event_id);

CREATE INDEX IF NOT EXISTS idx_qual_events_type
ON qual_events(user_id, event_type, valid_from);

CREATE TRIGGER IF NOT EXISTS qual_events_no_update
BEFORE UPDATE ON qual_events
BEGIN
    SELECT RAISE(ABORT, 'qual_events 是不可变事件账本，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS qual_events_no_delete
BEFORE DELETE ON qual_events
BEGIN
    SELECT RAISE(ABORT, 'qual_events 是不可变事件账本，禁止删除');
END;

CREATE TABLE IF NOT EXISTS qual_action_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    user_id TEXT NOT NULL,
    business_moment TEXT NOT NULL,
    required_competencies_json TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
    rules_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    checked_by TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qual_checks_user
ON qual_action_checks(user_id, check_id);

CREATE TRIGGER IF NOT EXISTS qual_action_checks_no_update
BEFORE UPDATE ON qual_action_checks
BEGIN
    SELECT RAISE(ABORT, 'qual_action_checks 是不可变决策事实，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS qual_action_checks_no_delete
BEFORE DELETE ON qual_action_checks
BEGIN
    SELECT RAISE(ABORT, 'qual_action_checks 是不可变决策事实，禁止删除');
END;
"""

GENESIS_HASH = "0" * 64


def attach_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """在调用方未开事务时开启一个 IMMEDIATE 事务。"""

    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
