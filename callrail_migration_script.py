#!/usr/bin/env python3
"""
CallRail → CTM Migration Packet Generator
Fetches every account accessible by the token.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests
from requests import HTTPError

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------
CALLRAIL_API_ROOT = "https://api.callrail.com/v3"
OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_WEBHOOK_URL = "https://hook.us1.make.com/rjv4nblrc6c2566pn2fg3geo1sltkvla"

# ---------------------------------------------------------------------
# HTML UTILS
# ---------------------------------------------------------------------
BASE_FONT = "font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.35;color:#111827;"

def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def box(bg="#ffffff", border="#e5e7eb", pad=10, margin=8):
    return f"background:{bg};border:1px solid {border};border-radius:8px;padding:{pad}px;margin-bottom:{margin}px;"

def badge(text, bg="#eef2ff", fg="#3730a3"):
    return f'<span style="display:inline-block;background:{bg};color:{fg};padding:1px 5px;border-radius:6px;font-size:10px;margin-right:4px;">{esc(text)}</span>'

# ---------------------------------------------------------------------
# TIME
# ---------------------------------------------------------------------
def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

# ---------------------------------------------------------------------
# CALLRAIL CLIENT
# ---------------------------------------------------------------------
class CallRailClient:
    def __init__(self, account_id: str, api_key: str, logger: logging.Logger):
        self.account_id = account_id
        self.base = f"{CALLRAIL_API_ROOT}/a/{account_id}"
        self.headers = {"Authorization": f'Token token="{api_key}"'}
        self.logger = logger

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params, timeout=30)
        try:
            r.raise_for_status()
        except HTTPError:
            self.logger.error("CallRail error %s: %s", r.status_code, r.text)
            raise
        return r.json()

    def paginate(self, path: str, key: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        out, page = [], 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            out.extend(data.get(key, []))
            if page >= data.get("total_pages", 1):
                break
            page += 1
        return out

# ---------------------------------------------------------------------
# LIST ACCESSIBLE ACCOUNTS
# ---------------------------------------------------------------------
def list_accessible_accounts(api_key: str, logger: logging.Logger) -> Dict[str, List[Dict[str, Any]]]:
    headers = {"Authorization": f'Token token="{api_key}"'}
    params = {"per_page": 100, "page": 1}
    accounts: List[Dict[str, Any]] = []
    agencies: List[Dict[str, Any]] = []
    while True:
        r = requests.get(f"{CALLRAIL_API_ROOT}/a", headers=headers, params=params, timeout=30)
        try:
            r.raise_for_status()
        except HTTPError:
            logger.error("Unable to list accounts (%s): %s", r.status_code, r.text)
            raise
        data = r.json()
        accounts.extend(data.get("accounts", []))
        agencies.extend(data.get("agencies", []))
        if params["page"] >= data.get("total_pages", 1):
            break
        params["page"] += 1
    return {"accounts": accounts, "agencies": agencies}

# ---------------------------------------------------------------------
# CALLRAIL UTILITIES
# ---------------------------------------------------------------------
TOLL_FREE_PREFIXES = {"800", "822", "833", "844", "855", "866", "877", "888"}

def normalize_number(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits

def detect_toll_free(number: str, metadata: Dict[str, Any]) -> bool:
    if metadata.get("toll_free") is not None:
        return bool(metadata["toll_free"])
    clean = normalize_number(number)
    return clean[:3] in TOLL_FREE_PREFIXES

def summarize_numbers_from_trackers(trackers: List[Dict[str, Any]]) -> Dict[str, int]:
    seen: Dict[str, Dict[str, bool]] = {}
    for tracker in trackers:
        for raw_number in tracker.get("tracking_numbers", []):
            entry = {"number": raw_number} if isinstance(raw_number, str) else (raw_number or {})
            number_value = entry.get("number") or entry.get("phone_number") or entry.get("tracking_number") or entry.get("value")
            if not number_value:
                continue
            normalized = number_value.strip()
            data = seen.setdefault(normalized, {"pool": False, "static": False, "toll_free": False})
            number_type = (entry.get("number_type") or entry.get("type") or "").lower()
            if number_type == "pool" or entry.get("pool") is True or entry.get("pool_size"):
                data["pool"] = True
            if number_type == "static" or entry.get("static") is True or not data["pool"]:
                data["static"] = True
            data["toll_free"] = data["toll_free"] or detect_toll_free(normalized, entry)

    pool_count = sum(1 for meta in seen.values() if meta["pool"])
    static_count = sum(1 for meta in seen.values() if not meta["pool"])
    toll_free_count = sum(1 for meta in seen.values() if meta["toll_free"])
    local_count = sum(1 for meta in seen.values() if not meta["toll_free"])

    return {
        "total_numbers": len(seen),
        "pool_numbers": pool_count,
        "static_numbers": static_count,
        "local_numbers": local_count,
        "toll_free_numbers": toll_free_count,
    }

def fetch_tracker_details(client: CallRailClient, trackers: List[Dict[str, Any]], logger: logging.Logger) -> List[Dict[str, Any]]:
    details = []
    for tracker in trackers:
        try:
            detail = client.get(f"/trackers/{tracker['id']}.json")
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Tracker %s payload:\n%s", tracker["id"], json.dumps(detail, indent=2))
            details.append(detail)
        except Exception:
            logger.warning("Could not fetch detail for tracker %s", tracker["id"])
            details.append(tracker)
    return details

# ---------------------------------------------------------------------
# LLM PROMPT
# ---------------------------------------------------------------------
SUMMARY_PROMPT = """
Return STRICT JSON only. No prose. No HTML.

