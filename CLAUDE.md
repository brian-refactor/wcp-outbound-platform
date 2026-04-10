# CLAUDE.md — WCP Outbound Platform

This file is read by Claude Code at the start of every session. It contains standing instructions, project context, known gotchas, and outstanding work. Keep it up to date as the project evolves.

---

## Project Overview

Internal investor acquisition platform for Willow Creek Partners. Automates outbound cold email outreach via Smartlead, tracks engagement events, syncs to HubSpot CRM, and validates emails via ZeroBounce. Managed through a private password-protected web dashboard.

**Live URL:** https://web-production-eeb6.up.railway.app

**Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL, Celery 5, Upstash Redis (TLS), Alembic, Jinja2 + HTMX + Tailwind CDN + Chart.js, Railway (two services: web + worker).

---

## Standing Instructions (Always Follow)

### Code Style
- Do not use `--reload` with uvicorn — it serves stale code unreliably. Restart the process manually after edits.
- Do not use `@apply` in Tailwind CSS style blocks — the CDN version does not support it. Use plain CSS properties in `<style>` tags or inline utility classes.
- Do not add error handling, fallbacks, or abstractions beyond what is needed for the task at hand.
- Do not add comments unless the logic is non-obvious.

### Celery (Windows local dev)
- Always use `--pool=solo` on Windows. The default prefork pool silently fails to execute tasks.
- Do NOT use `-B` / `--beat` combined with the worker on Windows — run beat as a separate process.
- On Railway (Linux), combined worker+beat works fine: `celery -A app.worker worker --beat -l info --pool=solo`

### Middleware Order (Starlette/FastAPI)
- `add_middleware` stacks in **reverse**: the last call added is the outermost and runs first.
- `SessionMiddleware` must be added **last** (outermost) so the session is populated before `DashboardAuthMiddleware` runs.
- Never change this order without understanding the implications.

### Database
- `DATABASE_URL` must use `postgresql+psycopg://` scheme, not `postgresql://`.
- Railway's PostgreSQL plugin provides the `postgresql://` scheme — the normalization `.replace("postgresql://", "postgresql+psycopg://", 1)` must exist in **both** `app/database.py` and `migrations/env.py`.
- If you touch either file, verify both still have the fix.

### Railway Environment Variables
- **Never use the Railway Raw Editor** to set env vars — it prepends the `=` sign to values, breaking auth and config.
- Always use the **+ New Variable** button in the Railway Variables tab.
- Both `web` and `worker` services need their own copies of shared variables (DATABASE_URL, REDIS_URL, etc.).

---

## Local Development Commands

```bash
# Activate venv
venv\Scripts\activate

# Run API server (Windows)
venv/Scripts/uvicorn.exe app.main:app --port 8000
# NOTE: no --reload

# Run Celery worker (Windows — pool=solo required)
venv/Scripts/celery.exe -A app.worker worker -l info --pool=solo

# Run Celery beat separately (Windows — cannot combine with worker)
venv/Scripts/celery.exe -A app.worker beat -l info

# Kill all Python processes between restarts
taskkill //F //IM python.exe //T
taskkill //F //IM celery.exe //T

# Apply database migrations
alembic upgrade head

# Create a new migration after changing models
alembic revision --autogenerate -m "describe the change"
```

Swagger UI: http://localhost:8000/docs

---

## Railway Deployment

### Services

| Service | Config File | Start Command |
|---------|-------------|---------------|
| `web` | `railway.toml` | `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `worker` | `railway.worker.toml` | `celery -A app.worker worker --beat -l info --pool=solo` |

The `web` service runs `alembic upgrade head` on every deploy before starting the server — migrations are always applied automatically.

The `worker` service Config File Path must be set to `railway.worker.toml` in the Railway service settings.

### Required Environment Variables (both services unless noted)

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | Railway Postgres plugin — reference as `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | Upstash `rediss://` TLS URL |
| `SMARTLEAD_API_KEY` | Smartlead API key |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot Private App bearer token |
| `HUBSPOT_DEAL_PIPELINE_ID` | HubSpot pipeline for reply deals (`default` for default pipeline) |
| `HUBSPOT_DEAL_STAGE_ID` | Deal stage ID where new reply deals land |
| `ZEROBOUNCE_API_KEY` | ZeroBounce API key — needed on **both** web and worker services |
| `API_KEY` | X-API-Key for REST API auth (web only; empty = disabled) |
| `DASHBOARD_USERNAME` | Dashboard login username (web only) |
| `DASHBOARD_PASSWORD` | Dashboard login password (web only; empty = auth disabled) |
| `SESSION_SECRET` | Random 64-char string for session cookie signing (web only) |

