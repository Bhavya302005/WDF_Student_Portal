"""Small shared primitives for bounded, observable public-board requests."""
import asyncio
import os
from weakref import WeakKeyDictionary
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class RequestGate:
    """One paced queue; cooldown invalidates slots reserved by waiting workers."""

    MAX_SLEEP = 60.0  # Never sleep longer than this in one go (acquire() loops)
    # A server-supplied Retry-After is authoritative and may legitimately
    # exceed MAX_SLEEP; retrying before it elapses usually escalates the block.
    # Still bounded so a hostile or garbage header cannot stall a run forever.
    MAX_RETRY_AFTER = 900.0

    def __init__(self, rate, min_rate, max_rate, cooldown=5.0, recovery=0.002):
        self.min_rate = max(0.01, min_rate)
        self.max_rate = max(self.min_rate, max_rate)
        self.rate = max(self.min_rate, min(rate, self.max_rate))
        self.cooldown = cooldown
        self.recovery = recovery
        self._next_slot = self._paused_until = 0.0
        self._generation = 0
        self._lock = asyncio.Lock()

    async def acquire(self):
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:
                now = loop.time()
                slot = max(self._paused_until, self._next_slot)
                if now >= slot:
                    # Allocate at actual dispatch time: delayed workers must not
                    # release a burst of overdue reservations after CPU work.
                    self._next_slot = now + 1.0 / self.rate
                    return
            await asyncio.sleep(min(max(0, slot - loop.time()), self.MAX_SLEEP))

    async def on_throttle(self, retry_after=None):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if retry_after:
                # Honour the server's explicit wait in full (bounded), rather
                # than clamping it to MAX_SLEEP and hammering it again early.
                delay = min(max(self.cooldown, retry_after), self.MAX_RETRY_AFTER)
            else:
                delay = min(self.cooldown, self.MAX_SLEEP)
            until = now + delay
            if now >= self._paused_until:
                self.rate = max(self.min_rate, self.rate * 0.7)
                self._generation += 1
            # Concurrent responses extend a cooldown but do not repeatedly cut rate.
            self._paused_until = max(self._paused_until, until)
            self._next_slot = self._paused_until

    async def on_429(self, retry_after=None):
        await self.on_throttle(retry_after)

    def on_success(self):
        if asyncio.get_running_loop().time() >= self._paused_until:
            self.rate = min(self.max_rate, self.rate + self.recovery)


_client_gates = WeakKeyDictionary()


async def paced_request(client, method, url, *, board, rate, **kwargs):
    """Share one request budget across listing/detail workers using a client.

    Retries remain with callers; every attempt goes through this gate.
    A 429 pauses all workers, including those already waiting to dispatch.
    """
    gates = _client_gates.setdefault(client, {})
    if board not in gates:
        ceiling = float(os.getenv(f'{board}_REQUEST_RATE', str(rate)))
        gates[board] = RequestGate(ceiling, max(ceiling * 0.1, 1.0), ceiling,
                                   cooldown=float(os.getenv(f'{board}_COOLDOWN_SECONDS', '2')),
                                   recovery=float(os.getenv(f'{board}_RECOVERY_RATE', '0.05')))
    gate = gates[board]
    await gate.acquire()
    response = await getattr(client, method)(url, **kwargs)
    if response.status_code == 429:
        await gate.on_throttle(retry_after_seconds(response.headers.get('Retry-After')))
    elif 200 <= response.status_code < 300:
        gate.on_success()
    return response


async def bounded_map(fn, items, concurrency):
    """Preserve input order without allocating a task for every item."""
    items = list(items)
    results = [None] * len(items)
    pending = iter(enumerate(items))

    async def worker():
        for index, item in pending:
            results[index] = await fn(item)

    tasks = [asyncio.create_task(worker()) for _ in range(min(max(1, concurrency), len(items)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return results
