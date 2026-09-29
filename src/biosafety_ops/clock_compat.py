"""biosafety_ops 使用 qualification_ledger 的可注入时钟。"""

from __future__ import annotations

from datetime import datetime

from qualification_ledger import FrozenClock, SystemClock


def make_clock(moment: datetime | None = None):
    return SystemClock() if moment is None else FrozenClock(moment)
