"""资格账本向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations


class QualificationError(RuntimeError):
    code = "qualification_error"
    status = 400


class QualificationNotFound(QualificationError):
    code = "qualification_not_found"
    status = 404


class QualificationConflict(QualificationError):
    code = "qualification_conflict"
    status = 409


class QualificationDenied(QualificationError):
    """关键动作时资格核对不通过。"""

    code = "qualification_denied"
    status = 403


class QualificationValidationFailed(QualificationError):
    code = "qualification_validation_failed"
    status = 422


class ImmutableLedgerError(QualificationError):
    """尝试修改或删除历史事件（含触发器拦截）。"""

    code = "immutable_ledger"
    status = 409
