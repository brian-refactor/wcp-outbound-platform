# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

# WCP Outbound Platform

This file is read by Claude Code at the start of every session. It contains standing instructions, project context, known gotchas, and outstanding work. Keep it up to date as the project evolves.

---

## Project Overview

Internal investor acquisition platform for Willow Creek Partners. Automates outbound cold email outreach via Smartlead, tracks engagement events, syncs to HubSpot CRM, validates emails via Bouncer, generates AI-powered personalized email openers via Claude (Anthropic), sources new leads via Apollo.io people search, and enriches contacts via Apollo.io and Hunter.io. Managed through a private password-protected web dashboard.

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
- Any flow that calls an external API and then creates a DB record must show a preview/confirm page first. The user must be able to cancel without anything being saved. (This pattern is used in the Lead Finder and EDGAR add-prospect flows — both share `edgar_preview.html` via a `confirm_url` template variable.)

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

**Note:** There are no automated tests in this project. Verification is done manually via the live Railway deployment or local dev server.

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
| `HUBSPOT_DEAL_PIPELINE_ID` | `890766156` — Outbound - Cold Leads pipeline |
| `HUBSPOT_DEAL_STAGE_ID` | `1341410439` — New Lead to Contact stage |
| `BOUNCER_API_KEY` | Bouncer (usebouncer.com) API key — needed on **both** web and worker services |
| `ANTHROPIC_API_KEY` | Claude API key — web service only; used for personalized intro generation |
| `APOLLO_API_KEY` | Apollo.io API key — web service only; enrichment (free) + people search (paid plan required) |
| `HUNTER_API_KEY` | Hunter.io email finder API key — web service only; email fallback after Apollo |
| `GOOGLE_ANALYTICS_PROPERTY_ID` | Numeric GA4 property ID — web service only; enables GA-verified sessions column on Sequences page |
| `GOOGLE_ANALYTICS_CREDENTIALS_JSON` | Service account JSON key content — shared by GA4 and Postmaster Tools |
| `GOOGLE_POSTMASTER_DOMAINS` | Comma-separated sending domains, e.g. `willowcreekinvest.com` — enables `/dashboard/deliverability` |
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
                          - Apollo Lead Finder /dashboard/leads
                          - Monthly Spend Tracker /dashboard/spend
                          - Deliverability /dashboard/deliverability

  Smartlead ───webhook──▶ /webhooks/smartlead

                          worker service (Celery + Beat)
                          - sync_to_hubspot       every 15 min
                          - scan_high_intent      every 15 min
                          - validate_emails       every 30 min
                          - sync_lead_categories  every 15 min

  All services share: PostgreSQL (Railway) + Redis (Upstash)
  External APIs: Smartlead, HubSpot, Bouncer, Anthropic (Claude),
                 Apollo.io, Hunter.io, Google Analytics 4,
                 Google Postmaster Tools
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
    sequence_enrollment.py SequenceEnrollment model (includes smartlead_category)
    email_event.py         EmailEvent model (includes is_ooo, sequence_number)
    campaign_config.py     CampaignConfig — per-campaign HubSpot trigger overrides
    saved_search.py        SavedSearch model (EDGAR saved searches)
    tool_cost.py           ToolCost model (monthly spend tracker)
  routers/
    dashboard.py           All dashboard routes + Jinja2 templates; imports stats functions directly as Python (not HTTP)
    webhooks.py            POST /webhooks/smartlead
    prospects.py           REST API /prospects (API-key protected)
    stats.py               Analytics aggregations (overview_stats, campaigns_funnel, sequence_stats, etc.) — called as Python functions by dashboard.py, also exposed as JSON at /stats/*
  schemas/
    prospect.py            Pydantic models for REST API I/O (ProspectCreate, ProspectOut, EnrollmentOut, etc.)
  dependencies.py          require_api_key FastAPI dependency (X-API-Key header; empty API_KEY bypasses in dev)
  integrations/
    smartlead.py           Smartlead API client (enroll, campaigns, mailboxes, lead categories)
    hubspot.py             HubSpot API client (upsert contacts, notes, deals)
    bouncer.py             Bouncer client (validate_batch, validate_all, get_credits) — primary email validator
    claude_ai.py           Claude API client (generate_personalized_intro)
    apollo.py              Apollo.io — enrich_person (People Match) + search_people (paid)
    hunter.py              Hunter.io email finder (email fallback)
    google_analytics.py    GA4 Data API — session counts by utm_campaign (sl{campaign_id} naming convention)
    postmaster.py          Google Postmaster Tools — domain reputation, spam rate, auth pass rates
    edgar.py               SEC EDGAR Form D search + XML parser (routes kept, not in nav)
    zerobounce.py          ZeroBounce client (legacy — kept for credits display only; validation uses Bouncer)
  tasks/
    hubspot_sync.py        Celery task — batch sync email events → HubSpot
    high_intent.py         Celery task — scan and upgrade high-intent enrollments
    email_validation.py    Celery task — validate_emails (scheduled), revalidate_unknown_emails, validate_selected_emails (on-demand)
    category_sync.py       Celery task — sync Smartlead AI lead categories → sequence_enrollments.smartlead_category
    enrollment.py          Celery task — bulk_enroll_campaign (background bulk enrollment)
  templates/
    base.html              Sidebar layout, nav
    dashboard/
      overview.html        KPI cards, funnel chart, activity feed
      prospects.html       Prospect list, filters, bulk enrollment, batch intro generation, bulk delete
      prospect_detail.html Contact card, investor profile, personalized intro, enrollment history
      prospect_edit.html   Edit form + enrollment + enrichment buttons
      prospect_new.html    Single prospect add form
      prospect_import.html CSV upload + HubSpot list import (multi-state template)
      sequences.html       Campaign performance charts and table (includes GA4 sessions column)
      sequence_detail.html Per-campaign lead-by-lead stats and activity
      sequence_clicks.html Link click tracking across campaigns
      campaign_config.html Per-campaign HubSpot trigger event config (none/open/click/reply)
      leads.html           Apollo people search lead finder
      leads_batch_preview.html  Preview page for batch-adding leads from Apollo
      mailboxes.html       Email account warmup status
      sync.html            HubSpot sync health page
      deliverability.html  Google Postmaster Tools — domain reputation, spam rate, auth rates
      spend.html           Monthly spend tracker + ZeroBounce credits
      edgar.html           EDGAR Form D lead finder (routes kept, removed from nav)
      edgar_preview.html   Shared preview/confirm page before saving any lead as prospect
      fragments/
        activity_feed.html HTMX auto-refresh fragment
        zb_credits.html    HTMX fragment — ZeroBounce credits card (spend page)
        zb_alert.html      HTMX fragment — site-wide low-credit banner (returns empty if ok)
migrations/
  versions/                Alembic migration files
scripts/
  sync_smartlead_enrollments.py  One-time backfill: insert missing enrollment records from Smartlead
  apply_bouncer_logs.py          One-time: apply Bouncer validation results from a CSV log
railway.toml               Web service Railway config
railway.worker.toml        Worker service Railway config
```

---

## Data Model Summary

### `prospects`
Email (unique), first_name, last_name, company, title, phone, linkedin_url, geography, asset_class_preference (PE/RE/both), net_worth_estimate, wealth_tier (mass_affluent/HNWI/UHNWI/institutional), investor_type (individual/family_office/RIA/broker_dealer/endowment/pension/other), source (apollo/manual/referral/linkedin), accredited_status (unverified/pending/verified/failed), email_validation_status (valid/invalid/catch-all/unknown — set by Bouncer), email_validated_at, **personalized_intro** (AI-generated email opener, set by Claude at enrollment time or on demand).

### `sequence_enrollments`
prospect_id, smartlead_campaign_id, campaign_name, track (standard/high_intent), status (active/completed/opted_out/bounced), high_intent_campaign_id, **smartlead_category** (Smartlead AI label e.g. "Interested" — synced every 15 min by category_sync task), timestamps. Note: `sequence_type` has been removed — campaigns are identified by name/ID only.

### `email_events`
prospect_id (nullable), enrollment_id (nullable), event_type (sent/open/click/reply/bounce/unsubscribe/complete), email_subject, domain_used, clicked_url, **sequence_number** (step number in the campaign), smartlead_message_id, composite unique `(smartlead_message_id, event_type)` — prevents duplicate events, **is_ooo** (bool — True if reply is an Out-of-Office auto-reply, excluded from reply counts), hubspot_synced_at, raw_payload, occurred_at.

### `campaign_configs`
smartlead_campaign_id (unique), campaign_name, **hubspot_trigger_event** (none/open/click/reply — overrides global default), hubspot_pipeline_id, hubspot_stage_id. Allows per-campaign control of which event creates a HubSpot deal.

### `saved_searches`
id, name, params (JSON string of EDGAR search params), created_at.

### `tool_costs`
id, name, category (outreach/crm/enrichment/ai/validation/hosting/infrastructure/other), monthly_cost (numeric), status (active/inactive), notes.

---

## Integration Notes

### Smartlead
- Webhook URL: `https://web-production-eeb6.up.railway.app/webhooks/smartlead`
- No webhook secret validation — intentionally omitted.
- Custom fields at enrollment must be nested under `"custom_fields"` key — NOT flat on the lead object.
- All event type variants are mapped (e.g. `EMAIL_REPLY` and `EMAIL_REPLIED` both resolve to `"reply"`).
- OOO replies are detected by text content and marked `is_ooo=True`; excluded from all replied counts.
- Campaigns must be created manually in Smartlead UI. Campaign IDs are integers.
- `personalized_intro` is passed as a custom field at enrollment. Use `{{custom_fields.personalized_intro}}` in Smartlead email templates.
- `unsubscribe_text` field in Smartlead = the footer text shown at the bottom of emails. It is NOT a reply keyword detector.
- Update campaign settings via `POST /api/v1/campaigns/{id}/settings` (not PUT or PATCH).

### HubSpot
- Auth: Private App token (Bearer). Required scopes: `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`, `crm.lists.read`.
- HubSpot list import uses v1 Contacts API (`/contacts/v1/lists`, `/contacts/v1/lists/{id}/contacts/all`).
- Per-campaign HubSpot trigger is configurable via `CampaignConfig`. Default (no config row): reply events only.
- click/reply events → upsert contact + note + Deal named `"WCP Automated Outbound - {name}"`
- sent/open/bounce/unsubscribe → marked synced, no HubSpot API call made
- **Active pipeline:** `890766156` (Outbound - Cold Leads) → stage `1341410439` (New Lead to Contact)

### Bouncer (email validation)
- Primary email validator — replaced ZeroBounce.
- `validate_batch(emails)`: synchronous, up to 10,000 emails per call. Use for small lists.
- `validate_all(emails)`: splits into batches of 20 with 1s delay between requests. Use for all bulk operations — sending large batches at once causes Bouncer to return 'unknown' due to internal timeout.
- Status mapping: `deliverable` + toxicity ≤ 5 → valid; `deliverable` + toxicity > 5 → catch-all; `risky` → catch-all; `undeliverable` → invalid; else → unknown.
- Enrollment is **blocked** for all statuses except `valid` and `catch-all`.
- Scheduled task validates NULL-status prospects every 30 min. On-demand tasks handle `unknown` revalidation and selected-prospect validation.

### Claude (Anthropic)
- Model: `claude-haiku-4-5-20251001` (fast and cheap for short generations).
- `generate_personalized_intro(prospect)` in `app/integrations/claude_ai.py` generates a 1–2 sentence personalized email opener.
- Called automatically at enrollment time via `_ensure_personalized_intro()` — generates once, reuses after.
- Falls back to rule-based opener if API key missing or generation fails.

### Apollo.io
- **Both functions** use `X-Api-Key` header auth (not `api_key` in body).
- **Enrichment** (`enrich_person`): `POST https://api.apollo.io/v1/people/match`. Free tier.
- **People Search** (`search_people`): `POST https://api.apollo.io/v1/mixed_people/api_search`. **Paid plan required.**
- Search results return obfuscated last names and a `has_email` boolean — full data only from `enrich_person`.
- Apollo returns 200 OK with JSON `{"error": "..."}` on failure — `raise_for_status()` won't catch it.
- `api_search` ignores `person_seniority_levels` — use `person_titles` with EXECUTIVE_TITLES constant instead.
- If Apollo returns no email during enrichment, Hunter.io is tried next.

### Google Analytics 4
- `get_email_sessions_by_campaign()` returns `{campaign_id: session_count}` for sessions where utm_source=outbound, utm_medium=email.
- Smartlead email templates must set `utm_campaign=sl{smartlead_campaign_id}` — the `sl` prefix is stripped to match campaign IDs.
- Returns `{}` if `GOOGLE_ANALYTICS_PROPERTY_ID` or `GOOGLE_ANALYTICS_CREDENTIALS_JSON` is not set (column hidden gracefully).

### Google Postmaster Tools
- `get_domain_stats()` returns domain reputation, spam_rate, spf/dkim/dmarc pass rates for each configured domain.
- Reuses `GOOGLE_ANALYTICS_CREDENTIALS_JSON` service account with a different scope (`postmaster.readonly`).
- The service account must be added as Viewer in Postmaster Tools UI and the Gmail Postmaster Tools API must be enabled in the Google Cloud project.
- Returns `[]` if `GOOGLE_POSTMASTER_DOMAINS` is not set — the deliverability page renders gracefully without it.

### Hunter.io
- Endpoint: `GET https://api.hunter.io/v2/email-finder`
- Called as fallback after Apollo if no email found.

### SEC EDGAR (Form D)
- Routes are kept in `app/routers/dashboard.py` but the nav link has been removed.
- Apollo people search is the primary lead source.

---

## Enrollment Rules

1. `email_validation_status` must be `"valid"` or `"catch-all"` — null, unknown, and invalid are blocked.
2. `personalized_intro` is generated (Claude or fallback) at enrollment time if not already set.
3. Bulk enroll skips prospects already `active` in the target campaign (deduped against both DB and Smartlead API).
4. Bulk enrollment runs in the background via the `bulk_enroll_campaign` Celery task — the web request returns immediately.
5. On **reply** or **sequence complete** event → enrollment `status = "completed"`.
6. On **bounce** → enrollment `status = "bounced"`.
7. On **unsubscribe** → enrollment `status = "opted_out"`.
8. High Intent upgrade: ≥ 1 click older than 48 hours AND no reply → enrolled in High Intent campaign, track set to `"high_intent"`.
9. OOO replies (`is_ooo=True`) do NOT complete the enrollment — they are excluded from replied counts everywhere.

---

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/login` | Password login |
| `/dashboard/` | Overview: KPIs, engagement rates, funnel chart by campaign, activity feed |
| `/dashboard/prospects` | List with search, filters, bulk enrollment, bulk delete, batch intro generation |
| `/dashboard/prospects/new` | Add single prospect (validates email via Bouncer immediately) |
| `/dashboard/prospects/import` | Import landing page — CSV upload or HubSpot list import |
| `/dashboard/prospects/import/hubspot` | GET — list picker; POST — dedup + validate + preview |
| `/dashboard/prospects/import/hubspot/confirm` | POST — save confirmed HubSpot contacts |
| `/dashboard/prospects/bulk-enroll` | POST — trigger background bulk enrollment Celery task |
| `/dashboard/prospects/bulk-delete` | POST — delete selected prospects (cascades enrollments + events) |
| `/dashboard/prospects/bulk-validate-emails` | POST — trigger Bouncer validation for selected prospects |
| `/dashboard/prospects/revalidate-unknown` | POST — trigger revalidation of all unknown-status prospects |
| `/dashboard/prospects/batch-generate-intro` | POST — generate Claude intros for selected or all missing (up to 100) |
| `/dashboard/prospects/{id}` | Detail page — contact card, investor profile, personalized intro, enrollment history |
| `/dashboard/prospects/{id}/edit` | Edit all fields + enroll in sequence + enrichment buttons |
| `/dashboard/prospects/{id}/enrich` | POST — run Apollo/Hunter enrichment, fill blank fields |
| `/dashboard/prospects/{id}/delete` | POST — delete prospect (cascades) |
| `/dashboard/prospects/{id}/generate-intro` | POST — generate/regenerate Claude intro (HTMX) |
| `/dashboard/sequences` | Campaign performance charts and table (GA4 sessions column if configured) |
| `/dashboard/sequences/clicks` | Link click tracking — which URLs are being clicked across all campaigns |
| `/dashboard/sequences/{campaign_id}` | Per-campaign detail: lead-by-lead stats and event history |
| `/dashboard/sequences/{campaign_id}/config` | GET/POST — per-campaign HubSpot trigger event config |
| `/dashboard/mailboxes` | Email account warmup status |
| `/dashboard/deliverability` | Google Postmaster Tools — domain reputation, spam rate, auth pass rates |
| `/dashboard/sync` | HubSpot sync health — pending count, recent synced events |
| `/dashboard/leads` | Apollo people search lead finder |
| `/dashboard/leads/add-prospect` | POST — enrich via Apollo+Hunter, show preview |
| `/dashboard/leads/confirm-prospect` | POST — save confirmed prospect |
| `/dashboard/spend` | Monthly spend tracker — run rate, cost/email, cost/lead, tool costs table |
| `/dashboard/spend/add` | POST — add new tool |
| `/dashboard/spend/{id}/update` | POST — update tool cost/status |
| `/dashboard/spend/{id}/delete` | POST — remove tool |
| `/dashboard/edgar` | EDGAR Form D lead finder (routes kept, not in nav) |
| `/dashboard/fragments/activity` | HTMX auto-refresh fragment (every 30s) |
| `/dashboard/fragments/zb-credits` | HTMX fragment — ZeroBounce credits card (spend page) |
| `/dashboard/fragments/zb-alert` | HTMX fragment — site-wide low-credit banner |
| `/dashboard/fragments/revalidate-status` | HTMX fragment — revalidation job progress |

All times displayed in US/Eastern timezone via Jinja2 `to_et` filter.
A `fromjson` filter is also registered for parsing saved search params in templates.

---

## Outstanding / To-Do

### Pending Configuration (Manual)
- [x] All API keys set on Railway.
- [x] Smartlead webhooks, High Intent scan, category sync all verified working.
- [ ] **Google Postmaster Tools** — service account (`GOOGLE_ANALYTICS_CREDENTIALS_JSON`) must be added as Viewer at postmaster.google.com → Settings → Users; Gmail Postmaster Tools API must be enabled in the Google Cloud project; set `GOOGLE_POSTMASTER_DOMAINS` on Railway web service.
- [ ] **GA4 sessions column** — set `GOOGLE_ANALYTICS_PROPERTY_ID` and `GOOGLE_ANALYTICS_CREDENTIALS_JSON` on Railway web service; email templates must set `utm_campaign=sl{smartlead_campaign_id}`.

### Future Features
- [ ] Spam event type mapping — waiting on Smartlead to confirm event name for spam reports
- [ ] Upstash Redis upgrade — if request volume grows, upgrade from free tier ($10/month for 100M requests)

### Future Lead Sources
- [ ] **SEC Form ADV (RIA database)** — Endpoint: `https://efts.sec.gov/LATEST/search-index?forms=ADV`. RIAs are warm intro path to HNWI clients.
- [ ] **SEC 13F Filings (institutional investors)** — Same EDGAR infrastructure (`app/integrations/edgar.py`), different form type.
- [ ] **Form 990 / Family Foundations** — ProPublica Nonprofit API (`https://projects.propublica.org/nonprofits/api/v2`). Trustees of a $50M+ foundation are prime UHNWI targets.

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
| Bulk enroll allowed duplicate active enrollments | Added check: skip if already `active` in target campaign (deduped against both DB and Smartlead API) |
| Smartlead `unsubscribe_text` is email footer text, not reply keywords | Set it to "Unsubscribe"; reply keyword detection is a separate Smartlead setting |
| Apollo `people/search` returns `API_INACCESSIBLE` | Search requires paid plan + `X-Api-Key` header; free tier only covers `people/match` |
| Apollo returns 200 OK with JSON `{"error": "..."}` | `raise_for_status()` won't catch it — check `if "error" in data: raise RuntimeError(data["error"])` |
| Apollo `api_search` ignores `person_seniority_levels` | Use `person_titles` with EXECUTIVE_TITLES constant instead |
| Multiple Alembic heads block `alembic upgrade head` | Create a merge migration with `down_revision = (head1, head2)` and empty upgrade/downgrade |
| Bouncer returns `unknown` for large batches | Use `validate_all()` (batches of 20, 1s delay) for any bulk operation |
| OOO auto-replies were counted as real replies | Detect by text content, set `is_ooo=True`, exclude from all reply counts |
