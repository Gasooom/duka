"""Small in-process rate limiters.

Good enough for a single-instance MVP. When running multiple instances, swap for a
Redis-backed limiter (documented in README > Future work)."""
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 50_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > self.max_keys:  # keys whose window has passed are forgotten, not kept forever
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
                    del self._hits[k]
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class FailureBackoff:
    """Progressive lockout per key (an account) after consecutive failures: the first `free` failures cost nothing,
    each further one locks the key for base * 2^n seconds (capped). Keyed by what is being attacked, not by who
    is asking, so rotating IP addresses or forging X-Forwarded-For does not help. A success clears the key; so
    does a quiet `forget` period. Bounded: the least recently failed keys are dropped first."""

    def __init__(self, *, free: int = 5, base_seconds: float = 30.0, max_seconds: float = 900.0,
                 forget_seconds: float = 3600.0, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.free, self.base, self.cap, self.forget = free, base_seconds, max_seconds, forget_seconds
        self.max_keys = max_keys
        self.clock = clock
        self._state: OrderedDict[str, tuple[int, float, float]] = OrderedDict()  # failures, locked until, last
        self._lock = threading.Lock()

    def _current(self, key: str, now: float) -> tuple[int, float, float] | None:
        st = self._state.get(key)
        if st is not None and now >= st[1] and now - st[2] > self.forget:
            del self._state[key]
            return None
        return st

    def retry_after(self, key: str) -> float:
        """Seconds before `key` may be tried again (0 = now)."""
        now = self.clock()
        with self._lock:
            st = self._current(key, now)
            return max(0.0, st[1] - now) if st else 0.0

    def failure(self, key: str) -> tuple[int, float]:
        """Record a failure. Returns (consecutive failures, lockout in seconds that starts now; 0 = none)."""
        now = self.clock()
        with self._lock:
            failures = (self._current(key, now) or (0, 0.0, 0.0))[0] + 1
            lockout = 0.0
            if failures > self.free:
                lockout = min(self.cap, self.base * 2 ** (failures - self.free - 1))
            self._state.pop(key, None)
            self._state[key] = (failures, now + lockout, now)
            while len(self._state) > self.max_keys:
                self._state.popitem(last=False)
            return failures, lockout

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


auth_limiter = RateLimiter(limit=20)
inbound_message_limiter = RateLimiter(limit=30)
login_backoff = FailureBackoff()
