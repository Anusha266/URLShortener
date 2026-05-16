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
        _client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            decode_responses=True,
        )
    return _client


def url_key(short_code: str) -> str:
    return f'short:{short_code}'


def get_long_url(short_code: str) -> str | None:
    return client().get(url_key(short_code))


def set_long_url(short_code: str, long_url: str) -> None:
    client().set(url_key(short_code), long_url, ex=settings.REDIS_CACHE_TTL_SECONDS)
