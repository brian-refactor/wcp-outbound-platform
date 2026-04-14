# CLAUDE.md — WCP Outbound Platform

This file is read by Claude Code at the start of every session. It contains standing instructions, project context, known gotchas, and outstanding work. Keep it up to date as the project evolves.

---

## Project Overview

Internal investor acquisition platform for Willow Creek Partners. Automates outbound cold email outreach via Smartlead, tracks engagement events, syncs to HubSpot CRM, validates emails via ZeroBounce, generates AI-powered personalized email openers via Claude (Anthropic), sources new leads from SEC EDGAR Form D filings, and enriches contacts via Apollo.io and Hunter.io. Managed through a private password-protected web dashboard.

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

### Celery Redis Usage
- Do NOT add a result backend (`backend=`) to the Celery app — tasks are fire-and-forget and results are never read. Adding a backend would hammer Upstash and exceed the free tier limit.
- `task_ignore_result=True`, `worker_send_task_events=False`, `task_send_sent_event=False` must remain set.

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

### UX Pattern — Preview Before Save
- Any flow that calls an external API and then creates a DB record must show a preview/confirm page first. The user must be able to cancel without anything being saved. (This pattern is used in the EDGAR add-prospect flow.)

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
| `APOLLO_API_KEY` | Apollo.io People Match API key — web service only; contact enrichment |
| `HUNTER_API_KEY` | Hunter.io email finder API key — web service only; email fallback after Apollo |
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
                          - EDGAR lead finder /dashboard/edgar

  Smartlead ───webhook──▶ /webhooks/smartlead

                          worker service (Celery + Beat)
                          - sync_to_hubspot     every 15 min
                          - scan_high_intent    every 15 min
                          - validate_emails     every 30 min

  All services share: PostgreSQL (Railway) + Redis (Upstash)
  External APIs: Smartlead, HubSpot, ZeroBounce, Anthropic (Claude),
                 Apollo.io, Hunter.io, SEC EDGAR (public)
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
    saved_search.py        SavedSearch model (EDGAR saved searches)
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
    apollo.py              Apollo.io People Match (contact enrichment)
    hunter.py              Hunter.io email finder (email fallback)
    edgar.py               SEC EDGAR Form D search + XML parser
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
      prospect_edit.html   Edit form for all prospect fields + enrollment + Apollo/Hunter/Google/LinkedIn enrichment
      prospect_new.html    Single prospect add form
      import.html          CSV upload
      sequences.html       Sequence/campaign performance charts and tables
      sync.html            HubSpot sync health page
      leads.html           Apollo people search lead finder (keyword/title/location filters)
      edgar.html           EDGAR Form D lead finder (routes kept, removed from nav)
      edgar_preview.html   Shared preview/confirm page before saving any lead as prospect
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

### `saved_searches`
id, name, params (JSON string of EDGAR search params: keywords/state/start_date/end_date), created_at. Used by the EDGAR lead finder to store named searches.

---

## Integration Notes

### Smartlead
- Webhook URL: `https://web-production-eeb6.up.railway.app/webhooks/smartlead`
- No webhook secret signing (Smartlead does not support it).
- Custom fields at enrollment must be nested under `"custom_fields"` key — NOT flat on the lead object.
- All event type variants are mapped (e.g. `EMAIL_REPLY` and `EMAIL_REPLIED` both resolve to `"reply"`).
- Campaigns must be created manually in Smartlead UI. Campaign IDs are integers.
- `personalized_intro` is passed as a custom field at enrollment. Use `{{custom_fields.personalized_intro}}` in Smartlead email templates.
- `unsubscribe_text` field in Smartlead = the footer text shown at the bottom of emails (e.g. "Unsubscribe"). It is NOT a reply keyword detector. Do not put keywords in this field.
- Update campaign settings via `POST /api/v1/campaigns/{id}/settings` (not PUT or PATCH).

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
- `generate_personalized_intro(prospect)` in `app/integrations/claude_ai.py` generates a 1–2 sentence personalized email opener.
- Called automatically at enrollment time via `_ensure_personalized_intro()` — generates once, reuses after.
- Falls back to rule-based opener if API key missing or generation fails.
- Can be generated/regenerated on demand from the prospect detail page (HTMX button).
- Batch generation available from prospects list.

