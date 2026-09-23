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
docs are English. The occupancy report uses proper diacritics - it is plain
text in a Telegram message, so nothing forces the stripped spelling. Older
modules mix both; match whatever the file around you already does.

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

**Current prices and minimum stay are read from the unit-type endpoints**,
which are the ones that carry per-date values:

```
GET /unit-types/{unitTypeId}/rates         -> [{"price": 65, "date": "2026-07-03"}]
GET /unit-types/{unitTypeId}/restrictions  -> [{"minStay": 2, "closed": false, "date": ...}]
GET /unit-types/{unitTypeId}/availability  -> [{"availability": 0, "date": ...}]
```

All three take `dateFrom`/`dateTo` (both required for the filter to apply) and
page with `perPage`/`page`. They serve the **standard rate** only, which is
the one the host edits. Per rate plan there is
`GET /unit-types/{unitTypeId}/rates/{ratePlanId}`, and writes go to
`POST /unit-types/{id}/availrates` or
`POST /unit-types/{unitTypeId}/rates-restrictions/{ratePlanId}` - those
propagate to the connected OTA channels, so nothing writes without the host
asking for it.

`/availability?propertiesIds=&dateFrom=&dateTo=` is a different thing than its
name suggests: it lists unit types that are free for **every** day of the
period, so it legitimately answers `200 []` whenever any night in the range is
booked. It is not a way to read rates, and an empty answer from it is not a
sign of anything being broken.

## Pricing rules that came from the host, not from theory

- **A one-night gap is never discounted.** A single night costs the same to
  clean as a five-night stay, which is why the host's own rate card prices
  1 night *above* the 2+ and 3+ tiers (e.g. Sunrise Booking: 70 / 60 / 55).
  Advising a discount there hands back a premium that was set deliberately -
  the only lever is opening the minimum stay, plus offering the neighbouring
  guest an extension, which costs no turnaround at all.
- **`PRICE_FLOORS` caps every discount.** Below the floor a night stops paying
  for its turnaround. The figure is on the **standard rate** scale, since that
  is what the API returns and what a recommendation moves - a floor copied
  from a cheaper derived plan (the direct-booking tier, say) silently lets the
  standard rate fall further than the host intended. When the floor makes the remaining discount negligible
  (<3%), the report says the price lever is spent and points at minimum stay
  or direct bookings instead of advising a 1% cut.
- **Direct beats OTA by more than a discount usually recovers.** On this rate
  card a 2-night direct stay nets ~84 EUR against ~72 EUR through Booking, so
  pushing the direct rate is worth more than shaving the OTA price.
- **`minStay` 0 means no minimum is set, which is the opposite of unknown.**
  Once 0 becomes None the two look identical, and reading "not set" as
  "unknown" makes the report advise a restriction change on a gap nothing was
  blocking. Only the presence of rate data for that apartment tells them
  apart.
- **Every price a recommendation quotes goes through `_apply_discount`**, so
  the floor cannot be skipped. It was once applied to slow stretches only,
  which let a short gap be advised below it.
- Rates read from the API are the **standard rate** (the Booking rate card,
  confirmed against the live calendar: the standard row and the Booking.com
  row carry identical numbers, and the direct plan sits 5 EUR below both);
  the channel-specific plans derive from it. The report says just "cijena" -
  the host knows which card that is, and the qualifier cost a line on every
  recommendation.

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
