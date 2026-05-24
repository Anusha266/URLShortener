import json

from django.conf import settings
from django.db import transaction
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import base62, cache
from .models import Url


def home(request: HttpRequest) -> HttpResponse:
    return render(request, 'shortener/home.html')


@csrf_exempt
@require_http_methods(['POST'])
def shorten(request: HttpRequest) -> JsonResponse:
    if request.content_type == 'application/json':
        try:
            payload = json.loads(request.body or b'{}')
            long_url = payload.get('url')
        except json.JSONDecodeError:
            return JsonResponse({'error': 'invalid JSON'}, status=400)
    else:
        long_url = request.POST.get('url')

    if not long_url:
        return JsonResponse({'error': 'url is required'}, status=400)

    # Dedupe: if we've already shortened this long_url, return the existing row.
    url = Url.objects.filter(long_url=long_url).first()
    created = False
    if url is None:
        # Two-step insert: row first (to get the auto id), then fill in
        # short_code = base62(id). Wrapped in a transaction so a reader never
        # sees a row with NULL short_code.
        with transaction.atomic():
            url = Url.objects.create(long_url=long_url)
            url.short_code = base62.encode(url.id)
            url.save(update_fields=['short_code'])
        created = True

    # Seed cache on write so the first redirect is a hit.
    cache.set_long_url(url.short_code, url.long_url)

    return JsonResponse({
        'short_url': f'{settings.PUBLIC_BASE_URL}/{url.short_code}',
        'long_url': url.long_url,
    }, status=201 if created else 200)


@require_http_methods(['GET'])
def redirect_view(request: HttpRequest, short_code: str) -> HttpResponse:
    # Cache-aside read.
    long_url = cache.get_long_url(short_code)
    if long_url is None:
        try:
            url = Url.objects.only('long_url').get(short_code=short_code)
        except Url.DoesNotExist:
            raise Http404('short code not found')
        long_url = url.long_url
        cache.set_long_url(short_code, long_url)

    # Write-buffered click counter: stays in Redis until flush_clicks moves it
    # to Postgres. Keeps the redirect path off the DB write path entirely.
    cache.incr_click(short_code)

    return HttpResponseRedirect(long_url)


@require_http_methods(['GET'])
def stats_view(request: HttpRequest, short_code: str) -> JsonResponse:
    try:
        url = Url.objects.only('short_code', 'long_url', 'click_count').get(short_code=short_code)
    except Url.DoesNotExist:
        return JsonResponse({'error': 'short code not found'}, status=404)

    # Combine durable (DB) + buffered (Redis) so the count is up-to-the-second
    # even between flushes. After flush_clicks runs, buffered drops to 0 and
    # the durable count absorbs it — the sum stays the same.
    durable = url.click_count
    buffered = cache.get_buffered_clicks(short_code)

    return JsonResponse({
        'short_code': url.short_code,
        'long_url': url.long_url,
        'clicks': durable + buffered,
        'clicks_durable': durable,
        'clicks_buffered': buffered,
    })
