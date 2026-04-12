# CLAUDE.md — WCP Outbound Platform

This file is read by Claude Code at the start of every session. It contains standing instructions, project context, known gotchas, and outstanding work. Keep it up to date as the project evolves.

---

## Project Overview

Internal investor acquisition platform for Willow Creek Partners. Automates outbound cold email outreach via Smartlead, tracks engagement events, syncs to HubSpot CRM, validates emails via ZeroBounce, and generates AI-powered personalized email openers via Claude (Anthropic). Managed through a private password-protected web dashboard.

**Live URL:** https://web-production-eeb6.up.railway.app

**Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL, Celery 5, Upstash Redis (TLS), Alembic, Jinja2 + HTMX + Tailwind CDN + Chart.js, Railway (two services: web + worker).

---

## Standing Instructions (Always Follow)

### Code Style
- Do not use `--reload` with uvicorn — it serves stale code unreliably. Restart the process manually after edits.
- Do not use `@apply` in Tailwind CSS style blocks — the CDN version does not support it. Use plain CSS properties in `<style>` tags or inline utility classes.
- Do not add error handling, fallbacks, or abstractions beyond what is needed for the task at hand.
- Do not add comments unless the logic is non-obvious.

### Jinja2 Templates
- Do NOT use backslash-escaped quotes (`\'`) inside `{{ }}` expression blocks — this causes a TemplateSyntaxError. Use single quotes freely inside `{{ }}` since HTML attributes use double quotes. e.g. `{{ 's' if x != 1 else '' }}` not `{{ \'s\' if x != 1 else \'\' }}`.

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
| `ANTHROPIC_API_KEY` | Claude API key — web service only; used for personalized intro generation |
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
  External APIs: Smartlead, HubSpot, ZeroBounce, Anthropic (Claude)
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
    claude_ai.py           Claude API client (generate_personalized_intro)
  tasks/
    hubspot_sync.py        Celery task — batch sync email events → HubSpot
    high_intent.py         Celery task — scan and upgrade high-intent enrollments
    email_validation.py    Celery task — batch validate emails via ZeroBounce
  templates/
    base.html              Sidebar layout, nav, ZeroBounce credit widget (HTMX)
    dashboard/
      overview.html        KPI cards, funnel chart, activity feed
      prospects.html       Prospect list, filters, bulk enrollment, batch intro generation
      prospect_detail.html Two-column info card, personalized intro card, enrollment history
      prospect_edit.html   Edit form for all prospect fields + enrollment
      prospect_new.html    Single prospect add form
      import.html          CSV upload
      sequences.html       Sequence/campaign performance charts and tables
      sync.html            HubSpot sync health page
      fragments/
        activity_feed.html HTMX auto-refresh fragment
        zb_credits.html    HTMX fragment for ZeroBounce credit widget in sidebar
migrations/
  versions/                Alembic migration files
railway.toml               Web service Railway config
railway.worker.toml        Worker service Railway config
```

---

## Data Model Summary

### `prospects`
Email (unique), name, company, title, phone, linkedin_url, geography, asset_class_preference (PE/RE/both), net_worth_estimate, wealth_tier (mass_affluent/HNWI/UHNWI/institutional), investor_type (individual/family_office/RIA/broker_dealer/endowment/pension/other), source (apollo/manual/referral/linkedin), accredited_status (unverified/pending/verified/failed), email_validation_status (valid/invalid/catch-all/unknown — set by ZeroBounce), email_validated_at, **personalized_intro** (AI-generated email opener, set by Claude at enrollment time or on demand).

### `sequence_enrollments`
prospect_id, smartlead_campaign_id, campaign_name, track (standard/high_intent), status (active/completed/opted_out/bounced), high_intent_campaign_id, timestamps. Note: `sequence_type` has been removed — campaigns are identified by name/ID only.

### `email_events`
prospect_id (nullable), enrollment_id (nullable), event_type (sent/open/click/reply/bounce/unsubscribe/complete), email_subject, domain_used, clicked_url, smartlead_message_id, event_type composite unique `(smartlead_message_id, event_type)` — prevents duplicate events of different types with the same message ID, hubspot_synced_at (NULL until synced), raw_payload, occurred_at.

---

## Integration Notes

### Smartlead
- Webhook URL: `https://web-production-eeb6.up.railway.app/webhooks/smartlead`
- No webhook secret signing (Smartlead does not support it).
- Custom fields at enrollment must be nested under `"custom_fields"` key — NOT flat on the lead object.
- All event type variants are mapped (e.g. `EMAIL_REPLY` and `EMAIL_REPLIED` both resolve to `"reply"`).
- Campaigns must be created manually in Smartlead UI. Campaign IDs are integers.
- `personalized_intro` is passed as a custom field — add `{{personalized_intro}}` to email templates in Smartlead.

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
- Credits shown live in the sidebar on all pages (HTMX fragment, loads on page load). Turns red below 500.
- Email is validated immediately when a new prospect is added (web request), and also in the background batch task every 30 min.

