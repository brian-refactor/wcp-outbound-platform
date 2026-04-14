# WCP Outbound Platform

An internal investor acquisition platform for Willow Creek Partners. Automates cold email outreach via Smartlead, tracks engagement events, syncs activity to HubSpot CRM, validates prospect email addresses via ZeroBounce, generates AI-powered personalized email openers via Claude, sources new leads from SEC EDGAR Form D filings, and enriches contacts via Apollo.io and Hunter.io — all managed through a private web dashboard.

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

1. **Source leads from SEC EDGAR** via the EDGAR Lead Finder (`/dashboard/edgar`). Search public Form D filings (private placements) by keyword, state, and date to find fund principals, family office operators, and real estate investors. Results are enriched automatically via Apollo.io and Hunter.io before being added to the prospect list.
2. **Import prospects** via CSV upload or manual entry form.
3. **Validate email addresses** automatically via ZeroBounce (runs every 30 min, and immediately on prospect add). Only `valid` emails can be enrolled in campaigns.
4. **Generate personalized email openers** per prospect using Claude (Anthropic). A 1–2 sentence opener tailored to each investor's profile (type, geography, asset class, wealth tier) is passed to Smartlead as a custom field at enrollment time.
5. **Enroll prospects** into Smartlead email sequences — individually from the edit or detail page, or in bulk from the list view. Duplicate enrollments in the same active campaign are blocked.
6. **Receive Smartlead webhooks** for every email event (sent, open, click, reply, bounce, unsubscribe, sequence complete). Events are stored in the `email_events` table.
7. **High Intent scan** runs every 15 min — if a prospect has ≥ 1 link click older than 48 hours with no reply, they are moved to a "High Intent" Smartlead campaign automatically.
8. **Sync to HubSpot** every 15 min — upserts contacts and creates a CRM note for each email event. On reply, a Deal is created in HubSpot.
9. **Dashboard** shows live KPI stats, campaign funnel charts, recent activity feed, and a ZeroBounce credit balance in the sidebar.

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
                        │  - EDGAR lead finder            │
                        │                                 │
  Smartlead ───webhook──▶│                                │
                        │                                 │
                        │  worker service (Celery+Beat)   │
                        │  - HubSpot sync  (every 15 min)│
                        │  - High Intent scan (15 min)    │
                        │  - Email validation (30 min)    │
                        │                                 │
                        │  PostgreSQL   Redis (Upstash)   │
                        └────────────────────────────────┘
                                  │           │
                     HubSpot CRM   ZeroBounce API
                     Smartlead API  Anthropic (Claude)
                     Apollo.io      Hunter.io
                     SEC EDGAR (public)
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
| Contact enrichment | Apollo.io (People Match) + Hunter.io (email finder) |
| Lead sourcing | SEC EDGAR Form D public API |
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
| personalized_intro | text | AI-generated email opener (Claude). Passed to Smartlead as `{{custom_fields.personalized_intro}}`. Falls back to rule-based opener if Claude unavailable. |
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
| hubspot_synced_at | timestamptz | NULL until synced; used by 15-min batch task |
| raw_payload | text | Full Smartlead JSON for debugging |
| occurred_at | timestamptz | |

### `saved_searches`

Named EDGAR search queries for quick re-use.

| Column | Type | Notes |
|--------|------|-------|
| id | str (UUID) | Primary key |
| name | str | User-defined label |
| params | str | JSON string: `{keywords, state, start_date, end_date}` |
| created_at | timestamptz | |

---

## Campaign Logic

Campaigns are created and managed manually in Smartlead. Each campaign is identified by its Smartlead ID and name.

### High Intent Upgrade

A background Celery task (`scan_high_intent`, every 15 min) looks for `active` / `standard` track enrollments where the prospect has:
- ≥ 1 link click older than 48 hours, **and**
- No reply yet

When matched, the prospect is enrolled in the configured High Intent campaign, and the enrollment `track` is updated to `high_intent`.

### Enrollment Rules

1. A prospect **must** have `email_validation_status = 'valid'` to be enrolled.
2. A `personalized_intro` is generated (Claude or fallback) at enrollment if not already set.
3. Bulk enroll **skips** any prospect already `active` in the target campaign.
4. On **reply** or **sequence complete**: enrollment `status` → `completed`.
5. On **bounce**: enrollment `status` → `bounced`.
6. On **unsubscribe**: enrollment `status` → `opted_out`.

---

## Integrations

### Smartlead

