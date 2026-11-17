"""Shared, atomic 60-second fixed windows anchored to the first request."""
from functools import lru_cache
from hashlib import sha256

from redis import Redis

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


@lru_cache(maxsize=1)
def redis_client(url: str) -> Redis:
    return Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)


def redis_limited(url: str, scope: str, key: str, limit: int) -> bool:
    # Synchronous boundary: async request handlers invoke limited in a threadpool.
    # Avoid storing raw usernames/IPs and delimiter collisions in Redis keys.
    digest = sha256(key.encode()).hexdigest()
    return bool(redis_client(url).eval(_COUNTER, 1, f"soe:rl:{scope}:{digest}", limit))
