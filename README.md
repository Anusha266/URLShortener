# URL Shortener

A small URL shortener built as a hands-on study of High-Level Design (HLD) concepts.
Each feature in this project maps to one or more production HLD patterns — caching,
write-buffering, rate limiting, containerization, and cloud-native deployment.

The goal of this repo is **learning, not minimalism**. Code is commented to explain
the *why* of each decision so the project doubles as a study reference.

---

## Architecture

```
                          Browser / curl
                                |
                                v
                  +----------------------------+
                  |   Render Web Service       |
                  |   (Dockerfile + gunicorn)  |
                  +----------------------------+
                  | Django + RateLimit MW      |
                  +------+----------------+----+
                         |                |
              cache-aside|                |durable
                read/write                 writes
                         |                |
                         v                v
                  +-------------+   +-------------+
                  |  Upstash    |   |    Neon     |
                  |   Redis     |   |  Postgres   |
                  +-------------+   +-------------+
                  | URL cache   |   | Url table   |
                  | click ctrs  |   | click_count |
                  | RL counters |   |             |
                  +-------------+   +-------------+
                         ^                ^
                         |                |
                  +-------------+         |
                  | flush_clicks|---------+
                  |  (cron job) |
                  +-------------+
```

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | Django 4.2 | Familiar batteries-included framework; built-in ORM, migrations, middleware |
| App server | Gunicorn (3 workers) | Production WSGI server; multi-worker for concurrency |
| Database | PostgreSQL (Neon managed) | Free serverless Postgres with SSL; production-grade |
| Cache + counters | Redis (Upstash managed) | Atomic operations (INCR), pub-sub potential, free tier |
| Container | Docker + docker-compose | Reproducible local + cloud parity |
| Hosting | Render (Web Service) | Free Docker hosting with auto-deploy from GitHub |
| Config | python-dotenv + env vars | 12-factor: same image, different env per environment |

---

## HLD concepts implemented

### 1. ID generation and short-code encoding
**Files:** `shortener/base62.py`, `shortener/models.py`, `shortener/views.py`

- Postgres `BigAutoField` (64-bit) provides unique IDs without coordination.
- IDs are encoded to base62 (`a-z`, `A-Z`, `0-9`) for short, URL-safe codes.
- Codes are stored alongside the row so reads can lookup by short_code directly.
- Two-step write (`create` then `update short_code = base62(id)`) is wrapped in
  a transaction so no reader ever sees a row with a NULL short_code.
- Dedup on long_url: shortening the same URL returns the existing short_code.

### 2. Cache-aside pattern
**Files:** `shortener/cache.py`, `shortener/views.py`

- Read path: try Redis first. If miss, fall back to Postgres, then populate Redis
  with a TTL of 24 hours.
- Write path: seed Redis on create so the first redirect is a cache hit, not a
  miss-then-fill.
- Cache key namespacing: `short:<code>`, `clicks:<code>`, `ratelimit:<...>` — each
  prefix is a logical "table" in Redis.

### 3. Atomic counters with Redis
**Files:** `shortener/cache.py` (`incr_click`, `rate_limit_check`)

- `INCR` is a single atomic Redis command. Two simultaneous increments cannot
  race; both get correct sequential values.
- Replaces the buggy read-modify-write pattern at the app layer.

### 4. Write-buffered click analytics
**Files:** `shortener/cache.py`, `shortener/views.py`, `shortener/management/commands/flush_clicks.py`

The redirect path is hot — must be fast. Writing to Postgres on every click
would bottleneck on DB write throughput.

- Each redirect does only `INCR clicks:<code>` in Redis (~50 microseconds).
- A separate command, `python manage.py flush_clicks`, drains Redis counters
  into the `Url.click_count` column.
- `flush_clicks` uses `GETDEL` (atomic read+delete) so a concurrent INCR
  during the drain lands in a fresh key, not lost.
- DB update uses Django's `F('click_count') + n` so the increment is atomic
  at the SQL level (`UPDATE ... SET click_count = click_count + N`).
- Stats endpoint sums durable (DB) + buffered (Redis) counts so users see
  consistent totals regardless of flush timing.
- This is the same pattern Reddit uses for upvote counters and Cloudflare uses
  for hit counters.

### 5. Safe Redis scanning
**Files:** `shortener/cache.py` (`scan_click_keys`)

- `KEYS *` blocks Redis until it scans all keys — unusable on large keyspaces.
- `SCAN` is cursor-based: returns keys in small batches, interleaved with other
  client requests. Safe to run on production Redis.