### Apollo.io
- Endpoint: `POST https://api.apollo.io/v1/people/match`
- Called in two places: (1) EDGAR "+ Add" flow — enriches before showing the preview page; (2) `POST /prospects/{id}/enrich` on the edit page.
- Returns: email, linkedin_url, title, phone, city, state, company.
- If Apollo returns no email, Hunter.io is tried next.

### Hunter.io
- Endpoint: `GET https://api.hunter.io/v2/email-finder`
- Called as fallback after Apollo if no email found. Also callable directly from the prospect edit page.
- Params: first_name, last_name, company, api_key.
- Returns: email, confidence score, number of sources.

### SEC EDGAR (Form D)
- Search endpoint: `GET https://efts.sec.gov/LATEST/search-index`
- Params: `forms=D`, `q`, `locationCode` (2-letter state), `dateRange=custom`, `startdt`, `enddt`, `from`, `size`
- Response field names: `adsh` (accession number), `ciks[]`, `display_names[]`, `biz_locations[]`, `file_date`
- Form D XML URL: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/primary_doc.xml`
- Requires `User-Agent` header per EDGAR access policy.
- XMLs fetched in parallel via `ThreadPoolExecutor(max_workers=10)`.
- Related persons filtered for individuals only (entities with LLC/LP/Fund/etc. suffixes are excluded).
- Deduplicated by (name, entity_name) across filings.

---

## Enrollment Rules

1. `email_validation_status` must equal `"valid"` — null, unknown, catch-all, and invalid are all blocked.
2. `personalized_intro` is generated (Claude or fallback) at enrollment time if not already set.
3. Bulk enroll skips prospects already `active` in the target campaign (duplicate prevention).
4. On **reply** or **sequence complete** event → enrollment `status = "completed"`.
5. On **bounce** → enrollment `status = "bounced"`.
6. On **unsubscribe** → enrollment `status = "opted_out"`.
7. High Intent upgrade: ≥ 1 click older than 48 hours AND no reply → enrolled in High Intent campaign, track set to `"high_intent"`.

---

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/login` | Password login |
| `/dashboard/` | Overview: KPIs, engagement rates, funnel chart by campaign, activity feed |
| `/dashboard/prospects` | List with search, filters, bulk enrollment, batch intro generation |
| `/dashboard/prospects/new` | Add single prospect (validates email via ZeroBounce immediately) |
| `/dashboard/prospects/import` | CSV upload |
| `/dashboard/prospects/bulk-enroll` | POST — bulk enroll selected prospects (skips already-active duplicates) |
| `/dashboard/prospects/batch-generate-intro` | POST — generate Claude intros for selected or all missing (up to 100) |
| `/dashboard/prospects/{id}` | Detail page — contact card, investor profile, personalized intro, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all fields + enroll in sequence + enrichment buttons |
| `/dashboard/prospects/{id}/enrich` | POST — run Apollo/Hunter enrichment, fill blank fields, redirect back to edit |
| `/dashboard/prospects/{id}/delete` | Delete prospect (cascades enrollments + events) |
| `/dashboard/prospects/{id}/generate-intro` | POST — generate/regenerate Claude intro (HTMX) |
| `/dashboard/sequences` | Campaign performance charts and table |
| `/dashboard/mailboxes` | Email account warmup status |
| `/dashboard/sync` | HubSpot sync health — pending count, recent synced events |
| `/dashboard/edgar` | EDGAR Form D lead finder — search, saved searches, Google/LinkedIn shortcuts |
| `/dashboard/edgar/add-prospect` | POST — run Apollo+Hunter enrichment, show preview (no save yet) |
| `/dashboard/edgar/confirm-prospect` | POST — save enriched prospect after user confirms preview |
| `/dashboard/edgar/save-search` | POST — save named search to DB |
| `/dashboard/edgar/saved-searches/{id}/delete` | POST — delete saved search |
| `/dashboard/fragments/activity` | HTMX auto-refresh fragment (every 30s) |
| `/dashboard/fragments/zb-credits` | HTMX fragment — ZeroBounce credit widget in sidebar |