---

## Architecture

```
  Browser ──────────────▶ web service (FastAPI/uvicorn)
                          - Dashboard UI (Jinja2 + HTMX)
                          - REST API /prospects
                          - Webhook receiver /webhooks/smartlead

  Smartlead ───webhook──▶ /webhooks/smartlead

                          worker service (Celery + Beat)
                          - sync_to_hubspot     every 5 min
                          - scan_high_intent    every 15 min
                          - validate_emails     every 30 min

  All services share: PostgreSQL (Railway) + Redis (Upstash)
  External APIs: Smartlead, HubSpot, ZeroBounce
```

---

## Key Files

```
app/
  main.py                  FastAPI app, middleware setup
  config.py                Settings (pydantic-settings, reads .env)
  database.py              SQLAlchemy engine + SessionLocal
  worker.py                Celery app + beat schedule
  models/
    prospect.py            Prospect model
    sequence_enrollment.py SequenceEnrollment model
    email_event.py         EmailEvent model
  routers/
    dashboard.py           All dashboard routes + Jinja2 templates
    webhooks.py            POST /webhooks/smartlead
    prospects.py           REST API /prospects (API-key protected)
    stats.py               Stats endpoints
  integrations/
    smartlead.py           Smartlead API client (enroll, campaigns, mailboxes)
    hubspot.py             HubSpot API client (upsert contacts, notes, deals)
    zerobounce.py          ZeroBounce client (validate_batch, get_credits)
  tasks/
    hubspot_sync.py        Celery task — batch sync email events → HubSpot
    high_intent.py         Celery task — scan and upgrade high-intent enrollments
    email_validation.py    Celery task — batch validate emails via ZeroBounce
  templates/
    base.html              Sidebar layout, nav
    dashboard/
      overview.html        KPI cards, funnel chart, activity feed, ZeroBounce credits
      prospects.html       Prospect list, filters, bulk enrollment
      prospect_detail.html Two-column info card, enrollment history
      prospect_edit.html   Edit form for all prospect fields
      prospect_new.html    Single prospect add form
      import.html          CSV upload
      fragments/
        activity_feed.html HTMX auto-refresh fragment
migrations/
  versions/                Alembic migration files
railway.toml               Web service Railway config
railway.worker.toml        Worker service Railway config
```

---

## Data Model Summary

### `prospects`
Email (unique), name, company, title, phone, linkedin_url, geography, asset_class_preference (PE/RE/both), net_worth_estimate, wealth_tier (mass_affluent/HNWI/UHNWI/institutional), investor_type (individual/family_office/RIA/broker_dealer/endowment/pension/other), source (apollo/manual), accredited_status (unverified/pending/verified/failed), email_validation_status (valid/invalid/catch-all/unknown — set by ZeroBounce), email_validated_at.

### `sequence_enrollments`
prospect_id, smartlead_campaign_id, campaign_name, sequence_type (RE_DEAL/RE_FUND/PE_DEAL/PE_FUND), track (standard/high_intent), status (active/completed/opted_out/bounced), high_intent_campaign_id, timestamps.

### `email_events`
prospect_id (nullable), enrollment_id (nullable), event_type (sent/open/click/reply/bounce/unsubscribe/complete), email_subject, domain_used, clicked_url, smartlead_message_id (unique — dedup), hubspot_synced_at (NULL until synced), raw_payload, occurred_at.

---

## Integration Notes

### Smartlead
- Webhook URL: `https://web-production-eeb6.up.railway.app/webhooks/smartlead`
- No webhook secret signing (Smartlead does not support it).
- Custom fields at enrollment must be nested under `"custom_fields"` key — NOT flat on the lead object.
- All event type variants are mapped (e.g. `EMAIL_REPLY` and `EMAIL_REPLIED` both resolve to `"reply"`).
- Campaigns must be created manually in Smartlead UI. Campaign IDs are integers.

### HubSpot
- Auth: Private App token (Bearer). Create under Settings → Integrations → Private Apps.
- Required scopes: `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`
- click events → upsert contact + note
- reply events → upsert contact + note + Deal named `"WCP Automated Outbound - {name}"`
- sent/open/bounce/unsubscribe → marked synced, no HubSpot API call made