### Claude (Anthropic)
- Model: `claude-haiku-4-5-20251001` (fast and cheap for short generations).
- `generate_personalized_intro(prospect)` in `app/integrations/claude_ai.py` generates a 1–2 sentence personalized email opener using investor_type, geography, wealth_tier, asset_class_preference, title, company.
- Called automatically at enrollment time via `_ensure_personalized_intro()` — generates once, reuses after.
- If API key is missing or generation fails, falls back to a rule-based opener based on investor_type/asset_class/geography so the Smartlead `{{personalized_intro}}` variable is never blank.
- Can be generated/regenerated on demand from the prospect detail page (HTMX button).
- Batch generation available from prospects list: "Generate N Missing Intros" button (processes up to 100 at a time).

---

## Enrollment Rules

1. `email_validation_status` must equal `"valid"` — null, unknown, catch-all, and invalid are all blocked.
2. `personalized_intro` is generated (Claude or fallback) at enrollment time if not already set.
3. On **reply** or **sequence complete** event → enrollment `status = "completed"`.
4. On **bounce** → enrollment `status = "bounced"`.
5. On **unsubscribe** → enrollment `status = "opted_out"`.
6. High Intent upgrade: ≥ 1 click older than 48 hours AND no reply → enrolled in High Intent campaign, track set to `"high_intent"`.

---

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/login` | Password login — protected by `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` |
| `/dashboard/` | Overview: KPIs, engagement rates, funnel chart by campaign, activity feed. Campaign filter dropdown. |
| `/dashboard/prospects` | List with search, filters (enrolled, campaign, validation status, investor type, wealth tier, intro status), bulk enrollment, batch intro generation |
| `/dashboard/prospects/new` | Add single prospect (validates email via ZeroBounce immediately) |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST — bulk enroll selected prospects into a campaign |
| `/dashboard/prospects/batch-generate-intro` | POST — generate Claude intros for selected or all missing (up to 100) |
| `/dashboard/prospects/{id}` | Detail page — contact card, investor profile, personalized intro card, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all fields + enroll in sequence |
| `/dashboard/prospects/{id}/delete` | Delete prospect (cascades enrollments + events) |
| `/dashboard/prospects/{id}/generate-intro` | POST — generate/regenerate Claude intro for one prospect (HTMX) |
| `/dashboard/sequences` | Campaign performance charts and table |
| `/dashboard/mailboxes` | Email account warmup status |
| `/dashboard/sync` | HubSpot sync health — pending count, recent synced events |
| `/dashboard/fragments/activity` | HTMX auto-refresh fragment (every 30s) |
| `/dashboard/fragments/zb-credits` | HTMX fragment — ZeroBounce credit widget loaded in sidebar |

All times displayed in US/Eastern timezone via Jinja2 `to_et` filter.

---

## Outstanding / To-Do

### Smartlead Webhook — Previously Blocked, Status Unknown
Open and click webhooks were not firing in earlier testing (sent to Smartlead support). Retest:
- [ ] Open event → webhook → EmailEvent recorded
- [ ] Click event → webhook → EmailEvent recorded → HubSpot contact + note
- [ ] High Intent upgrade: ≥ 1 click older than 48h AND no reply → scan upgrades track

### Pending Configuration (Manual)
- [ ] **Negative reply keywords in Smartlead** — In each campaign's settings, add: `"not interested"`, `"unsubscribe"`, `"stop"`, `"remove me"` so they fire `LEAD_UNSUBSCRIBED` instead of `EMAIL_REPLIED`.
- [ ] **Add `{{personalized_intro}}` to Smartlead email templates** — place it where the personalized opener should appear.
- [ ] **Set `ANTHROPIC_API_KEY`** on Railway web service.

### Future Features
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name for spam reports
- [ ] REST API documentation — `/prospects` endpoints protected by `X-API-Key` header
- [ ] Prospect activity endpoint `GET /prospects/{id}/activity` — full enrollment + event history as JSON

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
| `smartlead_message_id` unique constraint dropped open/click events | Fixed: composite `(smartlead_message_id, event_type)` constraint instead of single-column |
| Jinja2 `{{ \'s\' }}` causes TemplateSyntaxError | Use `{{ 's' }}` — no backslash escaping inside `{{ }}` blocks |