Schema:
{
  "features": [string],

  "phone_inventory": {
    "total_numbers": int,
    "pool_numbers": int,
    "static_numbers": int,
    "local_numbers": int,
    "toll_free_numbers": int
  },

  "trackers": [
    {
      "id": string,
      "name": string,
      "category": "session" | "source",
      "subtype": string,
      "status": "active" | "disabled",
      "destinations": [string],
      "number_count": int,
      "tracking_numbers": [string],
      "sms_support": "yes" | "no" | "not_supported",
      "flow_type": "basic" | "advanced",
      "flow_notes": string | null
    }
  ],

  "integrations": {
    "salesforce": {
      "status": "active" | "inactive" | "missing",
      "environment": string | null,
      "sync_direction": string | null,
      "objects": [string],
      "events": [string],
      "details": [string]
    } | null,

    "webhooks": {
      "status": "active" | "inactive",
      "domains": [string],
      "events": [string],
      "payload": [string]
    } | null,

    "google_ads": {
      "status": "active" | "disabled" | "failed",
      "conversion_actions": [string],
      "triggers": [string],
      "fields_sent": [string]
    } | null,

    "ga4": {
      "status": "active" | "disabled" | "failed",
      "events": [string],
      "parameters": [string],
      "triggers": [string]
    } | null,

    "other_integrations": [
      {
        "name": string,
        "status": "active" | "disabled" | "failed",
        "details": [string]
      }
    ]
  },

  "warnings": [string]
}