### ZeroBounce
- Batch validates up to 200 emails per API call.
- Status mapping: `valid` → valid; `catch-all` → catch-all; `invalid/spamtrap/abuse/do_not_mail/disposable` → invalid; else → unknown.
- Enrollment is **blocked** for all statuses except `valid` (including `null`).
- Credits shown live on dashboard overview; turns red below 500.

---

## Enrollment Rules

1. `email_validation_status` must equal `"valid"` — null, unknown, catch-all, and invalid are all blocked.
2. On **reply** or **sequence complete** event → enrollment `status = "completed"`.
3. On **bounce** → enrollment `status = "bounced"`.
4. On **unsubscribe** → enrollment `status = "opted_out"`.
5. High Intent upgrade: ≥ 3 opens AND ≥ 1 click → enrolled in High Intent campaign, track set to `"high_intent"`.

---

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/login` | Password login — protected by `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` |
| `/dashboard/` | Overview: KPIs, engagement rates, funnel chart, activity feed, ZeroBounce credits |
| `/dashboard/prospects` | List with search, filters (enrolled, validation status, investor type, wealth tier), pagination, bulk enrollment |
| `/dashboard/prospects/new` | Add single prospect |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST handler for checkbox bulk enrollment |
| `/dashboard/prospects/{id}` | Detail page — contact card, investor profile, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all fields |
| `/dashboard/prospects/{id}/delete` | Delete prospect (cascades enrollments + events) |
| `/dashboard/fragments/activity` | HTMX auto-refresh fragment (every 30s) |

All times displayed in US/Eastern timezone via Jinja2 `to_et` filter.

---

## Outstanding / To-Do

### Blocked — Smartlead Webhook Bug
Open and click events appear in Smartlead's UI but **webhooks do not fire** for those event types. Sent events work. Support ticket submitted.

- **Impact**: High Intent scan won't trigger (needs clicks). Click → HubSpot note blocked.
- **Next step**: Retest once Smartlead support resolves. Also retest reply + unsubscribe end-to-end.

### Pending Configuration (Manual)
- [ ] **Negative reply keywords in Smartlead** — In each campaign's settings, add keywords: `"not interested"`, `"unsubscribe"`, `"stop"`, `"remove me"` so they fire `LEAD_UNSUBSCRIBED` instead of `EMAIL_REPLIED`.
- [ ] **Confirm `ZEROBOUNCE_API_KEY` is set on Railway `worker` service** — was added to `web` but verify it's also in `worker`.

### End-to-End Tests Pending (blocked on webhook bug)
- [ ] Open → webhook fires → EmailEvent recorded
- [ ] Click → webhook fires → EmailEvent recorded → HubSpot contact upserted + note created
- [ ] Reply → enrollment marked `completed` → HubSpot deal created with correct pipeline/stage
- [ ] High Intent upgrade: ≥ 3 opens + ≥ 1 click → 15-min scan upgrades enrollment track

### Future Features
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name for spam reports
- [ ] Email sequence copy — being written separately; uses Smartlead custom field variables for personalization (first_name, last_name, company, geography, investor_type, wealth_tier, asset_class_preference)
- [ ] Polish dashboard UI styling (task #5)
- [ ] REST API documentation — `/prospects` endpoints protected by `X-API-Key` header; document if used externally
- [ ] Prospect activity endpoint `GET /prospects/{id}/activity` — returns full enrollment + event history as JSON

---

## Gotchas Learned in This Project

| Issue | Fix |
|-------|-----|
| Railway Raw Editor mangles env var values | Always use + New Variable form |
| Railway Postgres provides `postgresql://` scheme | Normalize to `postgresql+psycopg://` in BOTH `app/database.py` and `migrations/env.py` |
| Celery on Windows silently drops tasks | Always `--pool=solo`; run beat separately |
| `uvicorn --reload` serves stale code | Never use `--reload`; restart manually |
| Tailwind CDN ignores `@apply` | Use plain CSS in `<style>` tags |
| Starlette middleware runs in reverse order | `SessionMiddleware` must be added last (outermost) |
| Smartlead rejects custom fields flat on lead object | Nest under `"custom_fields"` key |
| Smartlead sends `EMAIL_REPLY` not `EMAIL_REPLIED` | All event type variants are aliased in `SMARTLEAD_EVENT_MAP` |
| HubSpot `hs_timestamp` format | Must be `"%Y-%m-%dT%H:%M:%S.000Z"` (milliseconds required) |
