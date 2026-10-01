"""Domain errors. Services raise these; the API layer maps them to HTTP responses
and the agent tool layer maps them to structured tool errors."""


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class PermissionDenied(DomainError):
    status_code = 403
    code = "forbidden"


class ExternalServiceError(DomainError):
    status_code = 502
    code = "external_service_error"
