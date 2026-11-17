"""Shared, atomic 60-second fixed windows anchored to the first request."""
import asyncio
import time
from contextlib import suppress
from hashlib import sha256

from redis.asyncio import Redis as AsyncRedis

# A single Lua invocation makes increment and expiry indivisible across workers.
# Rejected attempts do not extend the window or grow the counter indefinitely.
_COUNTER = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
    return 1
end
count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], 60)
end
return 0
"""


_failures = 0
_open_until = 0.0


async def async_redis_limited(url: str, scope: str, key: str, limit: int) -> bool:
    global _failures, _open_until
    from redis.exceptions import ConnectionError
    if time.monotonic() < _open_until:
        raise ConnectionError("circuit open")
    client = AsyncRedis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2)
    try:
        digest = sha256(key.encode()).hexdigest()
        result = await asyncio.wait_for(client.eval(_COUNTER, 1, f"soe:rl:{scope}:{digest}", limit), 0.2)
        _failures = 0
        return bool(result)
    except Exception:
        _failures += 1
        if _failures >= 3:
            _open_until = time.monotonic() + 10
        raise ConnectionError("rate limiter unavailable") from None
    finally:
        with suppress(Exception):
            await asyncio.wait_for(client.aclose(), 0.2)


async def redis_ready(url: str) -> bool:
    client = AsyncRedis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2)
    try:
        return bool(await asyncio.wait_for(client.ping(), 0.2))
    except Exception:
        return False
    finally:
        with suppress(Exception):
            await asyncio.wait_for(client.aclose(), 0.2)
