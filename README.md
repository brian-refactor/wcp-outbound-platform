# WCP Outbound Platform

An internal investor acquisition platform for Willow Creek Partners. Automates cold email outreach via Smartlead, tracks engagement events, syncs activity to HubSpot CRM, validates prospect email addresses via ZeroBounce, and generates AI-powered personalized email openers via Claude — all managed through a private web dashboard.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Data Model](#data-model)
- [Campaign Logic](#campaign-logic)
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
2. **Validate email addresses** automatically via ZeroBounce (runs every 30 min, and immediately on prospect add). Only `valid` emails can be enrolled in campaigns.
3. **Generate personalized email openers** per prospect using Claude (Anthropic). A 1–2 sentence opener tailored to each investor's profile (type, geography, asset class, wealth tier) is passed to Smartlead as a custom field at enrollment time.
4. **Enroll prospects** into Smartlead email sequences — individually from the edit or detail page, or in bulk from the list view.
5. **Receive Smartlead webhooks** for every email event (sent, open, click, reply, bounce, unsubscribe, sequence complete). Events are stored in the `email_events` table.
6. **High Intent scan** runs every 15 min — if a prospect has ≥ 1 link click older than 48 hours with no reply, they are moved to a "High Intent" Smartlead campaign automatically.
7. **Sync to HubSpot** every 5 min — upserts contacts and creates a CRM note for each email event. On reply, a Deal is created in HubSpot.
8. **Dashboard** shows live KPI stats, campaign funnel charts, recent activity feed, and a ZeroBounce credit balance in the sidebar.

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
                           Smartlead API  Anthropic (Claude)
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Web framework | FastAPI |
| UI | Jinja2 templates + Tailwind CSS CDN + HTMX + Chart.js |
| ORM | SQLAlchemy 2.x (mapped_column style) |
| Migrations | Alembic |
| Database | PostgreSQL (Railway plugin) |
| Task queue | Celery + Redis (Upstash TLS) |
| Email outreach | Smartlead |
| CRM | HubSpot (Private App, REST API v3/v4) |
| Email validation | ZeroBounce Batch API |
| AI personalization | Anthropic Claude (claude-haiku-4-5) |
| Deployment | Railway (two services: web + worker) |
| Python | 3.12 |

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
| geography | str | e.g. "Southeast US" |
| asset_class_preference | str | `PE` / `RE` / `both` |
| net_worth_estimate | str | Bucketed range string |
| wealth_tier | str | `mass_affluent` / `HNWI` / `UHNWI` / `institutional` |
| investor_type | str | `individual` / `family_office` / `RIA` / `broker_dealer` / `endowment` / `pension` / `other` |
| source | str | `apollo` / `manual` / `referral` / `linkedin` |
| accredited_status | str | `unverified` / `pending` / `verified` / `failed` |
| email_validation_status | str | `valid` / `invalid` / `catch-all` / `unknown` — set by ZeroBounce |
| email_validated_at | timestamptz | When ZeroBounce last validated this email |
| personalized_intro | text | AI-generated email opener (Claude). Passed to Smartlead as `{{personalized_intro}}` custom field. Falls back to rule-based opener if Claude unavailable. |
| created_at, updated_at | timestamptz | |

### `sequence_enrollments`

One row per (prospect × campaign) enrollment.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| prospect_id | UUID | FK → prospects |
| smartlead_campaign_id | str | Smartlead campaign integer ID (stored as str) |
| campaign_name | str | Human-readable name stored at enrollment time |
| high_intent_campaign_id | str | Set when a High Intent campaign is configured at enrollment |
| track | str | `standard` / `high_intent` |
| status | str | `active` / `completed` / `opted_out` / `bounced` |
| enrolled_at | timestamptz | |
| high_intent_switched_at | timestamptz | When the High Intent upgrade occurred |
| opted_out_at, completed_at | timestamptz | |

### `email_events`

One row per Smartlead webhook event.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| prospect_id | UUID | nullable — unknown sender emails recorded with NULL |
| enrollment_id | UUID | nullable |
| event_type | str | `sent` / `open` / `click` / `reply` / `bounce` / `unsubscribe` / `complete` |
| email_subject | text | |
| domain_used | str | Sending domain |
| clicked_url | text | For click events |
| smartlead_message_id | str | Composite unique with event_type — prevents duplicate events of different types per message |
| hubspot_synced_at | timestamptz | NULL until synced; used by 5-min batch task |
| raw_payload | text | Full Smartlead JSON for debugging |
| occurred_at | timestamptz | |

---

## Campaign Logic

Campaigns are created and managed manually in Smartlead. Each campaign is identified by its Smartlead ID and name — there is no `sequence_type` classification in this system.

### High Intent Upgrade

A background Celery task (`scan_high_intent`, every 15 min) looks for `active` / `standard` track enrollments where the prospect has:
- ≥ 1 link click older than 48 hours, **and**
- No reply yet

When matched, the prospect is enrolled in the configured High Intent campaign in Smartlead, and the enrollment `track` is updated to `high_intent`.

### Enrollment Rules

1. A prospect **must** have `email_validation_status = 'valid'` to be enrolled. `null`, `unknown`, `catch-all`, and `invalid` are all blocked.
2. A `personalized_intro` is generated (Claude or fallback) at enrollment if not already set.
3. On **reply** or **sequence complete**: enrollment `status` → `completed`.
4. On **bounce**: enrollment `status` → `bounced`.
5. On **unsubscribe**: enrollment `status` → `opted_out`.

---

## Integrations

### Smartlead

- **Outbound**: `POST /api/v1/campaigns/{id}/leads` to enroll a prospect.
- **Inbound**: Webhook at `POST /webhooks/smartlead` receives all email events.
- Custom fields (geography, investor type, wealth tier, personalized_intro, etc.) are passed at enrollment under `"custom_fields"` — not flat on the lead object.
- Supported event types (all Smartlead aliases handled):
  `EMAIL_SENT`, `EMAIL_OPEN`/`EMAIL_OPENED`, `EMAIL_LINK_CLICKED`/`EMAIL_CLICKED`/`EMAIL_LINK_CLICK`, `EMAIL_REPLIED`/`EMAIL_REPLY`, `EMAIL_BOUNCED`/`EMAIL_BOUNCE`, `LEAD_UNSUBSCRIBED`/`LEAD_UNSUBSCRIBE`, `LEAD_COMPLETED_SEQUENCE`/`SEQUENCE_COMPLETED`
- To use personalization: add `{{personalized_intro}}` to the email body in your Smartlead campaign templates.

### HubSpot

- Auth: Private App token (Bearer header). Set up under **HubSpot Settings → Integrations → Private Apps**.
- Required scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.objects.deals.write`
- **Contact upsert**: batch upsert by email every 5 min for all unsynced events.
- **Note creation**: one note per email event, associated to the contact.
- **Deal creation**: on `reply` event, a deal named `"WCP Automated Outbound - {prospect name}"` is created in the configured pipeline/stage.
- Pipeline ID and deal stage configured via `HUBSPOT_DEAL_PIPELINE_ID` and `HUBSPOT_DEAL_STAGE_ID` env vars.

### ZeroBounce

- **Batch validation** runs every 30 min via Celery — picks up all prospects with `email_validation_status IS NULL` and validates in batches of 200.
- Also validates immediately when a new prospect is added via the dashboard.
- Status mapping: `valid` → valid; `catch-all` → catch-all; `invalid` / `spamtrap` / `abuse` / `do_not_mail` / `disposable` → invalid; everything else → unknown.
- Credits shown live in the sidebar on every page (HTMX fragment loaded on page load). Turns red below 500.

### Claude (Anthropic)

- Model: `claude-haiku-4-5-20251001`
- Generates a 1–2 sentence personalized email opener per prospect using their investor profile.
- Called automatically at enrollment via `_ensure_personalized_intro()` — generates once, reuses on subsequent enrollments.
- If Claude fails or `ANTHROPIC_API_KEY` is not set, falls back to a rule-based opener so the Smartlead `{{personalized_intro}}` variable is never blank.
- Can be generated/regenerated per-prospect from the detail page (HTMX, no page reload).
- Batch generation from the prospects list: "Generate N Missing Intros" button, or select prospects and use bulk bar.

---

## Dashboard

All routes live under `/dashboard/` and require login (password set via `DASHBOARD_PASSWORD` env var). In development with no password set, auth is bypassed.

| Route | Description |
|-------|-------------|
| `/dashboard/` | Overview: KPI cards, funnel chart by campaign, activity feed. Filterable by campaign. |
| `/dashboard/prospects` | Prospect list with search, filters (enrolled, campaign, validation status, investor type, wealth tier, intro status), bulk enrollment, batch intro generation |
| `/dashboard/prospects/new` | Add single prospect (validates email via ZeroBounce immediately) |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST — bulk enroll selected prospects into a campaign |
| `/dashboard/prospects/batch-generate-intro` | POST — generate Claude intros for selected or all missing (up to 100) |
| `/dashboard/prospects/{id}` | Prospect detail — contact card, investor profile, personalized intro, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all prospect fields + enroll in sequence |
| `/dashboard/prospects/{id}/delete` | Delete prospect (cascades enrollments + events) |
| `/dashboard/prospects/{id}/generate-intro` | POST — generate/regenerate Claude intro (HTMX) |
| `/dashboard/sequences` | Campaign performance charts and table |
| `/dashboard/mailboxes` | Email account warmup status |
| `/dashboard/sync` | HubSpot sync health — pending count, recent synced events |
| `/dashboard/fragments/activity` | HTMX fragment — auto-refreshes every 30s |
| `/dashboard/fragments/zb-credits` | HTMX fragment — ZeroBounce credit widget in sidebar |

### Filtering on Prospect List

- Free-text search (name, email, company)
- Enrolled / Not Enrolled
- Campaign (by Smartlead campaign ID)
- Email validation status (valid, invalid, catch-all, unknown, not validated)
- Investor Type
- Wealth Tier
- Personalized Intro (has / missing)
- Pagination preserves all active filters

### All Times

All dates/times displayed in US/Eastern timezone via a custom Jinja2 `to_et` filter.

---

## Getting Started (Local)

### Prerequisites

- Python 3.12
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

On Windows, Celery requires `--pool=solo`. Beat must run separately.

```bash
# Worker
venv\Scripts\celery.exe -A app.worker worker -l info --pool=solo

# Beat (separate terminal)
venv\Scripts\celery.exe -A app.worker beat -l info
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL — must use `postgresql+psycopg://` scheme |
| `REDIS_URL` | Yes | Redis broker URL — use `rediss://` for TLS (Upstash) |
| `SMARTLEAD_API_KEY` | Yes | Smartlead API key |
| `HUBSPOT_ACCESS_TOKEN` | Yes | HubSpot Private App bearer token |
| `HUBSPOT_DEAL_PIPELINE_ID` | Yes | HubSpot pipeline ID (`default` for default pipeline) |
| `HUBSPOT_DEAL_STAGE_ID` | Yes | HubSpot deal stage ID |
| `ZEROBOUNCE_API_KEY` | Yes | ZeroBounce API key (web + worker) |
| `ANTHROPIC_API_KEY` | No | Claude API key — enables AI-generated personalized intros (web only) |
| `API_KEY` | No | X-API-Key for REST API. Empty = auth disabled |
| `DASHBOARD_USERNAME` | Yes (prod) | Login username |
| `DASHBOARD_PASSWORD` | Yes (prod) | Login password. Empty = auth disabled |
| `SESSION_SECRET` | Yes (prod) | Random secret for session cookie signing (64+ chars) |

> **Note**: `DATABASE_URL` must use `postgresql+psycopg://` not `postgresql://`. The app remaps it at startup in `app/database.py` and `migrations/env.py`.

---

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration after changing models
alembic revision --autogenerate -m "describe the change"

# View migration history
alembic history
```

The `web` Railway service runs `alembic upgrade head` automatically on every deploy.

---

## Running the Worker

| Task | Schedule | Description |
|------|----------|-------------|
| `scan_high_intent` | Every 15 min | Upgrades high-engagement prospects to High Intent campaign |
| `sync_to_hubspot` | Every 5 min | Syncs unsynced email events → HubSpot contacts + notes |
| `validate_emails` | Every 30 min | Runs ZeroBounce batch validation on unvalidated emails |

```bash
# Linux/Railway (combined)
celery -A app.worker worker --beat -l info --pool=solo

# Windows (separate terminals)
venv\Scripts\celery.exe -A app.worker worker -l info --pool=solo
venv\Scripts\celery.exe -A app.worker beat -l info
```

---

## Railway Deployment

Two Railway services from the same GitHub repo.

### Service 1 — `web`
- Config file: `railway.toml`
- Start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### Service 2 — `worker`
- Config file: `railway.worker.toml`
- Start command: `celery -A app.worker worker --beat -l info --pool=solo`
- Set **Config File Path** to `railway.worker.toml` in Railway service settings.

Set all env vars via Railway dashboard **+ New Variable** form — never use the Raw Editor.

---

## Outstanding / To-Do

### Smartlead Webhook Testing
Open/click webhooks were not firing in earlier testing (Smartlead support ticket submitted). Retest:
- [ ] Open event → webhook → EmailEvent recorded
- [ ] Click event → webhook → EmailEvent recorded + HubSpot contact upserted + note
- [ ] Reply event → enrollment completed + HubSpot deal created
- [ ] High Intent upgrade: ≥ 1 click (48h+) + no reply → 15-min scan upgrades track

### Pending Manual Configuration
- [ ] **Negative reply keywords in Smartlead** — add `"not interested"`, `"unsubscribe"`, `"stop"`, `"remove me"` to each campaign's reply keywords so negative replies fire `LEAD_UNSUBSCRIBED` not `EMAIL_REPLIED`.
- [ ] **Add `{{personalized_intro}}` to Smartlead email templates** — place it as the opening line of email body.
- [ ] **Set `ANTHROPIC_API_KEY`** on Railway web service to enable Claude-powered intros.

### Future Enhancements
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name for spam reports
- [ ] REST API documentation — `/prospects` endpoints protected by `X-API-Key` header
- [ ] Prospect activity endpoint `GET /prospects/{id}/activity` — full enrollment + event history as JSON

---

## Known Issues / Gotchas

- **Tailwind CDN**: Does not support `@apply`. Use plain utility classes; put custom CSS in `<style>` tags with raw CSS.
- **Jinja2 backslash escaping**: Do NOT use `\'` inside `{{ }}` expression blocks — it causes a TemplateSyntaxError. Use `'` freely inside `{{ }}` since HTML attributes use double quotes.
- **SQLAlchemy `updated_at`**: `onupdate=func.now()` only fires on ORM-level updates. Use ORM updates or manually set `updated_at` when running raw SQL.
- **Celery on Windows**: Must use `--pool=solo`. Beat must run as a separate process on Windows.
- **SessionMiddleware order**: In `app/main.py`, middleware is added in reverse — `SessionMiddleware` must be added **last** (making it outermost) so the session is populated before `DashboardAuthMiddleware` runs.
- **Smartlead webhook dedup**: The `email_events` table uses a composite unique constraint on `(smartlead_message_id, event_type)` — not a single-column constraint on `smartlead_message_id`. This allows different event types (e.g. `sent` and `open`) to share the same message ID without being dropped.
