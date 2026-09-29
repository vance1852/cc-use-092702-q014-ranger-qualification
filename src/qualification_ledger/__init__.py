"""巡护人员资格事件账本共享包。

账本只追加、不可修改，记录培训通过、体检、装备授权、违规扣分、临时
停权、复核与恢复等资格事件，并依据可注入时钟在任意业务时刻推导资格
投影。投影结论携带规则版本与生效事件清单，可供排班员核对与审计。
"""

from .clock import FrozenClock, SystemClock, parse_utc, utc_text
from .errors import (
    ImmutableLedgerError,
    QualificationConflict,
    QualificationDenied,
    QualificationError,
    QualificationNotFound,
    QualificationValidationFailed,
)
from .projection import project
from .rules import (
    ACTION_COMPETENCIES,
    COMPETENCY_FOREST_FIRE,
    COMPETENCY_HIGH_ALTITUDE_NIGHT,
    COMPETENCY_SPECIMEN_REVIEW,
    COMPETENCY_WILDLIFE_RESCUE,
    RESOURCE_KIND_COMPETENCIES,
    RULE_BOOK,
    RULES_VERSION,
    TASK_FAMILY_COMPETENCIES,
)
from .service import QualificationLedger, QualificationResult
from .store import attach_schema

__all__ = [
    "FrozenClock",
    "SystemClock",
    "parse_utc",
    "utc_text",
    "ImmutableLedgerError",
    "QualificationConflict",
    "QualificationDenied",
    "QualificationError",
    "QualificationNotFound",
    "QualificationValidationFailed",
    "project",
    "ACTION_COMPETENCIES",
    "COMPETENCY_FOREST_FIRE",
    "COMPETENCY_HIGH_ALTITUDE_NIGHT",
    "COMPETENCY_SPECIMEN_REVIEW",
    "COMPETENCY_WILDLIFE_RESCUE",
    "RULE_BOOK",
    "RULES_VERSION",
    "TASK_FAMILY_COMPETENCIES",
    "QualificationLedger",
    "QualificationResult",
    "attach_schema",
]
