"""The current AI turn's deadline, so work a turn starts deep inside a tool (an embeddings request during a search)
never outlives the turn budget (AGENT_TURN_TIMEOUT_SECONDS). AgentEngine.run sets it for the duration of a turn; it
is per thread and per context, so concurrent workers never see each other's deadline. Outside a turn it is unset."""
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("duka_turn_deadline", default=None)


def remaining() -> float | None:
    """Seconds left in the current turn (may be negative), or None outside a turn."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


@contextmanager
def turn_deadline(deadline: float) -> Iterator[None]:
    """Make `deadline` (a time.monotonic() value) the current turn's deadline inside the block."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)
