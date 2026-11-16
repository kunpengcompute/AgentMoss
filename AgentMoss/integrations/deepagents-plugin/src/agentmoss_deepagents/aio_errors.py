"""SDK-independent exceptions that must survive DeepAgents parameter handling."""


class AIOExecutionUncertain(RuntimeError):
    """A submitted command may still be running; do not retry or reuse this session."""


class AIOUnavailable(RuntimeError):
    """AIO transport/protocol failure, not a missing file or an empty directory."""


class AIOStartupError(AIOUnavailable):
    """Recoverable identifiers, without SDK bodies or credentials, for failed startup."""

    def __init__(self, *, sandbox_id: str, session_id: str, cleanup_confirmed: bool, failure_type: str):
        self.sandbox_id = sandbox_id
        self.session_id = session_id
        self.cleanup_confirmed = cleanup_confirmed
        self.failure_type = failure_type
        super().__init__(
            f"AIO shell creation failed ({failure_type}); sandbox={sandbox_id}; "
            f"session={session_id}; cleanup_confirmed={cleanup_confirmed}. "
            "Inspect this session before retrying."
        )
