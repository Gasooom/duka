"""Domain errors. Services raise these; the API layer maps them to HTTP responses
and the agent tool layer maps them to structured tool errors."""


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, params: dict | None = None):
        super().__init__(message)
        self.message = message
        # `params` are the facts inside the message (name, qty, order number...), so the customer-facing text can
        # be rendered in the conversation language from the same values (app/i18n.py, keys "err_<code>").
        self.params = params or {}
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
