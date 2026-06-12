"""Exception hierarchy for task-bundle.

Every predictable failure raises a ``TaskError`` subclass carrying a message that
tells the user what went wrong AND what to do about it. The CLI layer catches
``TaskError`` and renders it without a traceback; anything else is a genuine bug.
"""


class TaskError(Exception):
    """Base class for all expected, user-facing errors."""

    exit_code: int = 1


class BundleError(TaskError):
    """The bundle directory or its task.json is missing or invalid."""


class GitError(TaskError):
    """A git operation (clone/fetch/checkout) failed."""


class DockerError(TaskError):
    """A docker operation failed or the daemon is unreachable."""


class SolverError(TaskError):
    """A solver could not run (missing API key, exhausted budget, bad config)."""


class ContractViolation(TaskError):
    """The baseline test contract does not hold (validate failures)."""

    exit_code = 2


class HiddenTestLeak(TaskError):
    """A hidden test path or its content was found in the solver-visible workspace."""

    exit_code = 3
