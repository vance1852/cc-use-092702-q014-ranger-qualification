"""资格规则版本目录。

规则按版本不可变地登记，每个版本带有生效时间。投影某个业务时刻 T 的
资格时，只采用 effective_from <= T 的最新规则版本，因此后续修订只会
影响未来的判定，不会改写历史排班时刻已经形成的结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


# 资格（业务能力域）标识
COMPETENCY_FOREST_FIRE = "forest-firefighting"          # 森林消防
COMPETENCY_WILDLIFE_RESCUE = "wildlife-rescue"          # 野生动物救护
COMPETENCY_HIGH_ALTITUDE_NIGHT = "high-altitude-night"  # 高海拔夜巡
COMPETENCY_SPECIMEN_REVIEW = "specimen-review"          # 样本/证据复核

# 事件类型
EVENT_TRAINING = "training_passed"          # 培训通过
EVENT_MEDICAL = "medical_passed"            # 体检通过
EVENT_EQUIPMENT = "equipment_authorized"    # 装备授权
EVENT_DEMERIT = "demerit"                   # 违规扣分
EVENT_SUSPENSION = "temporary_suspension"   # 临时停权
EVENT_REVIEW = "review"                     # 复核
EVENT_REINSTATEMENT = "reinstatement"       # 恢复

GRANT_EVENTS = (EVENT_TRAINING, EVENT_MEDICAL, EVENT_EQUIPMENT)


@dataclass(frozen=True, slots=True)
class CompetencyRequirement:
    """单个资格对培训、体检与装备授权的不同要求。"""

    code: str
    label: str
    training_codes: frozenset[str]
    medical_kinds: frozenset[str]
    equipment_scopes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class RuleBook:
    version: str
    effective_from: str  # ISO 8601；该规则版本开始适用的业务时刻
    requirements: dict[str, CompetencyRequirement]
    # 各类授权的最长有效期；登记事件时校验 valid_until 不得超出
    validity_days: dict[str, int]
    # 违规扣分滚动有效窗口与停权阈值
    demerit_window_days: int = 730
    suspension_threshold: int = 12

    def requirement(self, competency: str) -> CompetencyRequirement:
        try:
            return self.requirements[competency]
        except KeyError as exc:
            raise KeyError(f"未知资格: {competency}") from exc

    @property
    def timedelta_window(self) -> timedelta:
        return timedelta(days=self.demerit_window_days)


_COMPETENCIES_V1 = {
    COMPETENCY_FOREST_FIRE: CompetencyRequirement(
        code=COMPETENCY_FOREST_FIRE,
        label="森林消防",
        training_codes=frozenset({"forest-fire-basic", "forest-fire-command"}),
        medical_kinds=frozenset({"fire-ground"}),
        equipment_scopes=frozenset({"fire-suit", "breathing-apparatus"}),
    ),
    COMPETENCY_WILDLIFE_RESCUE: CompetencyRequirement(
        code=COMPETENCY_WILDLIFE_RESCUE,
        label="野生动物救护",
        training_codes=frozenset({"wildlife-rescue-basic"}),
        medical_kinds=frozenset({"field-rescue"}),
        equipment_scopes=frozenset({"rescue-kit"}),
    ),
    COMPETENCY_HIGH_ALTITUDE_NIGHT: CompetencyRequirement(
        code=COMPETENCY_HIGH_ALTITUDE_NIGHT,
        label="高海拔夜巡",
        training_codes=frozenset({"high-altitude-night-basic"}),
        medical_kinds=frozenset({"high-altitude"}),
        equipment_scopes=frozenset({"night-vision", "oxygen-kit"}),
    ),
    COMPETENCY_SPECIMEN_REVIEW: CompetencyRequirement(
        code=COMPETENCY_SPECIMEN_REVIEW,
        label="样本复核",
        training_codes=frozenset({"taxonomy-review-basic"}),
        medical_kinds=frozenset(),
        equipment_scopes=frozenset(),
    ),
}

_VALIDITY_V1 = {
    EVENT_TRAINING: 365,
    EVENT_MEDICAL: 365,
    EVENT_EQUIPMENT: 180,
}

RULES_VERSION = "rules-2026-09-01"

RULE_BOOK = RuleBook(
    version=RULES_VERSION,
    effective_from="2026-09-01T00:00:00Z",
    requirements=_COMPETENCIES_V1,
    validity_days=_VALIDITY_V1,
)

# 未来修订版：收紧停权阈值（12 -> 8）。资格要求不变，因此历史授权事实
# 不受影响；2027-01-01 起的投影才采用新阈值，历史时刻仍按旧版判定。
RULES_V2 = RuleBook(
    version="rules-2027-01-01",
    effective_from="2027-01-01T00:00:00Z",
    requirements=_COMPETENCIES_V1,
    validity_days=_VALIDITY_V1,
    suspension_threshold=8,
)

# 有序版本目录；未来新增修订时追加（effective_from 更晚的）新版本。
RULE_VERSIONS: tuple[RuleBook, ...] = (RULE_BOOK, RULES_V2)

# 关键业务动作 -> 所需资格（默认映射；调用方可按任务上下文收窄/扩展）
ACTION_COMPETENCIES: dict[str, frozenset[str]] = {
    "job.claim": frozenset({COMPETENCY_SPECIMEN_REVIEW}),
    "specimen.review": frozenset({COMPETENCY_SPECIMEN_REVIEW}),
    "risk.dispose": frozenset({COMPETENCY_FOREST_FIRE}),
    "resource.allocate": frozenset({COMPETENCY_FOREST_FIRE}),
}

# 实验室任务族 -> 领取任务所需资格
TASK_FAMILY_COMPETENCIES: dict[str, frozenset[str]] = {
    "insect-taxonomy": frozenset({COMPETENCY_SPECIMEN_REVIEW}),
}

# 应急资源类型 -> 调拨该资源所需资格（不同救援业务要求不同）
RESOURCE_KIND_COMPETENCIES: dict[str, frozenset[str]] = {
    "preservation-box": frozenset({COMPETENCY_SPECIMEN_REVIEW}),
    "evidence-kit": frozenset({COMPETENCY_SPECIMEN_REVIEW}),
    "ambulance": frozenset({COMPETENCY_WILDLIFE_RESCUE}),
    "tow-truck": frozenset({COMPETENCY_FOREST_FIRE}),
    "warning-kit": frozenset({COMPETENCY_FOREST_FIRE}),
    "rapid-response-team": frozenset({COMPETENCY_FOREST_FIRE}),
}

GLOBAL_SCOPE = "*"


def select_rules(as_of: str) -> RuleBook:
    """选择业务时刻 as_of 适用的规则版本。"""

    from .clock import parse_utc

    moment = parse_utc(as_of, "as_of")
    chosen = RULE_VERSIONS[0]
    for book in RULE_VERSIONS:
        if parse_utc(book.effective_from, "effective_from") <= moment:
            chosen = book
    return chosen
