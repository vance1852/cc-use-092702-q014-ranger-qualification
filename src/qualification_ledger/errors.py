"""巡护资格账本服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations


class QualificationLedgerError(RuntimeError):
    code = "qualification_error"
    status = 400


class NotFound(QualificationLedgerError):
    code = "not_found"
    status = 404


class Conflict(QualificationLedgerError):
    code = "conflict"
    status = 409


class Forbidden(QualificationLedgerError):
    code = "forbidden"
    status = 403


class InvalidState(QualificationLedgerError):
    code = "invalid_state"
    status = 409


class ValidationFailed(QualificationLedgerError):
    code = "validation_failed"
    status = 422


class QualificationDenied(QualificationLedgerError):
    """关键动作时刻资格核对未通过。"""

    code = "qualification_denied"
    status = 403
