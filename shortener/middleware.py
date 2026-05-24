"""
Fixed-window rate limiter middleware.

One Redis counter per (bucket, ip, window) tuple. The window number is
embedded in the key, so rolling over to the next window uses a fresh key
automatically — no reset logic needed.

Configured buckets:
  - 'shorten':  POST /api/shorten         (expensive: DB write + base62 encode)
  - 'redirect': GET /<short_code>          (cheap: usually a Redis cache hit)

Excluded from limiting:
  - / (home page)
  - /api/stats/*
  - /admin/*
  - /static/*
"""

import json
import re

from django.conf import settings
from django.http import HttpResponse

from . import cache


# Compiled once at import time. Matches base62 short codes (alnum, 1-12 chars).
SHORT_CODE_RE = re.compile(r'^/[A-Za-z0-9]{1,12}$')


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rule = self._classify(request)
        if rule is None:
            # Path not subject to rate limiting (e.g., admin, static, stats).
            return self.get_response(request)

        bucket, limit = rule
        ip = self._client_ip(request)
        count, reset_in = cache.rate_limit_check(
            bucket=bucket,
            ip=ip,
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        )

        if count > limit:
            body = json.dumps({
                'error': 'rate limit exceeded',
                'bucket': bucket,
                'limit': limit,
                'retry_after_seconds': reset_in,
            })
            response = HttpResponse(body, status=429, content_type='application/json')
            response['Retry-After'] = str(reset_in)
            response['X-RateLimit-Limit'] = str(limit)
            response['X-RateLimit-Remaining'] = '0'
            response['X-RateLimit-Reset'] = str(reset_in)
            return response

        response = self.get_response(request)
        response['X-RateLimit-Limit'] = str(limit)
        response['X-RateLimit-Remaining'] = str(max(0, limit - count))
        response['X-RateLimit-Reset'] = str(reset_in)
        return response

    @staticmethod
    def _classify(request):
        path = request.path
        if path == '/api/shorten' and request.method == 'POST':
            return 'shorten', settings.RATE_LIMIT_SHORTEN_PER_MINUTE
        # Anything that looks like /<short_code> — the redirect route.
        if request.method == 'GET' and SHORT_CODE_RE.match(path):
            return 'redirect', settings.RATE_LIMIT_REDIRECT_PER_MINUTE
        return None

    @staticmethod
    def _client_ip(request):
        # Behind a proxy (Render, Cloudflare), the real client IP is in the
        # leftmost element of X-Forwarded-For. REMOTE_ADDR is just the proxy.
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            return xff.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')
