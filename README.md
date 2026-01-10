# CallRail → CTM Migration Packet Generator

This project generates a **Gmail-safe HTML “migration packet”** for customers migrating from **CallRail** to **CallTrackingMetrics (CTM)**.

The script:
1. Uses a **CallRail API token** to discover **all accounts** the token has access to.
2. For each account, pulls companies, trackers, tracker details, and integrations.
3. Produces a structured, compact HTML document that includes:
   - **Agency + Account identifiers** (IDs and numeric IDs)
   - **Phone inventory** (local vs toll-free; pool vs static where determinable)
   - **Per-tracker configuration details** (tracking numbers, call flow routing, recording settings, swap targets, source metadata, timestamps, etc.)
   - **Integration configuration summary** (Google Ads, GA4, Salesforce, Webhooks, and other integrations)
   - **Warnings** highlighting migration risks (e.g., disabled companies, no trackers, failing integrations)
4. Sends the resulting HTML to a **Make.com webhook**, which then emails it through Gmail.

The output is intended to help CTM implementation/support teams understand the customer’s current CallRail configuration and plan an accurate migration.

---

## Why this exists (CTM migration workflow)

When migrating a customer from CallRail to CTM, the team needs to know:
- Exactly **which tracking numbers** exist (and which trackers own them)
- Whether tracking is **source-based** or **session-based (DNI pools)**
- **Call flows**: routing steps, screening, menu/IVR behavior, voicemail, schedules, simulcall, timeouts, etc.
- Whether **call recording** is enabled and what the customer expects
- Whether **SMS** is enabled and supported on each tracker
- What is being sent to **Google Ads / GA4 / Salesforce / Webhooks**, and what triggers those events/conversions
- Any edge-case configuration that can break during migration (failed integrations, disabled trackers still referenced, missing pools, etc.)

This script captures those details and turns them into a single migration packet that is easy to review/share.

---

## What data is captured

### Account discovery
The script uses the CallRail endpoint:

- `GET /v3/a`

This returns both:
- `agencies[]`
- `accounts[]`

The report prints **Agency ID + Numeric ID** and **Account ID + Numeric ID** at the top.

### For each account
The script pulls:

- `GET /v3/a/{account_id}.json` (account summary)
- `GET /v3/a/{account_id}/companies.json`
- `GET /v3/a/{account_id}/trackers.json?company_id=...`
- `GET /v3/a/{account_id}/trackers/{tracker_id}.json` (per-tracker deep detail)
- `GET /v3/a/{account_id}/integrations.json?company_id=...`

> Note: Call flows are not fetched from a separate endpoint; call flow data is embedded inside the tracker detail payload.

### Phone inventory logic
CallRail does not provide a standalone “numbers” endpoint in this workflow.
Instead, the script derives inventory from each tracker’s `tracking_numbers` list.

It attempts to classify:
- **Toll-free vs Local** by using explicit fields when present, otherwise fallback to NANP toll-free prefixes (800/833/844/855/866/877/888/822).
- **Pool vs Static** where metadata supports it; otherwise it treats numbers as static by default.

### Per-tracker detail capture (high value)
For each tracker, the report includes:
- Tracker name, type (source/session), status
- Destinations (destination number or agent targets)
- **Tracking numbers** (explicit list)
- SMS supported + enabled
- Whisper message (if present)
- Call flow details (type, recording enabled, greeting text, voicemail settings, steps if present)
- Source metadata (search engine/type, medium/campaign when available)
- Swap targets (if present)
- Created timestamp, disabled timestamp (if applicable)

### Integrations capture
The integrations are summarized using an LLM (OpenAI) into structured JSON so we can reliably render:
- Google Ads (status, conversion actions, triggers, fields sent)
- GA4 (status, events, parameters, triggers)
- Salesforce (status, environment, objects, sync direction, events, mapping details)
- Webhooks (status, domains, events, payload destinations)
- Other integrations (name/status/details)

The script *does not* ask the model to write HTML; it only returns JSON.

---

## Example output (high level)

The email report contains sections like:

- **Agencies**
  - `Company Name — Agency ID: ACC... · Numeric: 979...`
- **Accounts**
  - `Company Name — Account ID: ACC... · Numeric: 979...`
- **Companies**
  - Feature badges (DNI, Lead Scoring, CallScribe, etc.)
  - Phone inventory totals (Total / Pool / Static / Local / Toll-free)
  - Trackers grouped by Source vs Session
  - Each tracker showing:
    - Destinations + Tracking Numbers
    - Call flow configuration summary + detailed bullet list
- **Integrations**
  - Google Ads / GA4 / Salesforce / Webhooks / Other integrations
- **Warnings**
  - E.g. “Company status is disabled”, “Google Ads integration is failing…”, “No trackers configured”, etc.

---

## Requirements
- Python 3.10+ (3.11/3.12 recommended)
- `requests`

---

## Installation

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
Configuration
Environment variables
CALLRAIL_API_KEY (required)
OPENAI_API_KEY (required)
Tip: keep secrets out of git. A .env.example is provided; your real .env is gitignored.

## Webhook destination
The script posts a JSON payload to a Make.com webhook:

subject
html
generated_at

The webhook URL is currently defined in the script as DEFAULT_WEBHOOK_URL.
If your team wants this configurable without code edits, consider adding a --webhook-url CLI flag (future improvement).

Running the script
Basic:

python callrail_migration_script.py --log-level INFO
Verbose debugging (prints full tracker JSON payloads to stdout):

python callrail_migration_script.py --log-level DEBUG
Operational considerations / safety
Rate limits / performance
For each company, the script fetches tracker lists and then fetches tracker details per tracker (/trackers/{id}.json).
Large accounts can have hundreds of trackers, which means many requests.

If this becomes an issue:

add caching for tracker detail responses
add concurrency with a bounded thread pool (and backoff)
implement retry with exponential backoff for 429/5xx


## Sensitive data / PII
The report can include:

phone numbers
webhook URLs
greeting text or whispers (may contain internal info)
Treat the output as internal-only unless scrubbed.

## LLM usage
The LLM is used only to:

normalize and summarize integrations/features/warnings into structured JSON
No HTML is generated by the model.

## Repository structure
callrail_migration_script.py — main script
requirements.txt — dependencies
.gitignore — prevents committing secrets/venvs
.env.example — example env var file
README.md — documentation

## Suggested next improvements (engineering backlog)
Add frontend landing page with a form to capture user input: email address and callrail api key. 
Replace webhook to make with a sendgrid template or just print the html packet so it displays in a browser or make it exportable to PDF/DOC.