All times displayed in US/Eastern timezone via Jinja2 `to_et` filter.
A `fromjson` filter is also registered for parsing saved search params in templates.

---

## Outstanding / To-Do

### Smartlead Webhook — Previously Blocked, Status Unknown
Open and click webhooks were not firing in earlier testing (sent to Smartlead support). Retest:
- [ ] Open event → webhook → EmailEvent recorded
- [ ] Click event → webhook → EmailEvent recorded → HubSpot contact + note
- [ ] High Intent upgrade: ≥ 1 click older than 48h AND no reply → scan upgrades track

### Pending Configuration (Manual)
- [x] **Negative reply keywords in Smartlead** — set via MCP on both campaigns. Verify in Smartlead UI.
- [x] **Set `ANTHROPIC_API_KEY`** on Railway web service — done and confirmed working.
- [x] **Set `APOLLO_API_KEY`** on Railway web service — done.
- [ ] **Set `HUNTER_API_KEY`** on Railway web service — key provided, needs to be added via + New Variable.
- [ ] **Add `{{custom_fields.personalized_intro}}` to Smartlead email templates** — place as opening line of email body.

### Future Features
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name for spam reports
- [ ] REST API documentation — `/prospects` endpoints protected by `X-API-Key` header
- [ ] Prospect activity endpoint `GET /prospects/{id}/activity` — full enrollment + event history as JSON
- [ ] Upstash Redis upgrade — if request volume grows, upgrade from free tier ($10/month for 100M requests)

### Future Lead Sources
- [ ] **#2 — SEC Form ADV (RIA database)** — Every registered investment adviser files Form ADV with SEC. Free public API via IAPD (SEC Investment Adviser Public Disclosure). Shows firm name, AUM, key personnel. Endpoint: `https://efts.sec.gov/LATEST/search-index?forms=ADV`. RIAs are warm intro path to HNWI clients.
- [ ] **#3 — SEC 13F Filings (institutional investors)** — Institutional managers with >$100M AUM file quarterly 13Fs listing holdings. These are actual investors, not fund managers. Same EDGAR infrastructure already in place (`app/integrations/edgar.py`), different form type.
- [ ] **#4 — Form 990 / Family Foundations** — Family foundations and endowments file public 990s. ProPublica Nonprofit API (`https://projects.propublica.org/nonprofits/api/v2`) exposes foundation name, assets, trustees. Trustees of a $50M+ foundation are prime UHNWI targets.

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
| `smartlead_message_id` unique constraint dropped open/click events | Fixed: composite `(smartlead_message_id, event_type)` constraint |
| Jinja2 `{{ \'s\' }}` causes TemplateSyntaxError | Use `{{ 's' }}` — no backslash escaping inside `{{ }}` blocks |
| Celery worker crashing with Upstash 500k limit | Removed result backend; disabled events; slowed HubSpot sync to 15 min |
| Bulk enroll allowed duplicate active enrollments | Added check: skip if already `active` in target campaign |
| Smartlead `unsubscribe_text` is email footer text, not reply keywords | Set it to "Unsubscribe"; reply keyword detection is a separate Smartlead setting |
| EDGAR API `_id` field includes filename suffix | Use `adsh` for accession number; `ciks[]`/`display_names[]`/`biz_locations[]` are arrays |
| EDGAR campaign update API | Use `POST /api/v1/campaigns/{id}/settings` — not PUT or PATCH |
