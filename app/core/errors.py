"""Domain errors.

Services raise these; the API layer maps them to status codes in one place
(`app.api.errors`) instead of every router hand-rolling `HTTPException`.
Workers use `PermanentError` to distinguish "retrying will never help" from a
transient failure worth another attempt.
"""
from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, caller-visible failures."""


class NotFoundError(DomainError):
    """The requested entity does not exist. -> HTTP 404"""


class ConflictError(DomainError):
    """The request is valid but conflicts with current state. -> HTTP 409"""


class ValidationError(DomainError):
    """The request is malformed in a way schemas could not catch. -> HTTP 422"""


class PermanentError(DomainError):
    """A task failed in a way that retrying cannot fix.

    The queue marks these failed immediately instead of burning attempts.
    """


class SessionExpiredError(PermanentError):
    """A TikTok session was missing or rejected server-side.

    Always permanent: the account needs a fresh login, and retrying the upload
    would only produce the same rejection.
    """


class TaskCancelled(Exception):
    """Raised inside a handler when the user cancelled the running task.

    Deliberately not a DomainError — cancellation is a control-flow signal,
    not a failure to report.
    """
