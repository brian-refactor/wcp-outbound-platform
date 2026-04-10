# WCP Outbound Platform

An internal investor acquisition platform for Willow Creek Partners. Automates cold email outreach via Smartlead, tracks engagement events, syncs activity to HubSpot CRM, and validates prospect email addresses via ZeroBounce — all managed through a private web dashboard.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Data Model](#data-model)
- [Sequence / Campaign Logic](#sequence--campaign-logic)
- [Integrations](#integrations)
- [Dashboard](#dashboard)
- [Getting Started (Local)](#getting-started-local)
- [Environment Variables](#environment-variables)
- [Database Migrations](#database-migrations)
- [Running the Worker](#running-the-worker)
- [Railway Deployment](#railway-deployment)
- [Outstanding / To-Do](#outstanding--to-do)
- [Known Issues](#known-issues)

---

## What It Does

1. **Import prospects** via CSV upload or manual entry form.
2. **Validate email addresses** automatically via ZeroBounce (runs every 30 min). Only `valid` emails can be enrolled in campaigns.
3. **Enroll prospects** into Smartlead email sequences — individually from the detail page, or in bulk from the list view. Choose campaign + sequence type at enrollment time.
4. **Receive Smartlead webhooks** for every email event (sent, open, click, reply, bounce, unsubscribe, sequence complete). Events are stored in the `email_events` table.
5. **High Intent scan** runs every 15 min — if a prospect has opened ≥ 3 emails and clicked ≥ 1 link, they are moved to a "High Intent" Smartlead campaign automatically.
6. **Sync to HubSpot** every 5 min — upserts contacts and creates a CRM note for each email event. On reply, a Deal is created in HubSpot.
7. **Dashboard** shows live stats, a funnel chart by sequence type, recent activity feed, and the ZeroBounce credit balance.

---

## Architecture

```
                        ┌────────────────────────────────┐
                        │         Railway (cloud)         │
                        │                                 │
  Browser ─────────────▶│  web service (uvicorn/FastAPI)  │
                        │  - Dashboard UI (Jinja2/HTMX)   │
                        │  - REST API /prospects          │
                        │  - Webhook receiver /webhooks   │
                        │                                 │
  Smartlead ───webhook──▶│                                │
                        │                                 │
                        │  worker service (Celery+Beat)   │
                        │  - HubSpot sync  (every 5 min) │
                        │  - High Intent scan (15 min)    │
                        │  - Email validation (30 min)    │
                        │                                 │
                        │  PostgreSQL   Redis (Upstash)   │
                        └────────────────────────────────┘
                                  │           │
                           HubSpot CRM   ZeroBounce API
                           Smartlead API
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI |
| UI | Jinja2 templates + Tailwind CSS CDN + HTMX |
| ORM | SQLAlchemy 2.x (mapped_column style) |
| Migrations | Alembic |
| Database | PostgreSQL (Railway plugin) |
| Task queue | Celery + Redis (Upstash TLS) |
| Email outreach | Smartlead |
| CRM | HubSpot (Private App, REST API v3/v4) |
| Email validation | ZeroBounce Batch API |
| Deployment | Railway (two services: web + worker) |
| Python | 3.11+ |

---

## Data Model

### `prospects`

Core record for every investor contact.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | Primary key |
| first_name, last_name | str | |
| email | str | Unique |
| company, title | str | |
| linkedin_url | text | |
| phone | str | |
| geography | str | City/state |
| asset_class_preference | str | `PE` / `RE` / `both` |
| net_worth_estimate | str | Bucketed range string |
| wealth_tier | str | `mass_affluent` / `HNWI` / `UHNWI` / `institutional` |
| investor_type | str | `individual` / `family_office` / `RIA` / `broker_dealer` / `endowment` / `pension` / `other` |
| source | str | `apollo` / `manual` |
| verified_email | bool | Legacy field — superceded by ZeroBounce |
| accredited_status | str | `unverified` / `pending` / `verified` / `failed` |
| email_validation_status | str | `valid` / `invalid` / `catch-all` / `unknown` — set by ZeroBounce |
| email_validated_at | timestamptz | When ZeroBounce last ran on this email |
| created_at, updated_at | timestamptz | |

### `sequence_enrollments`

One row per (prospect × campaign) enrollment.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| prospect_id | UUID | FK → prospects |
| smartlead_campaign_id | str | Smartlead campaign integer ID (stored as str) |
| campaign_name | str | Human-readable name stored at enrollment time |
| high_intent_campaign_id | str | Populated when High Intent scan upgrades this enrollment |
| sequence_type | str | `RE_DEAL` / `RE_FUND` / `PE_DEAL` / `PE_FUND` |
| track | str | `standard` / `high_intent` |
| status | str | `active` / `completed` / `opted_out` / `bounced` |
| enrolled_at | timestamptz | |
| high_intent_switched_at | timestamptz | |
| opted_out_at, completed_at | timestamptz | |

### `email_events`

One row per Smartlead webhook event.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| prospect_id | UUID | nullable — unknown sender emails are recorded with NULL |
| enrollment_id | UUID | nullable |
| event_type | str | `sent` / `open` / `click` / `reply` / `bounce` / `unsubscribe` / `complete` |
| email_subject | text | |
| domain_used | str | Sending domain |
| clicked_url | text | For click events |
| smartlead_message_id | str | Unique — prevents duplicate inserts on webhook retries |
| hubspot_synced_at | timestamptz | NULL until synced; used by 5-min batch task |
| raw_payload | text | Full Smartlead JSON for debugging |
| occurred_at | timestamptz | |

---

## Sequence / Campaign Logic

Campaigns are created manually in Smartlead. Each campaign targets a specific sequence type:

| Sequence Type | Description |
|---------------|-------------|
| `RE_DEAL` | Real estate deal-by-deal investors |
| `RE_FUND` | Real estate fund investors |
| `PE_DEAL` | Private equity deal-by-deal investors |
| `PE_FUND` | Private equity fund investors |

### High Intent Upgrade

A background Celery task (`scan_high_intent`, every 15 min) looks for `active` / `standard` track enrollments where the prospect has:
- Opened ≥ 3 emails, **and**
- Clicked ≥ 1 link

When matched, the prospect is enrolled in the corresponding High Intent campaign in Smartlead, and the enrollment `track` is updated to `high_intent`.

### Enrollment Rules

- A prospect **must** have `email_validation_status = 'valid'` to be enrolled. `null`, `unknown`, `catch-all`, and `invalid` are all blocked.
- On **reply** or **sequence complete** event: enrollment `status` is set to `completed`.
- On **bounce**: enrollment `status` is set to `bounced`.
- On **unsubscribe**: enrollment `status` is set to `opted_out`.

---

## Integrations

### Smartlead

- **Outbound**: `POST /api/v1/leads` to add a prospect to a campaign on enrollment.
- **Inbound**: Webhook at `POST /webhooks/smartlead` receives all email events.
- Custom fields (geography, investor type, net worth, etc.) are passed at enrollment time as `custom_fields` nested object — not flat on the lead object.
- Supported event types (with all Smartlead aliases handled):
  `EMAIL_SENT`, `EMAIL_OPEN`/`EMAIL_OPENED`, `EMAIL_LINK_CLICKED`/`EMAIL_CLICKED`/`EMAIL_LINK_CLICK`, `EMAIL_REPLIED`/`EMAIL_REPLY`, `EMAIL_BOUNCED`/`EMAIL_BOUNCE`, `LEAD_UNSUBSCRIBED`/`LEAD_UNSUBSCRIBE`, `LEAD_COMPLETED_SEQUENCE`/`SEQUENCE_COMPLETED`

### HubSpot

- Auth: Private App token (Bearer header). Set up under **HubSpot Settings → Integrations → Private Apps**.
- Required scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.objects.deals.write`
- **Contact upsert**: batch upsert by email every 5 min for all unsynced events.
- **Note creation**: one note per email event, associated to the contact.
- **Deal creation**: on `reply` event, a deal named `"WCP Automated Outbound - {prospect name}"` is created in the configured pipeline/stage.
- Pipeline ID and deal stage are configured via env vars (`HUBSPOT_DEAL_PIPELINE_ID`, `HUBSPOT_DEAL_STAGE_ID`).

### ZeroBounce

- **Batch validation** runs every 30 min via Celery — picks up all prospects with `email_validation_status IS NULL` and validates in batches of 200.
- Status mapping: `valid` → valid; `catch-all` → catch-all; `invalid` / `spamtrap` / `abuse` / `do_not_mail` / `disposable` → invalid; everything else → unknown.
- Credits remaining are shown live on the dashboard overview (turns red below 500).

---

## Dashboard

All routes live under `/dashboard/` and require login (password set via `DASHBOARD_PASSWORD` env var). In development with no password set, auth is bypassed.

| Route | Description |
|-------|-------------|
| `/dashboard/` | Overview: KPI cards, engagement rates, funnel chart, activity feed, ZeroBounce credits |
| `/dashboard/prospects` | Prospect list with search, filters, pagination, bulk enrollment |
| `/dashboard/prospects/new` | Add single prospect manually |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST handler for bulk enrollment from list |
| `/dashboard/prospects/{id}` | Prospect detail page |
| `/dashboard/prospects/{id}/edit` | Edit all prospect fields |
| `/dashboard/prospects/{id}/delete` | DELETE prospect (cascades enrollments + events) |
| `/dashboard/fragments/activity` | HTMX fragment — auto-refreshes every 30s |

### Filtering on Prospect List

- Free-text search (name, email, company)
- Enrolled / Not Enrolled
- Email validation status (valid, invalid, catch-all, unknown)
- Investor Type
- Wealth Tier
- Pagination preserves all active filters

### All Times

All dates/times displayed in US/Eastern timezone via a custom Jinja2 `to_et` filter.

---

## Getting Started (Local)

### Prerequisites

- Python 3.11+
- PostgreSQL running locally
- Redis (or use Upstash — set `REDIS_URL` in `.env`)

### Setup

```bash
# Clone the repo
git clone <repo-url>
cd wcp-outbound-platform

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

# Install dependencies
pip install -r requirements.txt

# Copy env file and fill in values
cp .env.example .env
# Edit .env — at minimum: DATABASE_URL, REDIS_URL, SMARTLEAD_API_KEY

# Run migrations
alembic upgrade head

# Start the web server
uvicorn app.main:app --host 0.0.0.0 --port 8000
# NOTE: do NOT use --reload; it serves stale code unreliably
```

### Running Celery Locally (Windows)

On Windows, Celery requires `--pool=solo`. Beat must run in the same process or separately.

```bash
# Combined worker + beat (Windows)
venv\Scripts\celery.exe -A app.worker worker --beat -l info --pool=solo
```

On Linux/Mac, `--pool=solo` is not required but won't hurt.

---

## Environment Variables

All variables are listed in `.env.example`. Copy it to `.env` and fill in values before running.

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string — must use `postgresql+psycopg://` scheme |
| `REDIS_URL` | Yes | Redis broker URL — use `rediss://` for TLS (Upstash/Railway) |
| `SMARTLEAD_API_KEY` | Yes | Smartlead API key |
| `SMARTLEAD_WEBHOOK_SECRET` | No | Shared secret for webhook verification (not currently enforced by Smartlead) |
| `HUBSPOT_ACCESS_TOKEN` | Yes | HubSpot Private App bearer token |
| `HUBSPOT_DEAL_PIPELINE_ID` | Yes | HubSpot pipeline ID for reply deals (`default` works for default pipeline) |
| `HUBSPOT_DEAL_STAGE_ID` | Yes | HubSpot deal stage ID where new deals are created |
| `API_KEY` | No | X-API-Key for REST API endpoints. Empty = auth disabled (dev only) |
| `DASHBOARD_USERNAME` | Yes (prod) | Login username for dashboard |
| `DASHBOARD_PASSWORD` | Yes (prod) | Login password for dashboard. Empty = auth disabled (dev only) |
| `SESSION_SECRET` | Yes (prod) | Random secret for session cookie signing (use 64+ random chars) |
| `ZEROBOUNCE_API_KEY` | Yes | ZeroBounce API key for email validation |

> **Important**: `DATABASE_URL` must use `postgresql+psycopg://` not `postgresql://`. This fix must be applied in both `app/database.py` and `migrations/env.py` — Railway provides the URL with the plain scheme and the code remaps it on startup.

---

## Database Migrations

Migrations are managed with Alembic.

```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration after changing models
alembic revision --autogenerate -m "describe the change"

# View migration history
alembic history
```

Migration files live in `migrations/versions/`. Always review autogenerated migrations before applying — Alembic doesn't always detect column type changes correctly.

---

## Running the Worker

The Celery worker runs the three background tasks:

| Task | Schedule | Description |
|------|----------|-------------|
| `scan_high_intent` | Every 15 min | Upgrades high-engagement prospects to High Intent campaign |
| `sync_to_hubspot` | Every 5 min | Syncs unsynced email events → HubSpot contacts + notes |
| `validate_emails` | Every 30 min | Runs ZeroBounce batch validation on unvalidated emails |

On Railway, the worker runs as a separate service (see below). Locally:

```bash
venv\Scripts\celery.exe -A app.worker worker --beat -l info --pool=solo
```

---

## Railway Deployment

The app runs as **two Railway services** from the same GitHub repo.

### Service 1 — `web`

- Config file: `railway.toml`
- Start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Runs migrations on every deploy before starting the server.
- Health check: `GET /health`

### Service 2 — `worker`

- Config file: `railway.worker.toml`
- Start command: `celery -A app.worker worker --beat -l info --pool=solo`
- No `--pool=solo` needed on Linux — but it doesn't hurt; leave it in for consistency.
- In Railway: set **Config File Path** to `railway.worker.toml` in the service settings.

### Environment Variables

Set all variables from `.env.example` in each service via the Railway dashboard **+ New Variable** form.

> **Never use the Raw Editor** in Railway to set env vars — it overwrites all existing variables.

Both services need: `DATABASE_URL`, `REDIS_URL`, `SMARTLEAD_API_KEY`, `HUBSPOT_ACCESS_TOKEN`, `HUBSPOT_DEAL_PIPELINE_ID`, `HUBSPOT_DEAL_STAGE_ID`, `ZEROBOUNCE_API_KEY`, `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, `SESSION_SECRET`.

### Database

Railway PostgreSQL plugin provides `DATABASE_URL`. The app remaps the scheme from `postgresql://` to `postgresql+psycopg://` at startup in `app/database.py` and `migrations/env.py`.

### Redis

Using Upstash Redis (external). Set `REDIS_URL` to the `rediss://` TLS connection string from Upstash. Both `broker_use_ssl` and `redis_backend_use_ssl` are configured with `ssl_cert_reqs: CERT_NONE` to work with Upstash's certificate.

---

## Outstanding / To-Do

### Blocked — Smartlead Webhook Bug

Open/click events are tracked inside Smartlead's UI but the webhooks do **not fire** for those event types. Sent events work fine. A support ticket has been submitted with Smartlead.

- **Impact**: Click-triggered HubSpot contact creation + note is blocked. High Intent scan relies on click count — will not trigger until clicks are received.
- **Next step**: Retest open/click webhooks once Smartlead resolves the bug. Reply and unsubscribe events also need retesting.

### Configuration Needed (Manual Steps)

- [ ] **Task #8 — Negative reply keywords in Smartlead**: In each campaign's settings, configure reply keywords like `"not interested"`, `"unsubscribe"`, `"stop"`, `"remove me"` to fire `LEAD_UNSUBSCRIBED` instead of `EMAIL_REPLIED`. This ensures negative replies mark the enrollment as opted-out rather than completed.
- [ ] **ZeroBounce API key on Railway worker service**: Confirm `ZEROBOUNCE_API_KEY` is set in the Railway `worker` service environment variables (was added to `web` but may need to be added separately to `worker`).

### End-to-End Tests Pending

Once the Smartlead webhook bug is resolved:

- [ ] **Open/click → HubSpot note**: Verify open and click events arrive via webhook, are stored, synced to HubSpot as notes on the contact.
- [ ] **Reply → HubSpot deal**: Verify reply event → enrollment marked `completed` + deal created in HubSpot with correct pipeline/stage.
- [ ] **High Intent upgrade**: Verify that after ≥ 3 opens + ≥ 1 click, the 15-min scan enrolls the prospect in the High Intent campaign and updates the enrollment track.

### Future Enhancements

- [ ] Spam event type mapping — waiting on Smartlead to confirm the event name they send for spam reports.
- [ ] Email sequence copy — being written separately. Will use Smartlead custom field variables (geography, investor type, etc.) for personalization.
- [ ] Existing enrollments before `campaign_name` was added show `sequence_type` as the badge label — no backfill needed unless it becomes a reporting issue.
- [ ] REST API (`/prospects` endpoints) currently protected by `X-API-Key` header. If the API is used externally, document the key and endpoint contract.

---

## Known Issues / Gotchas

- **Tailwind CDN**: The project uses the Tailwind CDN `<script>` tag. The CDN does **not** support `@apply` — use plain utility classes only. If you add custom CSS, put it in `<style>` tags with raw CSS, not `@apply`.
- **SQLAlchemy `updated_at`**: The `onupdate=func.now()` only fires on ORM-level updates, not raw SQL updates. Use ORM updates or update `updated_at` manually when running raw SQL.
- **Celery on Windows**: Must use `--pool=solo`. The default prefork pool does not work on Windows.
- **SessionMiddleware order**: In `app/main.py`, middleware is added in reverse — `SessionMiddleware` must be added **last** (making it outermost) so the session is populated before `DashboardAuthMiddleware` runs.
