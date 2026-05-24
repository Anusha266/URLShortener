"""
Drain buffered click counts from Redis into the Url.click_count column.

Designed to be run periodically (cron, scheduled job, k8s CronJob).
Idempotent and crash-safe: uses GETDEL so a concurrent INCR during the drain
isn't lost — it just lands in a fresh key and gets picked up next run.
"""

from django.core.management.base import BaseCommand
from django.db.models import F

from shortener import cache
from shortener.models import Url


class Command(BaseCommand):
    help = 'Flush buffered click counts from Redis into the Url table.'

    def handle(self, *args, **options):
        # Snapshot the keys first; SCAN is non-blocking but yields lazily, and
        # we don't want to interleave drains with further scanning.
        short_codes = list(cache.scan_click_keys())

        flushed = 0
        for short_code in short_codes:
            n = cache.drain_click(short_code)
            if n <= 0:
                continue
            # F() expression makes the increment atomic at the SQL level:
            # UPDATE url SET click_count = click_count + N — no read-modify-write,
            # so concurrent flushes can't lose updates.
            updated = Url.objects.filter(short_code=short_code).update(
                click_count=F('click_count') + n,
            )
            if updated == 0:
                # Short code is in Redis but not in DB (deleted? race?).
                # Re-buffer the count so it isn't lost; admin can investigate.
                cache.client().incrby(cache.click_key(short_code), n)
                self.stderr.write(
                    f'WARN: clicks:{short_code} drained {n} but no Url row; re-buffered.'
                )
                continue
            flushed += n

        self.stdout.write(
            self.style.SUCCESS(
                f'Flushed {flushed} clicks across {len(short_codes)} short codes.'
            )
        )
