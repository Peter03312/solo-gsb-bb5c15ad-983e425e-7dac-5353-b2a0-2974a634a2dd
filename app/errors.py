"""领域错误：携带稳定错误码与 HTTP 状态码，API 层统一转成明确报错。"""
from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class ValidationFailed(DomainError):
    status_code = 422
    code = "VALIDATION_FAILED"


class DuplicateCertNo(DomainError):
    # 422 与 409 之间，重复签发属于与既有资源冲突，统一按 409 处理
    status_code = 409
    code = "DUPLICATE_CERT_NO"


class TimeRangeInvalid(ValidationFailed):
    code = "TIME_RANGE_INVALID"


class ParentNotQualified(DomainError):
    """引用证书在该校准时刻不存在/无效/不合格。"""
    status_code = 409
    code = "PARENT_NOT_QUALIFIED"


class ValidityOverlap(DomainError):
    """同一设备生效证书有效期重叠（含并发重复签发）。"""
    status_code = 409
    code = "VALIDITY_OVERLAP"


class ReferenceChainCycle(DomainError):
    status_code = 409
    code = "REFERENCE_CHAIN_CYCLE"


class ReferenceRootInvalid(DomainError):
    status_code = 422
    code = "REFERENCE_ROOT_INVALID"


class CertificateNotActive(DomainError):
    status_code = 409
    code = "CERTIFICATE_NOT_ACTIVE"


class ConcurrentIssuance(DomainError):
    status_code = 409
    code = "CONCURRENT_ISSUANCE"