Call-flow configuration is embedded in each tracker (whisper, recording, greeting, steps, swap targets, sources).
Focus on:
• Google Ads + GA4 conversion actions/events/triggers/fields.
• Salesforce object mapping, sync direction, triggered events.
• Webhook destinations/payloads.
• Exact tracking numbers per tracker plus inventory totals (pool vs static, toll-free vs local).
"""

def llm_summary(api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        OPENAI_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SUMMARY_PROMPT.strip()}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(payload, indent=2)}],
                },
            ],
            "temperature": 0,
        },
        timeout=180,
    )

    if not r.ok:
        raise RuntimeError(f"OpenAI error {r.status_code}: {r.text}")

    data = r.json()
    text = data["output"][0]["content"][0]["text"]
    return json.loads(text)

# ---------------------------------------------------------------------
# RENDER HELPERS
# ---------------------------------------------------------------------
def describe_call_flow(flow: Dict[str, Any]) -> List[str]:
    if not flow:
        return []
    lines = []
    flow_type = flow.get("type")
    if flow_type:
        lines.append(f"Flow Type: {flow_type}")
    if flow.get("recording_enabled") is not None:
        lines.append(f"Recording: {'ON' if flow['recording_enabled'] else 'off'}")
    if flow.get("voicemail_enabled") is not None:
        lines.append(f"Voicemail: {'enabled' if flow['voicemail_enabled'] else 'disabled'}")
    if flow.get("greeting_text"):
        lines.append(f"Greeting: {flow['greeting_text']}")
    if flow.get("whisper_message"):
        lines.append(f"Whisper: {flow['whisper_message']}")
    steps = flow.get("steps") or []
    if steps:
        step_desc = []
        for step in steps:
            action = step.get("action") or step.get("type") or "step"
            target = step.get("destination_number") or step.get("agent") or step.get("value")
            if target:
                step_desc.append(f"{action}→{target}")
            else:
                step_desc.append(action)
        lines.append(f"Steps: {', '.join(step_desc[:8])}" + ("…" if len(step_desc) > 8 else ""))
    return lines

def describe_source(source: Dict[str, Any]) -> List[str]:
    if not source:
        return []
    lines = [f"Source Type: {source.get('type')}"]
    if source.get("search_engine"):
        lines.append(f"Search Engine: {source['search_engine']}")
    if source.get("search_type"):
        lines.append(f"Search Type: {source['search_type']}")
    if source.get("medium"):
        lines.append(f"Medium: {source['medium']}")
    if source.get("campaign"):
        lines.append(f"Campaign: {source['campaign']}")
    return lines

def render_tracker(t: Dict[str, Any], detail: Optional[Dict[str, Any]]) -> str:
    destinations = ", ".join(map(esc, t.get("destinations") or [])) or "—"
    tracking_numbers = ", ".join(t.get("tracking_numbers") or []) or "—"
    flow_notes = f" ({esc(t['flow_notes'])})" if t.get("flow_notes") else ""

    detail_lines: List[str] = []
    if detail:
        if detail.get("whisper_message"):
            detail_lines.append(f"Whisper: {detail['whisper_message']}")
        if detail.get("sms_enabled") is not None:
            detail_lines.append(f"SMS Enabled: {'yes' if detail['sms_enabled'] else 'no'}")
        if detail.get("destination_number"):
            detail_lines.append(f"Primary Destination: {detail['destination_number']}")
        detail_lines.extend(describe_call_flow(detail.get("call_flow") or {}))
        detail_lines.extend(describe_source(detail.get("source") or {}))
        if detail.get("swap_targets"):
            detail_lines.append(f"Swap Targets: {', '.join(detail['swap_targets'])}")
        if detail.get("created_at"):
            detail_lines.append(f"Created: {detail['created_at']}")
        if detail.get("disabled_at"):
            detail_lines.append(f"Disabled: {detail['disabled_at']}")

    detail_html = "".join(
        f"<div style='font-size:11px;color:#4b5563;margin-left:8px;'>• {esc(line)}</div>"
        for line in detail_lines
    )

    return (
        "<div style='margin-bottom:4px;'>"
        f"<div><strong>{esc(t.get('name', 'Unnamed Tracker'))}</strong> "
        f"{badge(t['category'])}{badge(t['subtype'], '#f0fdf4', '#166534')}"
        f"{badge(t['status'], '#ecfeff', '#155e75')}</div>"
        f"<div style='font-size:11px;color:#374151;margin-left:6px;'>"
        f"Destinations: {destinations} · Tracking Numbers: {esc(tracking_numbers)}<br>"
        f"Numbers: {t['number_count']} · SMS: {t['sms_support'].upper()} · "
        f"Flow: {t['flow_type'].upper()}{flow_notes}"
        "</div>"
        f"{detail_html}"
        "</div>"
    )

def render_inventory(inv: Dict[str, Any]) -> str:
    return (
        "<div style='font-size:11px;color:#374151;'>"
        f"Total: <strong>{inv['total_numbers']}</strong> · Pool: {inv['pool_numbers']} · "
        f"Static: {inv['static_numbers']} · Local: {inv['local_numbers']} · "
        f"Toll-free: {inv['toll_free_numbers']}"
        "</div>"
    )

def render_integration_block(title: str, data: Dict[str, Any]) -> str:
    if not data:
        return ""
    lines = []
    for key, value in data.items():
        if not value:
            continue
        label = key.replace("_", " ").title()
        if isinstance(value, list):
            lines.append(f"<div style='font-size:11px;margin-left:8px;'>• {esc(label)}: {esc(', '.join(value))}</div>")
        else:
            lines.append(f"<div style='font-size:11px;margin-left:8px;'>• {esc(label)}: {esc(str(value))}</div>")
    if not lines:
        return ""
    return f"<div style='margin-top:3px;'><strong>{esc(title)}</strong>{''.join(lines)}</div>"

def render_company(name: str, summary: Dict[str, Any], tracker_lookup: Dict[str, Dict[str, Any]]) -> str:
    html = [f"<div style='{box()} {BASE_FONT}'>"]
    html.append(f"<div style='font-size:13px;font-weight:600;'>{esc(name)}</div>")

    if summary.get("features"):
        html.append("<div style='margin:4px 0;'>")
        html.extend(badge(f) for f in summary["features"])
        html.append("</div>")

    if summary.get("phone_inventory"):
        html.append("<div style='margin-top:4px;'><strong>Phone Inventory</strong>")
        html.append(render_inventory(summary["phone_inventory"]))
        html.append("</div>")

    grouped = defaultdict(list)
    for tracker in summary.get("trackers", []):
        grouped[tracker["category"]].append(tracker)

    for category, trackers in grouped.items():
        html.append(f"<div style='margin-top:6px;'><strong>{esc(category.title())} Trackers</strong>")
        for t in trackers:
            html.append(render_tracker(t, tracker_lookup.get(t.get("id"))))
        html.append("</div>")

    integrations = summary.get("integrations", {})
    if integrations:
        html.append("<div style='margin-top:6px;'><strong>Integrations</strong>")
        html.append(render_integration_block("Salesforce", integrations.get("salesforce") or {}))
        html.append(render_integration_block("Google Ads", integrations.get("google_ads") or {}))
        html.append(render_integration_block("GA4", integrations.get("ga4") or {}))
        html.append(render_integration_block("Webhooks", integrations.get("webhooks") or {}))
        for other in integrations.get("other_integrations", []):
            html.append(render_integration_block(other["name"], other))
        html.append("</div>")

    for warning in summary.get("warnings", []):
        html.append(f"<div style='{box('#fef2f2', '#fecaca', 6, 4)}font-size:11px;'>⚠ {esc(warning)}</div>")

    html.append("</div>")
    return "".join(html)

def render_agencies(agencies: List[Dict[str, Any]]) -> str:
    if not agencies:
        return ""
    rows = []
    for ag in agencies:
        row = (
            f"<div style='margin-bottom:2px;'>"
            f"<strong>{esc(ag.get('name', 'Agency'))}</strong> "
            f"<span style='color:#4b5563;font-size:11px;'>"
            f"Agency ID: {esc(ag.get('id', ''))} · Numeric: {esc(str(ag.get('numeric_id', '—')))}"
            "</span></div>"
        )
        rows.append(row)
    return f"<div style='{box('#eef2ff', '#c7d2fe', 10, 10)} {BASE_FONT}'>" \
           "<div style='font-weight:600;margin-bottom:4px;'>Agencies</div>" \
           f"{''.join(rows)}</div>"

def render_account_block(account_meta: Dict[str, Any], account_detail: Dict[str, Any], company_html: List[str]) -> str:
    name = account_detail.get("name") or account_meta.get("name") or "Account"
    account_id = account_meta.get("id") or account_detail.get("id") or "—"
    numeric_id = account_meta.get("numeric_id") or account_detail.get("numeric_id") or account_detail.get("id") or "—"
    badges_html = []
    if account_meta.get("hipaa_account"):
        badges_html.append(badge("HIPAA", "#fee2e2", "#b91c1c"))
    if account_meta.get("agency_in_trial"):
        badges_html.append(badge("Trial", "#fef3c7", "#92400e"))
    if account_meta.get("inbound_recording_enabled") is False:
        badges_html.append(badge("Inbound Recording Off", "#fee2e2", "#991b1b"))
    if account_meta.get("outbound_recording_enabled") is False:
        badges_html.append(badge("Outbound Recording Off", "#fee2e2", "#991b1b"))

    header = (
        f"<div style='font-size:14px;font-weight:600;margin-bottom:2px;'>"
        f"{esc(name)} "
        f"<span style='color:#4b5563;font-size:11px;'>"
        f"Account ID: {esc(account_id)} · Numeric: {esc(str(numeric_id))}"
        "</span></div>"
    )

    if company_html:
        companies_block = "".join(company_html)
    else:
        companies_block = "<div style='font-size:11px;color:#6b7280;'>No companies found in this account.</div>"

    return (
        f"<div style='{box('#f9fafb', '#d1d5db', 12, 12)} {BASE_FONT}'>"
        f"{header}"
        f"{''.join(badges_html)}"
        f"{companies_block}"
        "</div>"
    )

# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------
def backfill_tracker_numbers(summary_trackers: List[Dict[str, Any]], tracker_lookup: Dict[str, Dict[str, Any]]):
    for tracker in summary_trackers:
        detail = tracker_lookup.get(tracker.get("id"))
        if not detail:
            continue
        tracker.setdefault("tracking_numbers", [])
        tracker["tracking_numbers"] = [
            num if isinstance(num, str) else (num.get("number") or num.get("phone_number") or "")
            for num in detail.get("tracking_numbers", [])
            if num
        ]
        if not tracker.get("destinations"):
            tracker["destinations"] = [detail.get("destination_number")] if detail.get("destination_number") else []
        if tracker.get("number_count") is None:
            tracker["number_count"] = len(detail.get("tracking_numbers", []))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key", default=os.getenv("CALLRAIL_API_KEY"))
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    logger = logging.getLogger("migration")

    if not args.api_key:
        raise SystemExit("CALLRAIL_API_KEY is required.")
    if not args.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    account_listing = list_accessible_accounts(args.api_key, logger)
    accounts_meta = account_listing.get("accounts", [])
    agencies_meta = account_listing.get("agencies", [])

    if not accounts_meta:
        logger.error("No accounts available for this API token.")
        return

    overall_sections = []
    agency_block = render_agencies(agencies_meta)

    for account_meta in accounts_meta:
        numeric_id = account_meta.get("numeric_id")
        if not numeric_id:
            logger.warning("Skipping account %s (no numeric_id).", account_meta.get("name"))
            continue

        client = CallRailClient(str(numeric_id), args.api_key, logger)

        try:
            account_detail = client.get(".json")
        except Exception as exc:
            logger.error("Unable to load account %s: %s", account_meta.get("name"), exc)
            continue

        companies = client.paginate("/companies.json", "companies", {"per_page": 250})
        if not companies:
            overall_sections.append(render_account_block(account_meta, account_detail, []))
            continue

        company_blocks = []

        for company in companies:
            trackers = client.paginate("/trackers.json", "trackers", {"company_id": company["id"], "per_page": 250})
            detailed_trackers = fetch_tracker_details(client, trackers, logger)
            tracker_lookup = {t["id"]: t for t in detailed_trackers}
            integrations = client.paginate("/integrations.json", "integrations", {"company_id": company["id"], "per_page": 250})

            phone_inventory = summarize_numbers_from_trackers(detailed_trackers)

            summary_payload = {
                "company": company,
                "trackers": detailed_trackers,
                "integrations": integrations,
                "phone_inventory": phone_inventory,
            }

            try:
                summary = llm_summary(args.openai_api_key, summary_payload)
            except Exception as exc:
                logger.error("LLM summarization failed for %s / %s: %s", account_meta.get("name"), company["name"], exc)
                continue

            summary.setdefault("phone_inventory", phone_inventory)
            summary.setdefault("features", [])
            summary.setdefault("warnings", [])
            summary.setdefault("trackers", [])
            summary.setdefault("integrations", {})

            backfill_tracker_numbers(summary["trackers"], tracker_lookup)

            company_blocks.append(render_company(company["name"], summary, tracker_lookup))

        overall_sections.append(render_account_block(account_meta, account_detail, company_blocks))

    body = (
        f"<div style='{BASE_FONT}'>"
        f"{agency_block}"
        f"{''.join(overall_sections)}"
        "<div style='margin-top:8px;font-size:10px;color:#6b7280;text-align:center;'>"
        "Internal migration document — CallRail → CallTrackingMetrics"
        "</div></div>"
    )

    html = f"<!doctype html><html><body style='background:#f5f7fb;padding:10px;'>{body}</body></html>"

    subject = f"CTM Migration Packet {len(overall_sections)} CallRail Account(s) –"

    requests.post(
        DEFAULT_WEBHOOK_URL,
        json={
            "subject": subject,
            "html": html,
            "generated_at": utc_now(),
        },
    ).raise_for_status()

    logger.info("Migration packet generated for %d account(s).", len(overall_sections))

if __name__ == "__main__":
    main()