- **Outbound**: `POST /api/v1/campaigns/{id}/leads` to enroll a prospect.
- **Inbound**: Webhook at `POST /webhooks/smartlead` receives all email events.
- **Update settings**: `POST /api/v1/campaigns/{id}/settings` (not PUT/PATCH).
- Custom fields (geography, investor type, wealth tier, personalized_intro, etc.) are passed at enrollment under `"custom_fields"` — not flat on the lead object.
- `unsubscribe_text` = the footer link text in emails (e.g. "Unsubscribe"). **Not** a reply keyword filter.
- To use personalization: add `{{custom_fields.personalized_intro}}` to the email body in Smartlead campaign templates.

### HubSpot

- Auth: Private App token (Bearer header).
- Required scopes: `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`
- **Contact upsert + note**: on click and reply events.
- **Deal creation**: on `reply` event — `"WCP Automated Outbound - {prospect name}"` in the configured pipeline/stage.

### ZeroBounce

- **Batch validation** runs every 30 min — validates all unvalidated emails in batches of 200.
- Also validates immediately when a new prospect is added via the dashboard.
- Credits shown live in the sidebar on every page (HTMX fragment). Turns red below 500.

### Claude (Anthropic)

- Model: `claude-haiku-4-5-20251001`
- Generates a 1–2 sentence personalized email opener per prospect using their investor profile.
- Auto-generated at enrollment; can be regenerated per-prospect or in batch from the list.

### Apollo.io

- Endpoint: `POST https://api.apollo.io/v1/people/match`
- Used in the EDGAR "+ Add" flow to enrich contacts before adding them as prospects.
- Returns: email, linkedin_url, title, phone, city, state, company.
- If Apollo returns no email, Hunter.io is tried next.

### Hunter.io

- Endpoint: `GET https://api.hunter.io/v2/email-finder`
- Fallback email finder after Apollo.
- Params: first_name, last_name, company, api_key.

### SEC EDGAR

- Searches public Form D filings (private placement disclosures) for accredited investor contacts.
- Search: `GET https://efts.sec.gov/LATEST/search-index?forms=D&q=...`
- Form D XML fetched per filing to extract related persons, offering details, and issuer phone.
- Results shown in `/dashboard/edgar` with Google and LinkedIn search shortcuts per contact.
- Supports saved named searches (stored in `saved_searches` table).
- Last search is persisted in the session — navigating away and back restores results.

---

## Dashboard

All routes live under `/dashboard/` and require login (set via `DASHBOARD_PASSWORD` env var).

| Route | Description |
|-------|-------------|
| `/dashboard/` | Overview: KPI cards, funnel chart by campaign, activity feed |
| `/dashboard/prospects` | Prospect list with search, filters, bulk enrollment, batch intro generation |
| `/dashboard/prospects/new` | Add single prospect |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST — bulk enroll (skips already-active duplicates) |
| `/dashboard/prospects/batch-generate-intro` | POST — generate missing Claude intros |
| `/dashboard/prospects/{id}` | Prospect detail — contact card, intro, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all fields + enroll |
| `/dashboard/prospects/{id}/delete` | Delete (cascades enrollments + events) |
| `/dashboard/prospects/{id}/generate-intro` | POST — regenerate Claude intro (HTMX) |
| `/dashboard/sequences` | Campaign performance charts |
| `/dashboard/mailboxes` | Email account warmup status |
| `/dashboard/sync` | HubSpot sync health |
| `/dashboard/edgar` | EDGAR Form D lead finder |
| `/dashboard/edgar/add-prospect` | POST — enrich via Apollo+Hunter, show preview |
| `/dashboard/edgar/confirm-prospect` | POST — save confirmed prospect |
| `/dashboard/edgar/save-search` | POST — save named search |
| `/dashboard/edgar/saved-searches/{id}/delete` | POST — delete saved search |
| `/dashboard/fragments/activity` | HTMX auto-refresh fragment |
| `/dashboard/fragments/zb-credits` | HTMX ZeroBounce credit widget |

---

## Getting Started (Local)

### Prerequisites

- Python 3.12
- PostgreSQL running locally
- Redis (or use Upstash — set `REDIS_URL` in `.env`)

### Setup

```bash
git clone git@github.com:brian-refactor/wcp-outbound-platform.git
cd wcp-outbound-platform

python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum: DATABASE_URL, REDIS_URL, SMARTLEAD_API_KEY

alembic upgrade head

uvicorn app.main:app --host 0.0.0.0 --port 8000
# NOTE: do NOT use --reload
```

### Running Celery Locally

**Mac/Linux:**
```bash
celery -A app.worker worker --beat -l info --pool=solo
```

**Windows:** `--pool=solo` required; beat must run separately.
```bash
venv\Scripts\celery.exe -A app.worker worker -l info --pool=solo
venv\Scripts\celery.exe -A app.worker beat -l info
```

---

## Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `DATABASE_URL` | both | PostgreSQL — must use `postgresql+psycopg://` scheme |
| `REDIS_URL` | both | Redis broker URL — use `rediss://` for TLS (Upstash) |
| `SMARTLEAD_API_KEY` | both | Smartlead API key |
| `HUBSPOT_ACCESS_TOKEN` | both | HubSpot Private App bearer token |
| `HUBSPOT_DEAL_PIPELINE_ID` | both | HubSpot pipeline ID (`default` for default pipeline) |
| `HUBSPOT_DEAL_STAGE_ID` | both | HubSpot deal stage ID |
| `ZEROBOUNCE_API_KEY` | both | ZeroBounce API key |
| `ANTHROPIC_API_KEY` | web | Claude API key — personalized intro generation |
| `APOLLO_API_KEY` | web | Apollo.io People Match — contact enrichment |
| `HUNTER_API_KEY` | web | Hunter.io — email finder fallback after Apollo |
| `API_KEY` | web | X-API-Key for REST API. Empty = disabled |
| `DASHBOARD_USERNAME` | web | Login username |
| `DASHBOARD_PASSWORD` | web | Login password. Empty = auth disabled |
| `SESSION_SECRET` | web | Random 64-char secret for session cookie signing |

> **Note**: `DATABASE_URL` must use `postgresql+psycopg://`. The app remaps it at startup in `app/database.py` and `migrations/env.py`.

> **Note**: Set all variables via Railway dashboard **+ New Variable** — never use the Raw Editor.

---

## Database Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe the change"
alembic history
```

The `web` Railway service runs `alembic upgrade head` automatically on every deploy.

---

## Running the Worker

| Task | Schedule | Description |
|------|----------|-------------|
| `scan_high_intent` | Every 15 min | Upgrades high-engagement prospects to High Intent campaign |
| `sync_to_hubspot` | Every 15 min | Syncs unsynced email events → HubSpot contacts + notes |
| `validate_emails` | Every 30 min | Runs ZeroBounce batch validation on unvalidated emails |

The Celery worker has **no result backend** — all tasks are fire-and-forget. Do not add one; it would exhaust the Upstash Redis free tier.

```bash
# Linux/Railway (combined)
celery -A app.worker worker --beat -l info --pool=solo

# Windows (separate terminals)
venv\Scripts\celery.exe -A app.worker worker -l info --pool=solo
venv\Scripts\celery.exe -A app.worker beat -l info
```

---

## Railway Deployment

Two Railway services from the same GitHub repo. Push to `master` → both services autodeploy.

### Service 1 — `web`
- Config file: `railway.toml`
- Start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### Service 2 — `worker`
- Config file: `railway.worker.toml`
- Start command: `celery -A app.worker worker --beat -l info --pool=solo`
- Set **Config File Path** to `railway.worker.toml` in Railway service settings.

---

## Outstanding / To-Do

### Smartlead Webhook Testing
- [ ] Open event → webhook → EmailEvent recorded
- [ ] Click event → webhook → EmailEvent recorded + HubSpot contact upserted + note
- [ ] Reply event → enrollment completed + HubSpot deal created
- [ ] High Intent upgrade: ≥ 1 click (48h+) + no reply → 15-min scan upgrades track

### Pending Manual Configuration
- [x] Negative reply keywords in Smartlead — set on both campaigns
- [x] `ANTHROPIC_API_KEY` on Railway web service
- [x] `APOLLO_API_KEY` on Railway web service
- [ ] `HUNTER_API_KEY` on Railway web service — add via + New Variable
- [ ] Add `{{custom_fields.personalized_intro}}` to Smartlead email templates

### Future Enhancements
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name
- [ ] REST API documentation — `/prospects` endpoints
- [ ] Prospect activity endpoint `GET /prospects/{id}/activity`
- [ ] Upstash Redis upgrade if usage grows ($10/month for 100M requests)

---

## Known Issues / Gotchas

- **Tailwind CDN**: Does not support `@apply`. Use plain utility classes; custom CSS in `<style>` tags.
- **Jinja2 backslash escaping**: Do NOT use `\'` inside `{{ }}` — causes TemplateSyntaxError. Use `'` freely.
- **Celery on Windows**: Must use `--pool=solo`. Beat must run as a separate process.
- **SessionMiddleware order**: Must be added last in `app/main.py` (making it outermost) so session is populated before `DashboardAuthMiddleware`.
- **Smartlead webhook dedup**: `email_events` uses a composite unique on `(smartlead_message_id, event_type)`.
- **Smartlead `unsubscribe_text`**: This is the footer link text, not a reply keyword filter. Set to `"Unsubscribe"`.
- **Celery result backend**: Do not add one — it would exhaust the Upstash 500k/month free tier.
- **EDGAR API field names**: Use `adsh` for accession number, `ciks[]`/`display_names[]`/`biz_locations[]` are arrays.
- **Bulk enroll duplicates**: Skips prospects already `active` in the target campaign.
