import asyncio
import time

class SimpleRateLimiter:
    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.last_request = -1000.0

    async def __call__(self) -> None:
        now_time = time.time()
        if self.last_request + self.rate > now_time:
            await asyncio.sleep(self.last_request + self.rate - now_time)
            self.last_request = time.time()
        else:
            self.last_request = now_time
