# CLAUDE.md

Context for anyone (human or Claude) working on this repo. Read this before
suggesting how to deploy, test or run anything.

## What this is

A Telegram bot that automates a private host's Rentlio PMS work: guest
check-in from ID photos (Google Cloud Vision OCR), non-fiscalized invoices,
daily arrival/departure notifications, and occupancy/pricing analysis.

**The property is two apartments, one owner, one Telegram user.** Scale
assumptions follow from that: a season is on the order of a hundred
reservations, not thousands. Anything that iterates the whole calendar in
memory is fine.

## Deployment - image pull, NOT a git checkout

This is the part that gets assumed wrong most often. The Raspberry Pi
(`luciolab`) runs **Docker Compose, pulling a prebuilt image** from
`ghcr.io/greqq/rentlio-bot:latest`. There is no repo clone on the host, no
virtualenv, no `systemctl restart rentlio-bot`, no `pip install` on the Pi.

The path to production is:

1. Merge to `main`.
2. `.github/workflows/docker.yml` builds `linux/amd64,linux/arm64` and pushes
   `:latest` + `:<sha>` to GHCR. **Only pushes to `main` publish an image** -
   pull requests build but do not push.
3. **Watchtower** on the Pi polls hourly (`WATCHTOWER_POLL_INTERVAL=3600`) and
   restarts the container on a new `:latest`.

To skip the wait, on the Pi: `docker compose pull rentlio-bot && docker compose up -d rentlio-bot`.

Consequences to keep in mind:

- **Only what the Dockerfile COPYs exists in production.** It copies `src/`
  and `scripts/`. A file added anywhere else will not be there - and a path
  listed in `.dockerignore` fails the COPY outright rather than being skipped,
  so the two files have to agree.
- One-off scripts run *inside the container*:
  `docker compose exec rentlio-bot python scripts/analyze_occupancy.py --days 30`
- Config is environment only - `env_file: .env` next to `docker-compose.yml`,
  which is shared with other services on that host (finance bot, dashboard).
  `ANTHROPIC_API_KEY` in particular is already defined there for another
  service, so it may already be set.
- `TZ=Europe/Zagreb` is set in compose. Local dates matter (check-in days,
  night boundaries) - keep using local-time conversions, not UTC.
- Logs: `docker compose logs -f rentlio-bot`.

## Language

**All user-facing bot text is Croatian.** Code, comments, commit messages and
docs are English. Croatian strings in the source avoid diacritics in the newer
modules (c/z/s instead of ć/ž/š) because some output paths are plain text -
match whatever the file around you already does.

## Layout

```
src/bot.py                           Telegram handlers, all commands, job queue
src/config.py                        env -> Config, single source of settings
src/services/rentlio_api.py          async Rentlio client + reservation statuses
src/services/ocr_service.py          Google Vision + MRZ parsing
src/services/country_mapper.py       ISO code -> Rentlio country id
src/services/occupancy_analyzer.py   pricing/occupancy engine (pure, no I/O)
src/services/occupancy_service.py    fetch + cache + Croatian report rendering
src/services/ai_advisor.py           optional Claude briefing over the analysis
scripts/                             diagnostics and one-off tools
```

`src/bot.py` is a flat module of handler functions - no framework, no DI. New
commands go in it next to the related ones, then get registered in `main()`,
in `setup_bot_commands()`, and in `help_command()`.

## Rentlio API - hard-won facts

- Auth is an `apikey` header, not a bearer token.
- Dates in and out are **Unix timestamps in seconds**, not ISO strings.
- **Reservation statuses are not what they look like.** 1 confirmed,
  2 waiting, 3 refused, 4 accepted, 5 cancelled, 6 deleted, 7 option,
  8 in-house, 9 departed. A reservation the bot checked in is status 8, not 1 -
  filter with `is_live_reservation()` / `is_checked_in()`, never `status == 1`.
- `/reservations` is **paginated** (`perPage`, `page`). One page is fine for
  "who arrives tomorrow"; anything reading a season must use
  `get_all_reservations()` or it will silently lose rows.
- Guests are added via `POST /reservations-guests/{id}` with a **bare JSON
  array** as the body, not an object wrapper. Same for invoice item bulk.
- The API does **not** document a booking-creation timestamp. The analysis
  probes several likely field names and degrades to an estimated pickup curve
  when none is present - and says so in the report rather than presenting the
  estimate as measured.
- Endpoint availability differs between accounts. When unsure whether
  something exists, **probe rather than assume**:
  `python scripts/api_capability_scan.py` prints status codes and field shapes
  (redacted by default; `--raw` includes guest PII).

### Endpoint map (probed live 2026-09-23)

Property `26022`; unit types `52887` Sunrise and `52888` Sunset. Note that
unit **types** and unit **ids** differ - `/unit-types/<unit id>/...` answers
403, which reads like a permission problem but is just the wrong id.

Answers 200: `/properties`, `/properties/{id}/units`, `/properties/{id}/unit-types`,
`/properties/{id}/rates` (rate plan definitions, as a **bare list**, not
`{"data": [...]}`), `/reservations`, `/reservations/{id}/details|guests|invoices`,
`/reservations-guests/{id}`, `/invoices`, `/webhooks`, most `/enums/*`.

Does not exist (404): `/units`, `/guests`, `/rates`, `/rate-plans`,
`/unit-types`, `/calendar`, `/restrictions`, `/prices`, `/tourist-tax`,
`/evisitor`, `/online-checkin`, `/messages`, `/account`, `/me`,
`/properties/{id}/rate-plans|settings|webhooks`.

**Current prices and minimum stay cannot be read.** `/availability` exists and
validates its parameters (`propertiesIds`, `dateFrom`, `dateTo`, optionally
`unitTypesIds` / `ratePlansIds`; it rejects `from`/`to` and demands both
dates), but returns `200` with an empty list for every combination tried -
including peak-season July with each real rate plan id. Rentlio documents an
endpoint that *updates* rates, availability and restrictions per unit type, so
this is almost certainly write-only in practice for this account. Do not
re-investigate this without a new reason: the occupancy analysis therefore
compares against what past bookings actually sold for, not against the current
rate card, and that is a deliberate limitation rather than an oversight.

## Anthropic usage

`src/services/ai_advisor.py` is the only place that calls Claude. Rules:

- The deterministic engine owns the numbers; the model only narrates them and
  is told to reason strictly from the JSON payload it receives.
- It must stay **optional**: no key, no `anthropic` package, or a failed call
  all degrade to the rule-based report. Never make a bot feature depend on it.
- Model id comes from `ANTHROPIC_MODEL` (default `claude-opus-5`).

## Testing

There is no test suite. What exists instead:

- CI (`.github/workflows/docker.yml`) runs `pyflakes` and **fails the build on
  undefined names only** - other lint noise is pre-existing and tolerated.
  Keep new files fully clean anyway.
- `python scripts/analyze_occupancy.py --demo` runs the whole pricing engine on
  synthetic seasons, no API key needed. Use it to check analysis changes.
- The API scripts in `scripts/` are read-only probes, safe to run against the
  live account.

Before proposing a change, verifying it with one of those beats reasoning about
it - and never claim a change works in production until an image carrying it
has actually been pulled.

## Style

Match the file you are editing. General rules for this repo: `async`/`await`
throughout, `logger` over `print` in `src/`, no new dependencies without a
reason, and comments that explain *why* a non-obvious thing is that way (the
status-code and pagination comments above are the model), not what the line does.
