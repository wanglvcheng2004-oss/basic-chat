import asyncio
from contextlib import asynccontextmanager


class Dispatcher:
    """Foreground-first admission; a background call never occupies every slot."""
    def __init__(self, capacity=3):
        self.capacity = max(1, capacity)
        self.condition = asyncio.Condition()
        self.waiting = []
        self.active = {}
        self.counter = 0

    @asynccontextmanager
    async def slot(self, key, background=False):
        ticket = (int(background), self.counter, key)
        self.counter += 1
        admitted = False
        async with self.condition:
            self.waiting.append(ticket)
            try:
                while True:
                    first = min(self.waiting)
                    eligible = first == ticket and len(self.active) < self.capacity
                    if background:
                        eligible = eligible and not any(self.active.values())
                    if eligible:
                        self.waiting.remove(ticket)
                        self.active[key] = background
                        admitted = True
                        break
                    await self.condition.wait()
            finally:
                if not admitted:
                    self.waiting.remove(ticket)
                    self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.active.pop(key, None)
                self.condition.notify_all()
