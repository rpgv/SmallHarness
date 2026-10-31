"""Errors that say what went wrong in terms a user can act on. The terminal prints the
message; the web interface maps the kind to an HTTP status in one place."""


class HarnessError(Exception):
    """Something that cannot be done as asked; the message says why."""
    status = 400


class Invalid(HarnessError, ValueError):
    """The input cannot be used: a bad name, an empty plan."""
    status = 422


class NotFound(HarnessError):
    status = 404


class Conflict(HarnessError):
    """Not now: the thing is in use or busy."""
    status = 409
