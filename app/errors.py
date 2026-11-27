"""Safe public failures; SDK exception bodies must never cross this boundary."""


class ApplicationError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "The operation could not be completed.") -> None:
        super().__init__(message)


class Conflict(ApplicationError):
    status_code = 409
    code = "conflict"


class Forbidden(ApplicationError):
    status_code = 403
    code = "forbidden"


class Invalid(ApplicationError):
    status_code = 400
    code = "invalid"


class Unavailable(ApplicationError):
    status_code = 503
    code = "unavailable"
