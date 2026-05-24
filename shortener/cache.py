"""
Thin Redis wrapper. One connection pool per process via a module-level
singleton. We talk to redis-py directly (not Django's cache framework) so
the cache-aside pattern stays explicit in our view code.
"""

import redis
from django.conf import settings

_client = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        # from_url parses redis://[:password@]host:port/db so the same code
        # works locally (no auth) and on managed Redis (with auth).
        _client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


def url_key(short_code: str) -> str:
    return f'short:{short_code}'


def click_key(short_code: str) -> str:
    return f'clicks:{short_code}'


CLICK_SCAN_PATTERN = 'clicks:*'


def get_long_url(short_code: str) -> str | None:
    return client().get(url_key(short_code))


def set_long_url(short_code: str, long_url: str) -> None:
    client().set(url_key(short_code), long_url, ex=settings.REDIS_CACHE_TTL_SECONDS)


def incr_click(short_code: str) -> int:
    # Atomic counter. Returns the new value after increment.
    # No EXPIRE — counts must persist until the flush command drains them.
    return client().incr(click_key(short_code))


def get_buffered_clicks(short_code: str) -> int:
    raw = client().get(click_key(short_code))
    return int(raw) if raw is not None else 0


def drain_click(short_code: str) -> int:
    # GETDEL is atomic: read and delete in one round-trip, so a concurrent
    # INCR during the flush can't be lost (it lands in a fresh key).
    raw = client().getdel(click_key(short_code))
    return int(raw) if raw is not None else 0


def scan_click_keys():
    # Yields short_codes (the part after "clicks:"). Uses SCAN, not KEYS,
    # so it's safe on a large keyspace — never blocks Redis.
    for key in client().scan_iter(match=CLICK_SCAN_PATTERN):
        yield key.split(':', 1)[1]
