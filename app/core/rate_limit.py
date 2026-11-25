from collections import OrderedDict
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request

from app.core.security import digest


class RateLimiter:
    """Bounded, thread-safe single-process limiter. Deploy one worker or add edge limits."""

    def __init__(self):
        self.entries: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.lock = Lock()

    def check(self, key: str, limit: int, seconds: int = 900) -> None:
        current = monotonic()
        with self.lock:
            # Entries keep their fixed-window insertion order.
            while self.entries and next(iter(self.entries.values()))[0] <= current:
                self.entries.popitem(last=False)
            deadline, count = self.entries.get(key, (current + seconds, 0))
            if count >= limit:
                raise HTTPException(429, "Too many attempts. Try again later.", headers={"Retry-After": str(max(1, int(deadline - current)))})
            if key not in self.entries and len(self.entries) >= 10000:
                # Fail closed under key flooding instead of evicting active limits.
                raise HTTPException(429, "Too many attempts. Try again later.")
            self.entries[key] = (deadline, count + 1)

    def auth(self, request: Request, operation: str, email: str = "") -> None:
        ip = request.client.host if request.client else "unknown"
        self.check(f"{operation}:ip:{digest(ip)}", 40)
        if email:
            self.check(f"{operation}:email:{digest(email)}", 10)


limiter = RateLimiter()
