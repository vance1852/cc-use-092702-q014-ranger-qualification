"""资格准入规则版本。

森林消防、野生动物救护和高海拔夜巡对培训、体检与装备授权的要求不同。
规则集不可变：每个版本自发布起生效，投影按业务时刻选择当时适用的版本，
后续版本只能影响未来，不能改变历史时刻的结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ActionRule:
    """一类关键动作需要的资格项。"""

    action: str
    trainings: frozenset[str]
    clearances: frozenset[str]
    equipment: frozenset[str]
    max_points: int

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "trainings": sorted(self.trainings),
            "clearances": sorted(self.clearances),
            "equipment": sorted(self.equipment),
            "max_points": self.max_points,
        }


@dataclass(frozen=True, slots=True)
class RuleSet:
    """某个规则版本对全部四类动作的完整要求。"""

    version: str
    actions: Mapping[str, ActionRule]


def _rule(
    action: str,
    trainings: tuple[str, ...],
    clearances: tuple[str, ...] = (),
    equipment: tuple[str, ...] = (),
    max_points: int = 12,
) -> ActionRule:
    return ActionRule(
        action=action,
        trainings=frozenset(trainings),
        clearances=frozenset(clearances),
        equipment=frozenset(equipment),
        max_points=max_points,
    )


# 2026 年训练季起执行的首版规则。
RULES_2026 = RuleSet(
    version="rules-2026.1",
    actions={
        # 任务领取：承担对应任务线即需对应培训；夜巡额外要求体检与高海拔装备授权。
        "task_claim.forest_fire": _rule(
            "task_claim.forest_fire",
            ("forest-fire-basic", "wilderness-first-aid"),
            ("general-medical",),
        ),
        "task_claim.wildlife_rescue": _rule(
            "task_claim.wildlife_rescue",
            ("wildlife-rescue", "wilderness-first-aid"),
            ("general-medical",),
        ),
        "task_claim.high_altitude_night_patrol": _rule(
            "task_claim.high_altitude_night_patrol",
            ("high-altitude-night-patrol", "wilderness-first-aid"),
            ("high-altitude-medical",),
            ("night-optics", "high-altitude-gear"),
        ),
        # 样本复核：鉴定培训与常规体检。
        "sample_review": _rule(
            "sample_review",
            ("taxonomy-review",),
            ("general-medical",),
        ),
        # 风险处置：消防与急救培训、常规体检、防护装备授权。
        "risk_handling": _rule(
            "risk_handling",
            ("forest-fire-basic", "wilderness-first-aid"),
            ("general-medical",),
            ("protective-gear",),
        ),
        # 资源调拨：装备与应急资源培训、防护装备授权。
        "resource_allocation": _rule(
            "resource_allocation",
            ("equipment-ops",),
            (),
            ("protective-gear",),
        ),
    },
)


# 按生效时间排序的已发布规则版本；新版本只能追加。
PUBLISHED_RULES: tuple[tuple[str, RuleSet], ...] = (
    ("2026-01-01T00:00:00Z", RULES_2026),
)

RULE_VERSIONS = frozenset(ruleset.version for _, ruleset in PUBLISHED_RULES)
ACTIONS = frozenset(RULES_2026.actions)


def rules_effective_at(as_of: str) -> RuleSet:
    """返回业务时刻 as_of（UTC 文本）适用的最新规则版本。"""

    selected = PUBLISHED_RULES[0][1]
    for effective_from, ruleset in PUBLISHED_RULES:
        if effective_from <= as_of:
            selected = ruleset
        else:
            break
    return selected


def action_rule(version: str, action: str) -> ActionRule:
    for _, ruleset in PUBLISHED_RULES:
        if ruleset.version == version:
            if action not in ruleset.actions:
                raise KeyError(action)
            return ruleset.actions[action]
    raise KeyError(version)