- Implemented as a generator so memory usage is constant regardless of how many
  keys exist.

### 6. Django management commands
**Files:** `shortener/management/commands/flush_clicks.py`

- Standalone CLI commands separate from the web request path.
- Triggered manually or by a cron job (Linux cron, Render Cron Job, Celery Beat,
  Kubernetes CronJob — all use the same command).
- Demonstrates the production split: web workers serve HTTP, separate processes
  handle scheduled batch work.

### 7. Rate limiting (fixed window per IP)
**Files:** `shortener/middleware.py`, `shortener/cache.py` (`rate_limit_check`)

- Implemented as Django middleware so it applies before views run.
- Two buckets:
  - `shorten` (POST /api/shorten): 10 req/min — DB-write-heavy, tightly limited.
  - `redirect` (GET /<code>): 100 req/min — cache-hit-heavy, loosely limited.
- Redis key: `ratelimit:<bucket>:<ip>:<window_minute>`. Encoding the window
  number in the key gives free auto-reset on minute boundaries — no manual reset
  logic.
- Each request fires one Redis pipeline (`INCR + EXPIRE`) for a single round-trip.
- Returns standard HTTP `429 Too Many Requests` with a `Retry-After` header so
  any well-behaved HTTP client auto-backs-off.
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers on
  every response let clients self-throttle.
- Reads real client IP from `X-Forwarded-For` (set by Render's load balancer)
  with a fallback to `REMOTE_ADDR` for local dev.

### 8. Middleware ordering
**Files:** `settings.py`

- Rate limiter sits early in the `MIDDLEWARE` list (slot #2, after
  SecurityMiddleware) so a rejected request never pays the cost of session
  loading, CSRF validation, or auth lookups.

### 9. 12-factor configuration
**Files:** `settings.py`, `.env.example`

- All configuration via environment variables — no hardcoded hosts, passwords,
  or limits.
- `REDIS_URL` supports `rediss://` (TLS) for managed Redis and `redis://` for
  local docker-compose, same code path.
- `POSTGRES_SSLMODE` switches between `prefer` (local) and `require` (managed)
  without code changes.
- Same Docker image runs locally, in CI, and in production with different env
  files — *"build once, deploy anywhere."*

### 10. Containerization and orchestration
**Files:** `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, `.dockerignore`

- Layer caching: `COPY requirements.txt` before `COPY . .` so dependency
  installs are cached across rebuilds.
- `.dockerignore` excludes secrets (`.env`), local venvs, and editor files —
  smaller images and no leaked credentials.
- `docker-compose.yml` defines three services (web, db, redis) on a private
  network. Services find each other by service name (DNS-based service
  discovery), not by IP.
- Healthcheck-gated startup: web waits for `pg_isready` and `redis-cli ping` to
  succeed before booting, preventing crash-on-connect.
- Persistent volume for Postgres data; Redis is treated as ephemeral cache.

### 11. Release-phase migrations
**Files:** `entrypoint.sh`

- Container startup runs `python manage.py migrate` before `gunicorn` boots.
- Guarantees the DB schema matches the deployed code version.
- Same pattern at Heroku, Render, Netflix — "release phase" in 12-factor terms.

### 12. Managed services and connection strings
**Files:** `settings.py`, `shortener/cache.py`

- Postgres on Neon, Redis on Upstash, app on Render — three vendors stitched
  together by URLs and env vars.
- Connection strings (`postgresql://...?sslmode=require`,
  `rediss://default:pass@host:6379`) as the universal addressing format.
- SSL/TLS required for managed services (encryption in transit over the public
  internet).

### 13. Standard HTTP semantics
**Files:** `shortener/middleware.py`

- `429 Too Many Requests` is the HTTP standard for rate limiting.
- `Retry-After` is the standard "back off for N seconds" header.
- `X-RateLimit-*` headers are widely-used conventions (GitHub, Twitter, Stripe).
- `302 Found` for redirects (temporary redirect — appropriate for short links
  whose target may change).

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | HTML form to shorten a URL |
| `POST` | `/api/shorten` | Create a short URL (JSON or form-encoded body) |
| `GET` | `/<short_code>` | 302 redirect to the long URL |
| `GET` | `/api/stats/<short_code>` | Click count (durable + buffered) |

### Example requests

