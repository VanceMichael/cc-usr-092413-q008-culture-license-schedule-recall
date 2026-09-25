"""业务错误与 HTTP 状态码映射。"""


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class LicenseCoverageError(ServiceError):
    status_code = 422
    code = "license_not_covered"


class SegregationError(ServiceError):
    status_code = 403
    code = "segregation_violation"


class InvalidState(ServiceError):
    status_code = 409
    code = "invalid_state"
