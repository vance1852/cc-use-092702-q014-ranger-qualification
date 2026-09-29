"""资格事件账本的 SQLite 模式与事务辅助。

账本表 qualification_events 只追加：触发器拒绝 UPDATE 和 DELETE，
并以 previous_hash/event_hash 串成哈希链，任何篡改都会在链校验中暴露。
后续修订只能写入新事件（amends 指向被修订事件），不能回写历史事实。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledger_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('administrator','reviewer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 不可变资格事件账本：只允许 INSERT。
CREATE TABLE IF NOT EXISTS qualification_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'training_passed','medical_cleared','equipment_authorized',
        'violation_points','suspension','review','reinstatement'
    )),
    person_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    points INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    idempotency_key TEXT,
    amends_event_id INTEGER REFERENCES qualification_events(event_id),
    review_event_id INTEGER REFERENCES qualification_events(event_id),
    rule_version TEXT,
    actor_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_qual_events_person
ON qualification_events(person_id, event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_qual_events_idempotency
ON qualification_events(person_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS qualification_events_no_update
BEFORE UPDATE ON qualification_events
BEGIN
    SELECT RAISE(ABORT, '资格事件账本不可变：禁止修改事件');
END;

CREATE TRIGGER IF NOT EXISTS qualification_events_no_delete
BEFORE DELETE ON qualification_events
BEGIN
    SELECT RAISE(ABORT, '资格事件账本不可变：禁止删除事件');
END;

-- 关键动作的资格裁决留痕；同键重放返回原裁决，不产生新扣分或恢复。
CREATE TABLE IF NOT EXISTS qualification_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    action TEXT NOT NULL,
    business_ref TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    business_at TEXT NOT NULL,
    approved INTEGER NOT NULL CHECK(approved IN (0,1)),
    rule_version TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(person_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_qual_decisions_action
ON qualification_decisions(action, business_ref, decision_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：HTTP 线程服务器的工作线程共享同一连接，
    # 写入由 BEGIN IMMEDIATE 串行化，配合 WAL 与 busy_timeout 保证安全。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