```bash
# Shorten
curl -X POST http://localhost:8000/api/shorten \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
# -> {"short_url": "http://localhost:8000/1", "long_url": "https://example.com"}

# Redirect
curl -i http://localhost:8000/1
# -> 302 Found, Location: https://example.com

# Stats
curl http://localhost:8000/api/stats/1
# -> {"short_code": "1", "long_url": "...", "clicks": 5,
#     "clicks_durable": 3, "clicks_buffered": 2}
```

### Rate limit response

When over the limit:
```
HTTP/1.1 429 Too Many Requests
Retry-After: 47
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 47
{"error": "rate limit exceeded", "bucket": "shorten",
 "limit": 10, "retry_after_seconds": 47}
```

---

## Quick start

### Local (docker-compose)

```bash
cp .env.example .env       # then fill in DJANGO_SECRET_KEY, POSTGRES_PASSWORD
docker compose up --build  # builds image, starts db + redis + web
```

App at `http://localhost:8000`. Postgres on `db:5432` inside the compose
network. Redis on `redis:6379`. Migrations run automatically via
`entrypoint.sh`.

### Run against managed services (Neon + Upstash)

Set `REDIS_URL`, `POSTGRES_HOST`, `POSTGRES_PASSWORD`, etc. in `.env` to point
at your managed instances. Then:

```bash
docker run --rm --env-file .env urlshortener-web python manage.py migrate
docker run --rm --env-file .env -p 8000:8000 urlshortener-web
```

### Flush click counters

```bash
# Local
docker exec <container> python manage.py flush_clicks

# Render (Shell tab)
python manage.py flush_clicks
```

For automation, set up a Render Cron Job or any other scheduler to run
`python manage.py flush_clicks` every N minutes.

---

## Environment variables

See `.env.example` for the full list. Highlights:

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django's secret key — generate fresh per environment |
| `DJANGO_DEBUG` | `True` for dev, `False` for prod |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames Django will accept |
| `POSTGRES_*` | Connection params for Postgres (Neon in prod) |
| `POSTGRES_SSLMODE` | `prefer` for local, `require` for managed |
| `REDIS_URL` | Full Redis connection string. Takes precedence over individual host/port. |
| `RATE_LIMIT_WINDOW_SECONDS` | Bucket size for rate limiter (default 60) |
| `RATE_LIMIT_SHORTEN_PER_MINUTE` | Shorten endpoint quota (default 10) |
| `RATE_LIMIT_REDIRECT_PER_MINUTE` | Redirect endpoint quota (default 100) |
| `REDIS_CACHE_TTL_SECONDS` | TTL for cached URLs (default 86400 = 24h) |
| `PUBLIC_BASE_URL` | The base URL used in returned short_url responses |

---

## Project layout

```
urlshortener/
  Dockerfile                          # build recipe
  docker-compose.yml                  # local orchestration (3 services)
  entrypoint.sh                       # migrate then start gunicorn
  .dockerignore                       # excludes .env, .git, caches
  .env.example                        # template — copy to .env
  manage.py
  settings.py                         # all config via env vars
  urls.py                             # root URL conf
  wsgi.py / asgi.py
  requirements.txt
  shortener/
    base62.py                         # int <-> base62 encoder
    models.py                         # Url table (short_code, long_url, click_count)
    views.py                          # shorten, redirect, stats
    urls.py                           # app routes
    cache.py                          # all Redis helpers
    middleware.py                     # RateLimitMiddleware
    admin.py
    templates/shortener/home.html
    management/
      commands/
        flush_clicks.py               # drains click counters Redis -> DB
    migrations/
      0001_initial.py
      0002_url_click_count.py
```

---

## What I'd add next (out of scope for this project)

These are the next HLD concepts that would be valuable to learn, but are
better as new projects rather than additions here:

- **Background job queue** (Celery + Redis broker) — for async tasks beyond cron.
- **Object storage** (S3 / R2 + pre-signed URLs) — needed for a Pastebin clone.
- **Sliding window / token bucket rate limiting** — variations of the current
  algorithm, same Redis primitives.
- **Custom domains + TLS provisioning** — DevOps-leaning, not algorithmic.
- **Per-day analytics buckets** — `clicks:<code>:<YYYY-MM-DD>` keys + a stats
  endpoint that returns a time series.
- **Bloom filter** for "does this short code exist" — premature optimization at
  small scale but a real production pattern.
- **Read replicas / sharding** — only meaningful past a few thousand QPS.

---

## Credits

Project built as part of a step-by-step study of HLD concepts. Each feature
was added incrementally, with the *why* of each design choice documented in
inline code comments. The intent is that the codebase itself reads like a
walkthrough — every non-obvious line has a comment explaining the trade-off
or the underlying pattern.
