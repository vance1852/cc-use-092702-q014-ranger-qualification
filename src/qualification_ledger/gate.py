"""关键动作资格核对网关协议。

业务服务（任务领取、样本复核、风险处置、资源调拨）在关键动作发生时
只依赖本协议；默认实现放行一切，保持无账本环境下的既有行为。
生产环境注入 LedgerGate（由资格账本服务支持），拒绝时抛出
GateDenied，宿主服务捕获后转换为各自的 403 错误。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class GateDenied(RuntimeError):
    """资格核对未通过；explanation 为账本给出的可解释结论。"""

    def __init__(self, explanation: dict[str, Any]) -> None:
        self.explanation = explanation
        codes = "；".join(reason.get("code", "unknown") for reason in explanation.get("reasons", []))
        super().__init__(
            f"{explanation.get('person_id')} 在 {explanation.get('business_at')} "
            f"不具备 {explanation.get('action')} 资格：{codes}"
        )


@runtime_checkable
class QualificationGate(Protocol):
    def check(
        self,
        person_id: str,
        action: str,
        business_ref: str,
        business_at: str | None = ...,
        idempotency_key: str | None = ...,
    ) -> dict[str, Any]:
        """核对资格；不满足时抛出 GateDenied，满足时返回可解释裁决。"""


class PermissiveGate:
    """未接入资格账本时的空实现：只回传一个恒通过结论。"""

    def check(
        self,
        person_id: str,
        action: str,
        business_ref: str,
        business_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "person_id": person_id,
            "action": action,
            "business_ref": business_ref,
            "approved": True,
            "rule_version": None,
            "replayed": False,
            "permissive": True,
        }


class LedgerGate:
    """将资格账本服务的裁决适配为业务服务使用的网关。

    actor_id 为执行登记的系统账号（需具备 gate.check 权限）。
    """

    def __init__(self, service: Any, actor_id: str) -> None:
        self.service = service
        self.actor_id = actor_id

    def check(
        self,
        person_id: str,
        action: str,
        business_ref: str,
        business_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.service.authorize_action(
                self.actor_id,
                person_id,
                action,
                business_ref,
                business_at=business_at,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            explanation = getattr(exc, "explanation", None)
            if exc.__class__.__name__ == "QualificationDenied" and isinstance(explanation, dict):
                raise GateDenied(explanation) from exc
            raise
