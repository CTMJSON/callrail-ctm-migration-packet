#!/usr/bin/env python3
"""
CallRail → CTM Migration Comparison
Pulls deep CallRail account data and renders a collapsible side-by-side
HTML report showing current config and its CTM equivalent.

No OpenAI key required — all mapping is deterministic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests import HTTPError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Constants ────────────────────────────────────────────────────────────────
CALLRAIL_API_ROOT = "https://api.callrail.com/v3"

TOLL_FREE_PREFIXES = {"800", "822", "833", "844", "855", "866", "877", "888"}

# Fields to skip when rendering raw integration detail
_INTEG_SKIP = {"id", "account_id", "company_id", "created_at", "updated_at",
               "name", "type", "status", "uid"}

CTM_INTEGRATION_MAP: Dict[str, Tuple[str, str, str]] = {
    "google_ads":        ("Google Ads",            "Full parity",         "green"),
    "adwords":           ("Google Ads",            "Full parity",         "green"),
    "google_analytics":  ("GA4",                   "Full parity",         "green"),
    "ga4":               ("GA4",                   "Full parity",         "green"),
    "salesforce":        ("Salesforce",             "Full parity",         "green"),
    "hubspot":           ("HubSpot",                "Supported",           "green"),
    "webhook":           ("Webhooks",               "Supported",           "green"),
    "webhooks":          ("Webhooks",               "Supported",           "green"),
    "zapier":            ("Zapier (via webhook)",   "Via webhook bridge",  "yellow"),
    "slack":             ("Slack (via webhook)",    "Via webhook bridge",  "yellow"),
    "bing_ads":          ("Bing Ads",               "Supported",           "green"),
    "facebook":          ("Facebook",               "Verify availability", "yellow"),
    "activecampaign":    ("ActiveCampaign",         "Verify availability", "yellow"),
}

# ─── HTTP Session ─────────────────────────────────────────────────────────────
def _make_session(retries: int = 4, backoff: float = 0.5) -> requests.Session:
    retry = Retry(
        total=retries, backoff_factor=backoff,
        status_forcelist={429, 500, 502, 503, 504},
        allowed_methods={"GET", "POST"}, raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_SESSION = _make_session()

# ─── CallRail Client ──────────────────────────────────────────────────────────
class CallRailClient:
    def __init__(self, account_id: str, api_key: str, logger: logging.Logger):
        self.base = f"{CALLRAIL_API_ROOT}/a/{account_id}"
        self.headers = {"Authorization": f'Token token="{api_key}"'}
        self.logger = logger

    def get(self, path: str, params: Optional[Dict] = None) -> Dict:
        r = _SESSION.get(f"{self.base}{path}", headers=self.headers, params=params, timeout=30)
        try:
            r.raise_for_status()
        except HTTPError:
            self.logger.error("CallRail %s %s: %s", r.status_code, path, r.text[:200])
            raise
        return r.json()

    def try_get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """GET that returns None on 4xx instead of raising."""
        try:
            return self.get(path, params)
        except HTTPError:
            return None

    def paginate(self, path: str, key: str, params: Dict) -> List[Dict]:
        out, page = [], 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            out.extend(data.get(key, []))
            if page >= data.get("total_pages", 1):
                break
            page += 1
        return out

    def try_paginate(self, path: str, key: str, params: Dict) -> List[Dict]:
        """paginate() that swallows 4xx errors (endpoint may not exist)."""
        try:
            return self.paginate(path, key, params)
        except HTTPError:
            return []


def list_accessible_accounts(api_key: str, logger: logging.Logger) -> Dict:
    headers = {"Authorization": f'Token token="{api_key}"'}
    params: Dict[str, Any] = {"per_page": 100, "page": 1}
    accounts: List[Dict] = []
    agencies: List[Dict] = []
    while True:
        r = _SESSION.get(f"{CALLRAIL_API_ROOT}/a", headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        accounts.extend(data.get("accounts", []))
        agencies.extend(data.get("agencies", []))
        if params["page"] >= data.get("total_pages", 1):
            break
        params["page"] += 1
    return {"accounts": accounts, "agencies": agencies}


def fetch_tracker_details(
    client: CallRailClient, trackers: List[Dict],
    logger: logging.Logger, max_workers: int = 10,
) -> List[Dict]:
    if not trackers:
        return []
    results: Dict[str, Dict] = {}

    def _fetch(t: Dict) -> Tuple[str, Dict]:
        try:
            return t["id"], client.get(f"/trackers/{t['id']}.json")
        except Exception:
            logger.warning("Could not fetch tracker %s", t["id"])
            return t["id"], t

    with ThreadPoolExecutor(max_workers=min(max_workers, len(trackers))) as pool:
        futures = {pool.submit(_fetch, t): t for t in trackers}
        for future in as_completed(futures):
            tid, detail = future.result()
            results[tid] = detail

    return [results[t["id"]] for t in trackers]


def normalize_number(v: str) -> str:
    digits = re.sub(r"\D", "", v or "")
    return digits[1:] if digits.startswith("1") and len(digits) == 11 else digits


def detect_toll_free(number: str, meta: Dict) -> bool:
    if meta.get("toll_free") is not None:
        return bool(meta["toll_free"])
    return normalize_number(number)[:3] in TOLL_FREE_PREFIXES


# ─── CTM Concept Mapping ──────────────────────────────────────────────────────
def _extract_tracking_numbers(tracker: Dict) -> List[str]:
    out = []
    for raw in tracker.get("tracking_numbers", []):
        if isinstance(raw, str):
            out.append(raw)
        elif isinstance(raw, dict):
            n = raw.get("number") or raw.get("phone_number") or raw.get("tracking_number") or ""
            if n:
                out.append(n)
    return out


def _parse_steps(steps: List[Dict]) -> List[Dict]:
    """Normalize call flow steps into a consistent shape."""
    out = []
    for s in steps:
        action = (s.get("action") or s.get("type") or "step").lower()
        target = (
            s.get("destination_number") or s.get("agent_name") or
            s.get("agent") or s.get("value") or s.get("phone_number") or ""
        )
        ring_time = s.get("ring_time") or s.get("seconds") or s.get("ring_timeout") or ""
        timeout = s.get("timeout") or s.get("no_answer_timeout") or ""
        out.append({
            "action": action,
            "target": str(target) if target else "",
            "ring_time": str(ring_time) if ring_time else "",
            "timeout": str(timeout) if timeout else "",
        })
    return out


def map_tracker_to_ctm(tracker: Dict) -> Dict:
    category = (tracker.get("category") or "").lower()
    tracker_type = (tracker.get("type") or "").lower()
    is_session = category == "session" or "pool" in tracker_type

    tracking_numbers = _extract_tracking_numbers(tracker)

    destinations = list(tracker.get("destinations") or [])
    if not destinations and tracker.get("destination_number"):
        destinations = [tracker["destination_number"]]

    call_flow = tracker.get("call_flow") or {}

    # Recording
    recording = call_flow.get("recording_enabled")
    if recording is None:
        recording = tracker.get("recording_enabled")

    # Whisper
    whisper = call_flow.get("whisper_message") or tracker.get("whisper_message") or ""

    # Greeting
    greeting_type = (call_flow.get("greeting_type") or "").lower()  # simple | audio | tts | none
    greeting_text = call_flow.get("greeting_text") or ""
    greeting_audio = bool(call_flow.get("greeting_audio_url") or call_flow.get("greeting_audio"))

    # Voicemail
    voicemail_config = call_flow.get("voicemail") or {}
    voicemail_enabled = call_flow.get("voicemail_enabled") or bool(voicemail_config)
    voicemail_transcript = voicemail_config.get("transcription_enabled") or False

    # Steps
    raw_steps = call_flow.get("steps") or []
    steps = _parse_steps(raw_steps)

    # Schedule (business hours)
    schedule = call_flow.get("schedule") or tracker.get("schedule") or {}
    has_schedule = bool(schedule and schedule != {})
    schedule_name = schedule.get("name") or schedule.get("id") or ""

    # SMS
    sms_enabled = bool(tracker.get("sms_enabled"))
    sms_settings = tracker.get("sms_settings") or {}
    sms_greeting = sms_settings.get("greeting_text") or sms_settings.get("message") or ""
    sms_auto_reply = sms_settings.get("auto_reply_text") or sms_settings.get("auto_reply") or ""

    # Keywords / keyword spotting
    raw_keywords = tracker.get("keywords") or tracker.get("keyword_spotting") or []
    if isinstance(raw_keywords, list):
        keywords = [k.get("keyword") if isinstance(k, dict) else str(k) for k in raw_keywords[:12]]
    else:
        keywords = []

    # Auto-tags
    auto_tags = call_flow.get("tags") or tracker.get("auto_tags") or []
    tag_names = [t.get("name") if isinstance(t, dict) else str(t) for t in auto_tags[:8]]

    # Swap targets
    swap_targets = tracker.get("swap_targets") or []

    # Source metadata
    source_meta = tracker.get("source") or {}
    source_type = source_meta.get("type") or source_meta.get("search_type") or ""
    medium = source_meta.get("medium") or ""
    search_engine = source_meta.get("search_engine") or ""
    campaign = source_meta.get("campaign") or ""
    referrer_url = source_meta.get("referrer_url") or ""
    landing_url = source_meta.get("landing_page_url") or source_meta.get("landing_url") or ""

    # Call flow type
    flow_type = (call_flow.get("type") or "basic").lower()

    # Pool size
    pool_size: Optional[int] = None
    if is_session:
        for raw in tracker.get("tracking_numbers", []):
            if isinstance(raw, dict) and raw.get("pool_size"):
                pool_size = int(raw["pool_size"])
                break
        if pool_size is None and tracking_numbers:
            pool_size = len(tracking_numbers)

    # Build CTM config notes
    ctm_notes: List[str] = []
    ctm_source_type = "DNI Pool (Session)" if is_session else "Static Source"

    if recording:
        ctm_notes.append("Enable call recording on queue/source")
    if whisper:
        ctm_notes.append(f'Queue whisper: "{whisper}"')
    if greeting_text:
        gtlabel = f" [{greeting_type}]" if greeting_type and greeting_type != "simple" else ""
        ctm_notes.append(f'Queue greeting{gtlabel}: "{greeting_text}"')
    elif greeting_audio:
        ctm_notes.append("Queue greeting: audio file (re-upload to CTM)")
    if voicemail_enabled:
        vm_extra = " + transcription" if voicemail_transcript else ""
        ctm_notes.append(f"Enable voicemail on queue{vm_extra}")
    if steps:
        ctm_notes.append(f"{len(steps)} routing step(s) → map to CTM Queue routing")
        if flow_type == "advanced":
            ctm_notes.append("Advanced call flow → may need CTM Voice Menu")
    if has_schedule:
        sname = f' "{schedule_name}"' if schedule_name else ""
        ctm_notes.append(f"Business hours schedule{sname} → recreate as CTM Schedule")
    if swap_targets:
        ctm_notes.append(f"{len(swap_targets)} swap target(s) → add as CTM Target Numbers")
    if sms_enabled:
        ctm_notes.append("Enable SMS on CTM tracking number(s)")
    if sms_greeting:
        ctm_notes.append(f'SMS greeting: "{sms_greeting}"')
    if keywords:
        ctm_notes.append(f"Keywords: {', '.join(keywords)} → configure in CTM keyword spotting")
    if tag_names:
        ctm_notes.append(f"Auto-tags: {', '.join(tag_names)} → recreate in CTM")
    if source_type:
        label = source_type.replace("_", " ").title()
        if search_engine:
            label += f" / {search_engine}"
        if medium:
            label += f" ({medium})"
        ctm_notes.append(f"Source channel: {label}")
    if campaign:
        ctm_notes.append(f"Campaign: {campaign}")
    if is_session and pool_size:
        ctm_notes.append(f"DNI pool size: {pool_size} number(s)")
    if referrer_url:
        ctm_notes.append(f"Referrer filter: {referrer_url}")

    queue_needed = bool(destinations or steps or whisper or greeting_text or voicemail_enabled or recording)

    return {
        "id": tracker.get("id"),
        "name": tracker.get("name") or "Unnamed",
        "callrail_category": category,
        "callrail_subtype": tracker_type,
        "flow_type": flow_type,
        "status": tracker.get("status") or "active",
        "tracking_numbers": tracking_numbers,
        "destinations": destinations,
        "recording": recording,
        "whisper": whisper,
        "greeting_type": greeting_type,
        "greeting_text": greeting_text,
        "greeting_audio": greeting_audio,
        "voicemail_enabled": voicemail_enabled,
        "voicemail_transcript": voicemail_transcript,
        "steps": steps,
        "has_schedule": has_schedule,
        "schedule_name": schedule_name,
        "sms_enabled": sms_enabled,
        "sms_greeting": sms_greeting,
        "sms_auto_reply": sms_auto_reply,
        "keywords": keywords,
        "tag_names": tag_names,
        "source_type": source_type,
        "medium": medium,
        "search_engine": search_engine,
        "campaign": campaign,
        "referrer_url": referrer_url,
        "landing_url": landing_url,
        "swap_targets": swap_targets,
        "is_session": is_session,
        "ctm_source_type": ctm_source_type,
        "ctm_pool_size": pool_size,
        "ctm_queue_needed": queue_needed,
        "ctm_notes": ctm_notes,
        "created_at": tracker.get("created_at"),
        "disabled_at": tracker.get("disabled_at"),
    }


def map_integrations(integrations: List[Dict]) -> List[Dict]:
    mapped = []
    for integ in integrations:
        raw_name = integ.get("name") or integ.get("type") or "unknown"
        key = raw_name.lower().replace(" ", "_")
        status = (integ.get("status") or "active").lower()
        is_active = status in ("active", "enabled", "connected")

        ctm_label, ctm_compat, ctm_color = CTM_INTEGRATION_MAP.get(
            key, (raw_name.title(), "Verify availability in CTM", "yellow")
        )

        # Extract meaningful sub-fields from the raw payload
        detail_fields: List[Tuple[str, str]] = []
        for k, v in integ.items():
            if k in _INTEG_SKIP or v is None or v == "" or v == [] or v == {}:
                continue
            label = k.replace("_", " ").title()
            if isinstance(v, list):
                val = ", ".join(str(x) for x in v[:6]) + ("…" if len(v) > 6 else "")
            elif isinstance(v, dict):
                val = ", ".join(f"{dk}: {dv}" for dk, dv in list(v.items())[:4])
            else:
                val = str(v)
            detail_fields.append((label, val))

        mapped.append({
            "cr_name": raw_name.title(),
            "cr_status": status,
            "cr_active": is_active,
            "ctm_name": ctm_label,
            "ctm_compat": ctm_compat,
            "ctm_color": ctm_color,
            "detail_fields": detail_fields,
        })
    return mapped


def summarize_inventory(trackers: List[Dict]) -> Dict:
    seen: Dict[str, Dict] = {}
    for t in trackers:
        for raw in t.get("tracking_numbers", []):
            entry = {"number": raw} if isinstance(raw, str) else (raw or {})
            num = entry.get("number") or entry.get("phone_number") or entry.get("tracking_number") or ""
            if not num:
                continue
            d = seen.setdefault(num.strip(), {"pool": False, "toll_free": False})
            ntype = (entry.get("number_type") or entry.get("type") or "").lower()
            if ntype == "pool" or entry.get("pool") or entry.get("pool_size"):
                d["pool"] = True
            d["toll_free"] = d["toll_free"] or detect_toll_free(num.strip(), entry)

    pool = sum(1 for v in seen.values() if v["pool"])
    tf = sum(1 for v in seen.values() if v["toll_free"])
    return {
        "total": len(seen),
        "pool": pool,
        "static": len(seen) - pool,
        "toll_free": tf,
        "local": len(seen) - tf,
    }


def compute_migration_score(
    mapped_trackers: List[Dict],
    mapped_integrations: List[Dict],
    users: List[Dict],
    tags: List[Dict],
) -> Dict:
    score = 100
    warnings: List[str] = []
    checklist: List[Tuple[str, str]] = []  # (text, priority)

    static_count = sum(1 for t in mapped_trackers if not t["is_session"])
    session_count = sum(1 for t in mapped_trackers if t["is_session"])
    disabled_count = sum(1 for t in mapped_trackers if t["status"] != "active")
    complex_count = sum(1 for t in mapped_trackers if t["flow_type"] == "advanced" or len(t["steps"]) > 2)
    queue_count = sum(1 for t in mapped_trackers if t["ctm_queue_needed"])
    has_recording = any(t["recording"] for t in mapped_trackers)
    has_whisper = any(t["whisper"] for t in mapped_trackers)
    has_sms = any(t["sms_enabled"] for t in mapped_trackers)
    has_voicemail = any(t["voicemail_enabled"] for t in mapped_trackers)
    has_schedule = any(t["has_schedule"] for t in mapped_trackers)
    has_keywords = any(t["keywords"] for t in mapped_trackers)
    yellow_integ = [i for i in mapped_integrations if i["ctm_color"] == "yellow"]

    if disabled_count:
        score -= min(10, 5 * disabled_count)
        warnings.append(f"{disabled_count} disabled tracker(s) — confirm whether to migrate or decommission")
    if complex_count:
        score -= min(15, 5 * complex_count)
        warnings.append(f"{complex_count} tracker(s) with advanced/multi-step flows — requires manual CTM Queue + Voice Menu setup")
    if has_schedule:
        score -= 5
        warnings.append("Business hour schedules detected — recreate as CTM Schedules and attach to queues")
    for i in yellow_integ:
        score -= 5
        warnings.append(f"'{i['cr_name']}' → {i['ctm_compat']} — plan workaround before cutover")

    score = max(0, min(100, score))

    checklist.append(("Port all tracking numbers to CTM", "required"))
    if static_count:
        checklist.append((f"Create {static_count} Static Source(s) in CTM", "required"))
    if session_count:
        checklist.append((f"Create {session_count} DNI Pool Source(s) + update website JS snippet", "required"))
    if queue_count:
        checklist.append((f"Build {queue_count} Queue(s) for call routing/forwarding", "required"))
    if has_recording:
        checklist.append(("Enable call recording on relevant queues/sources", "required"))
    if has_whisper:
        checklist.append(("Configure whisper messages on CTM queues", "recommended"))
    if has_voicemail:
        checklist.append(("Set up voicemail on CTM queues", "recommended"))
    if has_sms:
        checklist.append(("Enable SMS on CTM tracking numbers", "recommended"))
    if has_schedule:
        checklist.append(("Recreate business hour schedules in CTM", "required"))
    if has_keywords:
        checklist.append(("Configure keyword spotting in CTM", "recommended"))
    if tags:
        checklist.append((f"Recreate {len(tags)} call tag(s) in CTM", "recommended"))
    if users:
        checklist.append((f"Invite {len(users)} user(s) to CTM account", "required"))
    for i in mapped_integrations:
        if i["cr_active"]:
            checklist.append((f"Reconnect {i['ctm_name']} integration in CTM", "required"))
    checklist.append(("QA test all tracking numbers post-migration", "required"))
    checklist.append(("Set CallRail cancellation date with your rep", "required"))

    return {
        "score": score, "warnings": warnings, "checklist": checklist,
        "static_count": static_count, "session_count": session_count,
        "disabled_count": disabled_count, "complex_count": complex_count,
        "queue_count": queue_count,
    }


# ─── HTML Helpers ─────────────────────────────────────────────────────────────
def esc(x: Any) -> str:
    return "" if x is None else html_lib.escape(str(x))


def _badge(text: str, cls: str = "") -> str:
    return f"<span class='badge {esc(cls)}'>{esc(text)}</span>"


def _chip(text: str) -> str:
    return f"<span class='chip'>{esc(text)}</span>"


def _item(label: str, value: Any, mono: bool = False) -> str:
    if value is None or value == "" or value is False:
        return ""
    vcls = " mono" if mono else ""
    return (f"<div class='item'><span class='key'>{esc(label)}:</span>"
            f" <span class='val{vcls}'>{esc(str(value))}</span></div>")


def _stat(label: str, value: Any) -> str:
    return (f"<div class='inv-item'><div class='inv-num'>{esc(str(value))}</div>"
            f"<div class='inv-lbl'>{esc(label)}</div></div>")


def _ctm_note(text: str) -> str:
    return f"<div class='ctm-note-item'>→ {esc(text)}</div>"


# ─── Section Renderers ────────────────────────────────────────────────────────
def _two_col(cr_html: str, ctm_html: str) -> str:
    return (f"<div class='two-col'>"
            f"<div class='col-cr'>{cr_html}</div>"
            f"<div class='col-arr'>→</div>"
            f"<div class='col-ctm'>{ctm_html}</div>"
            f"</div>")


def _section_hdr(title: str) -> str:
    return f"<div class='section-hdr'>{esc(title)}</div>"


def render_inventory_section(inv: Dict) -> str:
    stats = (f"<div class='inv-grid'>"
             f"{_stat('Total', inv['total'])}"
             f"{_stat('Pool (DNI)', inv['pool'])}"
             f"{_stat('Static', inv['static'])}"
             f"{_stat('Local', inv['local'])}"
             f"{_stat('Toll-Free', inv['toll_free'])}"
             f"</div>")

    cr = f"<div class='plat-label cr-label'>Phone Inventory — CallRail</div>{stats}"
    ctm_note = "<div class='ctm-note' style='margin-top:8px;'>Numbers port 1:1 — same DIDs, reassigned to CTM sources</div>"
    ctm_stats = (f"<div class='inv-grid'>"
                 f"{_stat('Total', inv['total'])}"
                 f"{_stat('DNI Pool', inv['pool'])}"
                 f"{_stat('Static', inv['static'])}"
                 f"{_stat('Local', inv['local'])}"
                 f"{_stat('Toll-Free', inv['toll_free'])}"
                 f"</div>")
    ctm = f"<div class='plat-label ctm-label'>Phone Inventory — CTM</div>{ctm_stats}{ctm_note}"
    return _two_col(cr, ctm)


def _render_steps(steps: List[Dict]) -> str:
    if not steps:
        return ""
    parts = []
    for s in steps:
        action = s["action"]
        label = action
        if s["target"]:
            label += f" → {s['target']}"
        if s["ring_time"]:
            label += f" ({s['ring_time']}s)"
        parts.append(f"<span class='step-chip'>{esc(label)}</span>")
    return f"<div class='step-flow'>{'<span class=\"step-arr\">›</span>'.join(parts)}</div>"


def render_tracker_row(t: Dict) -> str:
    """Returns a collapsible <details> block for one tracker."""
    status_cls = "badge-active" if t["status"] == "active" else "badge-disabled"
    cat_cls = "badge-session" if t["is_session"] else "badge-source"

    nums_html = "".join(_chip(n) for n in t["tracking_numbers"]) or "<span class='muted'>none</span>"
    dests_html = ", ".join(t["destinations"]) if t["destinations"] else "—"

    # Summary line flags
    flags = []
    if t["recording"]:         flags.append("● Rec")
    if t["sms_enabled"]:       flags.append("● SMS")
    if t["voicemail_enabled"]: flags.append("● VM")
    if t["has_schedule"]:      flags.append("● Schedule")
    if t["keywords"]:          flags.append("● Keywords")
    if t["steps"]:             flags.append(f"● {len(t['steps'])} steps")
    flags_html = "".join(f"<span class='flag'>{esc(f)}</span>" for f in flags)

    summary_html = (
        f"<summary class='tracker-summary'>"
        f"<span class='ts-name'>{esc(t['name'])}</span>"
        f"{_badge(t['callrail_category'], cat_cls)}"
        f"{_badge(t['status'], status_cls)}"
        f"<span class='ts-meta'>{esc(str(len(t['tracking_numbers'])) + ' number(s)')} "
        f"{'· → ' + esc(dests_html) if t['destinations'] else ''}</span>"
        f"{flags_html}"
        f"<span class='ts-arrow'>▶</span>"
        f"</summary>"
    )

    # ── CallRail detail ──
    greeting_val = ""
    if t["greeting_text"]:
        gt = f" [{t['greeting_type']}]" if t["greeting_type"] else ""
        greeting_val = f'"{t["greeting_text"]}"{gt}'
    elif t["greeting_audio"]:
        greeting_val = "Audio file"

    cr = (
        f"<div class='plat-label cr-label'>CallRail</div>"
        f"<div class='num-row'>{nums_html}</div>"
        + _item("Destination(s)", dests_html if t["destinations"] else None)
        + _item("Type", t["callrail_subtype"] if t["callrail_subtype"] and t["callrail_subtype"] != t["callrail_category"] else None)
        + _item("Flow type", t["flow_type"] if t["flow_type"] != "basic" else None)
        + _item("Recording", "ON" if t["recording"] else ("off" if t["recording"] is False else None))
        + _item("Whisper", t["whisper"])
        + _item("Greeting", greeting_val)
        + _item("Voicemail", ("ON + transcription" if t["voicemail_transcript"] else "ON") if t["voicemail_enabled"] else None)
        + _item("Business hours", t["schedule_name"] or "Configured" if t["has_schedule"] else None)
        + _item("SMS", "enabled" if t["sms_enabled"] else None)
        + _item("SMS greeting", t["sms_greeting"])
        + _item("SMS auto-reply", t["sms_auto_reply"])
        + _item("Source type", t["source_type"])
        + _item("Medium", t["medium"])
        + _item("Search engine", t["search_engine"])
        + _item("Campaign", t["campaign"])
        + _item("Referrer", t["referrer_url"], mono=True)
        + _item("Swap targets", len(t["swap_targets"]) if t["swap_targets"] else None)
        + _item("Auto-tags", ", ".join(t["tag_names"]) if t["tag_names"] else None)
        + _item("Keywords", ", ".join(t["keywords"]) if t["keywords"] else None)
        + (_render_steps(t["steps"]) if t["steps"] else "")
        + _item("Created", t["created_at"])
        + _item("Disabled", t["disabled_at"])
    )

    # ── CTM detail ──
    ctm_obj = "Source + DNI Pool" if t["is_session"] else "Source"
    if t["ctm_queue_needed"]:
        ctm_obj += " + Queue"

    pool_line = _item("Pool size", f"{t['ctm_pool_size']} numbers") if t["is_session"] and t["ctm_pool_size"] else ""

    ctm = (
        f"<div class='plat-label ctm-label'>CTM</div>"
        + _item("CTM objects", ctm_obj)
        + f"<div class='num-row' style='margin:6px 0;'>{nums_html}</div>"
        + _item("Target Number(s)", dests_html if t["destinations"] else None)
        + pool_line
        + (f"<div class='ctm-notes'>{''.join(_ctm_note(n) for n in t['ctm_notes'])}</div>" if t["ctm_notes"] else "")
    )

    inner = _two_col(cr, ctm)
    return f"<details class='tracker-details'>{summary_html}{inner}</details>"


def render_integrations_section(mapped: List[Dict]) -> str:
    if not mapped:
        return _two_col(
            "<div class='muted'>No integrations configured</div>",
            "<div class='muted'>Nothing to migrate</div>",
        )

    cr_body = "<div class='plat-label cr-label'>Integrations — CallRail</div>"
    ctm_body = "<div class='plat-label ctm-label'>Integrations — CTM</div>"

    for i in mapped:
        cr_status_cls = "badge-active" if i["cr_active"] else "badge-disabled"
        ctm_cls = f"compat-{i['ctm_color']}"

        detail_html = ""
        for label, val in i["detail_fields"][:8]:
            detail_html += f"<div class='integ-detail'><span class='key'>{esc(label)}:</span> {esc(val)}</div>"

        cr_body += (
            f"<div class='integ-block'>"
            f"<div class='integ-name'>{esc(i['cr_name'])} {_badge(i['cr_status'], cr_status_cls)}</div>"
            f"{detail_html}"
            f"</div>"
        )
        ctm_body += (
            f"<div class='integ-block'>"
            f"<div class='integ-name'>{esc(i['ctm_name'])} "
            f"<span class='{esc(ctm_cls)}'>{esc(i['ctm_compat'])}</span></div>"
            f"</div>"
        )

    return _two_col(cr_body, ctm_body)


def render_users_section(users: List[Dict]) -> str:
    if not users:
        return ""
    cr_rows = ""
    ctm_rows = ""
    for u in users:
        name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("name") or "—"
        email = u.get("email") or "—"
        role = u.get("role") or "user"
        cr_rows += (f"<tr><td>{esc(name)}</td><td class='mono small'>{esc(email)}</td>"
                    f"<td>{_badge(role, 'badge-role')}</td></tr>")
        ctm_rows += (f"<tr><td>{esc(name)}</td><td class='mono small'>{esc(email)}</td>"
                     f"<td class='muted small'>Invite as Agent</td></tr>")

    tbl = "<table class='data-table'><tr><th>Name</th><th>Email</th><th>Role</th></tr>{}</table>"
    cr = f"<div class='plat-label cr-label'>Users — CallRail ({len(users)})</div>" + tbl.format(cr_rows)
    ctm = f"<div class='plat-label ctm-label'>Agents — CTM (to invite)</div>" + tbl.format(ctm_rows)
    return _two_col(cr, ctm)


def render_tags_section(tags: List[Dict]) -> str:
    if not tags:
        return ""
    cr_rows = ""
    ctm_rows = ""
    for tg in tags:
        name = tg.get("name") or "—"
        color = tg.get("color") or ""
        color_dot = f"<span class='color-dot' style='background:{esc(color)};'></span>" if color else ""
        cr_rows += f"<tr><td>{color_dot}{esc(name)}</td><td class='muted small'>{esc(color)}</td></tr>"
        ctm_rows += f"<tr><td>{color_dot}{esc(name)}</td><td class='muted small'>Create in CTM → Tags</td></tr>"

    tbl = "<table class='data-table'><tr><th>Tag</th><th>Color / Note</th></tr>{}</table>"
    cr = f"<div class='plat-label cr-label'>Call Tags — CallRail ({len(tags)})</div>" + tbl.format(cr_rows)
    ctm = f"<div class='plat-label ctm-label'>Call Tags — CTM (to recreate)</div>" + tbl.format(ctm_rows)
    return _two_col(cr, ctm)


def render_notifications_section(notifications: List[Dict]) -> str:
    if not notifications:
        return ""
    rows = ""
    for n in notifications:
        ntype = n.get("type") or n.get("notification_type") or "—"
        event = n.get("event") or n.get("trigger") or n.get("call_type") or "—"
        recipient = n.get("recipient") or n.get("email") or n.get("to") or "—"
        rows += (f"<tr><td>{esc(ntype)}</td><td>{esc(event)}</td>"
                 f"<td class='mono small'>{esc(recipient)}</td></tr>")

    tbl = (f"<table class='data-table'>"
           f"<tr><th>Type</th><th>Event</th><th>Recipient</th></tr>{rows}</table>")
    cr = f"<div class='plat-label cr-label'>Notifications — CallRail ({len(notifications)})</div>{tbl}"
    notif_notes = "".join(
        _ctm_note(f"Recreate {n.get('type', 'notification')} → CTM Notification rules")
        for n in notifications[:5]
    )
    ctm = (f"<div class='plat-label ctm-label'>Notifications — CTM (to recreate)</div>"
           f"<div class='ctm-notes'>{notif_notes}</div>")
    return _two_col(cr, ctm)


def render_checklist_warnings(score_data: Dict) -> str:
    score = score_data["score"]
    score_cls = "score-high" if score >= 85 else ("score-mid" if score >= 65 else "score-low")
    score_label = "Migration Ready" if score >= 85 else ("Manual Steps Needed" if score >= 65 else "Complex Migration")

    warn_html = "".join(f"<div class='warning'>{esc(w)}</div>" for w in score_data["warnings"])
    if not warn_html:
        warn_html = "<div class='no-warn'>No migration blockers detected</div>"

    check_html = ""
    for text, priority in score_data["checklist"]:
        prio_cls = "check-req" if priority == "required" else "check-rec"
        check_html += (
            f"<li><div class='check-icon {esc(prio_cls)}'>✓</div>"
            f"<div class='check-body'>{esc(text)}"
            f"<span class='check-prio'>— {priority}</span></div></li>"
        )

    return (
        f"<div class='checklist-section'>"
        f"  <div class='score-col'>"
        f"    <div class='plat-label' style='color:var(--lime);'>Migration Readiness</div>"
        f"    <div class='score-card'>"
        f"      <div class='score-num {esc(score_cls)}'>{score}</div>"
        f"      <div class='score-denom'>/100</div>"
        f"      <div class='score-lbl'>{esc(score_label)}</div>"
        f"    </div>"
        f"    <div class='sub-hdr'>Warnings</div>"
        f"    {warn_html}"
        f"  </div>"
        f"  <div class='checklist-col'>"
        f"    <div class='plat-label' style='color:var(--blue);'>Migration Checklist</div>"
        f"    <ul class='checklist'>{check_html}</ul>"
        f"  </div>"
        f"</div>"
    )


def render_company_block(
    company_name: str,
    mapped_trackers: List[Dict],
    mapped_integrations: List[Dict],
    notifications: List[Dict],
    score_data: Dict,
    inventory: Dict,
) -> str:
    active = [t for t in mapped_trackers if t["status"] == "active"]
    disabled = [t for t in mapped_trackers if t["status"] != "active"]

    tracker_rows = "".join(render_tracker_row(t) for t in active)
    if disabled:
        dis_rows = "".join(render_tracker_row(t) for t in disabled)
        tracker_rows += f"<div class='disabled-hdr'>Disabled Trackers ({len(disabled)})</div><div class='disabled-wrap'>{dis_rows}</div>"

    if not mapped_trackers:
        tracker_rows = "<div class='empty-msg'>No trackers configured for this company</div>"

    notif_section = ""
    if notifications:
        notif_section = _section_hdr("Notifications") + render_notifications_section(notifications)

    slug = re.sub(r"[^a-z0-9]", "-", company_name.lower())
    return (
        f"<div class='company-block' id='{esc(slug)}'>"
        f"  <div class='company-hdr'>"
        f"    <span>🏢 {esc(company_name)}</span>"
        f"    <span class='company-count'>{len(active)} active tracker(s)</span>"
        f"    <div class='ctrl-btns'>"
        f"      <button class='ctrl-btn' onclick='openAll(this)'>▼ Open All</button>"
        f"      <button class='ctrl-btn' onclick='closeAll(this)'>▲ Close All</button>"
        f"    </div>"
        f"  </div>"
        f"  {_section_hdr('📞 Phone Inventory')}"
        f"  {render_inventory_section(inventory)}"
        f"  {_section_hdr('🔀 Trackers → CTM Sources')}"
        f"  <div class='tracker-col-hdr two-col'>"
        f"    <div class='col-cr'><div class='plat-label cr-label'>CallRail — Tracker Detail</div></div>"
        f"    <div class='col-arr'></div>"
        f"    <div class='col-ctm'><div class='plat-label ctm-label'>CTM — Equivalent Setup</div></div>"
        f"  </div>"
        f"  <div class='trackers-outer'>{tracker_rows}</div>"
        f"  {_section_hdr('🔗 Integrations')}"
        f"  {render_integrations_section(mapped_integrations)}"
        f"  {notif_section}"
        f"  {_section_hdr('📋 Migration Readiness & Checklist')}"
        f"  {render_checklist_warnings(score_data)}"
        f"</div>"
    )


# ─── CSS ──────────────────────────────────────────────────────────────────────
CSS = """
:root {
  --cr: #FF6B35; --cr-bg: rgba(255,107,53,.07); --cr-border: rgba(255,107,53,.22);
  --blue: #0EA5E9; --lime: #84CC16;
  --ctm-bg: rgba(14,165,233,.07); --ctm-border: rgba(14,165,233,.22);
  --bg: #0F172A; --card: #1E293B; --card2: #162032;
  --border: #2D3F56; --text: #E2E8F0; --muted: #7A92AD;
  --green: #10B981; --yellow: #F59E0B; --red: #EF4444;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; line-height: 1.5; }

/* Header */
.header { background: linear-gradient(135deg, #0a1628, #0f2540 60%, #091e38); padding: 26px 36px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 20px; font-weight: 700; }
.header h1 .cr  { color: var(--cr); }
.header h1 .arr { color: var(--muted); margin: 0 6px; font-weight: 300; }
.header h1 .ctm { color: var(--blue); }
.header-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }
.header-right { text-align: right; font-size: 13px; font-weight: 600; }
.header-right .sub { font-size: 11px; color: var(--muted); margin-top: 2px; font-weight: 400; }

.container { max-width: 1360px; margin: 0 auto; padding: 24px 32px; }

/* KPI grid */
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap: 10px; margin-bottom: 28px; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.kpi-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .6px; font-weight: 600; }
.kpi-val { font-size: 28px; font-weight: 700; margin-top: 2px; line-height: 1.1; }
.kpi-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }

/* TOC */
.toc { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin-bottom: 24px; }
.toc-title { font-size: 10px; text-transform: uppercase; letter-spacing: .7px; color: var(--muted); font-weight: 700; margin-bottom: 8px; }
.toc-list { list-style: none; display: flex; flex-wrap: wrap; gap: 6px; }
.toc-list a { font-size: 12px; color: var(--blue); text-decoration: none; background: rgba(14,165,233,.08); border: 1px solid rgba(14,165,233,.2); border-radius: 6px; padding: 3px 10px; }
.toc-list a:hover { background: rgba(14,165,233,.16); }

/* Account label */
.acct-label { font-size: 15px; font-weight: 700; margin: 24px 0 14px; display: flex; align-items: center; gap: 10px; }
.acct-label::after { content:''; flex:1; height:1px; background: var(--border); }
.acct-label .sub { font-size: 11px; color: var(--muted); font-weight: 400; }

/* Account-level sections */
.acct-section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 16px; overflow: hidden; }
.acct-section-hdr { padding: 10px 16px; background: var(--card2); border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); font-weight: 700; }

/* Company block */
.company-block { margin-bottom: 28px; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.company-hdr { background: var(--card2); padding: 12px 18px; font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.company-count { margin-left: auto; font-size: 11px; color: var(--muted); font-weight: 400; background: rgba(255,255,255,.05); border: 1px solid var(--border); border-radius: 20px; padding: 2px 9px; }
.ctrl-btns { display: flex; gap: 6px; }
.ctrl-btn { font-size: 11px; padding: 4px 12px; border-radius: 6px; border: 1px solid var(--border); background: rgba(255,255,255,.05); color: var(--muted); cursor: pointer; transition: background .15s, color .15s; }
.ctrl-btn:hover { background: rgba(14,165,233,.15); color: var(--blue); border-color: rgba(14,165,233,.3); }

/* Section header */
.section-hdr { font-size: 11px; text-transform: uppercase; letter-spacing: .9px; font-weight: 700; color: var(--muted); padding: 9px 18px; background: var(--card2); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }

/* Two-column */
.two-col { display: grid; grid-template-columns: 1fr 32px 1fr; }
.col-cr  { padding: 14px 18px; background: var(--cr-bg); border-right: 1px solid var(--cr-border); }
.col-ctm { padding: 14px 18px; background: var(--ctm-bg); }
.col-arr { display: flex; align-items: center; justify-content: center; background: var(--card2); color: var(--muted); font-size: 13px; border-left: 1px solid var(--border); border-right: 1px solid var(--border); }

/* Platform labels */
.plat-label { font-size: 10px; text-transform: uppercase; letter-spacing: .9px; font-weight: 700; margin-bottom: 10px; }
.cr-label  { color: var(--cr); }
.ctm-label { color: var(--blue); }

/* Inventory */
.inv-grid { display: grid; grid-template-columns: repeat(5,1fr); gap: 6px; }
.inv-item { text-align: center; padding: 8px 4px; background: rgba(255,255,255,.03); border-radius: 7px; border: 1px solid rgba(255,255,255,.05); }
.inv-num { font-size: 22px; font-weight: 700; line-height: 1.1; }
.inv-lbl { font-size: 9px; text-transform: uppercase; color: var(--muted); margin-top: 2px; letter-spacing: .4px; }

/* Tracker column header */
.tracker-col-hdr .col-cr, .tracker-col-hdr .col-ctm { padding: 8px 18px; border-top: 1px solid var(--border); }
.trackers-outer { }

/* Collapsible tracker */
.tracker-details { border-top: 1px solid var(--border); }
.tracker-details > summary { list-style: none; cursor: pointer; padding: 10px 18px; display: flex; align-items: center; gap: 8px; user-select: none; }
.tracker-details > summary::-webkit-details-marker { display: none; }
.tracker-details[open] > summary { border-bottom: 1px solid var(--border); background: rgba(255,255,255,.02); }
.tracker-details:hover > summary { background: rgba(255,255,255,.03); }
.ts-name { font-weight: 600; font-size: 13px; flex: 0 0 auto; max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ts-meta { color: var(--muted); font-size: 11px; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ts-arrow { margin-left: 4px; color: var(--muted); font-size: 11px; transition: transform .15s; flex-shrink: 0; }
.tracker-details[open] .ts-arrow { transform: rotate(90deg); }
.flag { font-size: 10px; color: var(--blue); background: rgba(14,165,233,.1); border: 1px solid rgba(14,165,233,.2); border-radius: 4px; padding: 1px 5px; white-space: nowrap; }

/* Disabled trackers */
.disabled-hdr { padding: 7px 18px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; background: var(--card2); border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.disabled-wrap { opacity: .65; }
.empty-msg { padding: 16px 18px; color: var(--muted); font-size: 12px; }

/* Badges */
.badge { display: inline-block; padding: 1px 7px; border-radius: 20px; font-size: 10px; font-weight: 600; margin-right: 3px; vertical-align: middle; }
.badge-session  { background: rgba(132,204,22,.13); color: var(--lime); border: 1px solid rgba(132,204,22,.25); }
.badge-source   { background: rgba(14,165,233,.13); color: var(--blue); border: 1px solid rgba(14,165,233,.25); }
.badge-active   { background: rgba(16,185,129,.13); color: var(--green); border: 1px solid rgba(16,185,129,.25); }
.badge-disabled { background: rgba(239,68,68,.13); color: var(--red); border: 1px solid rgba(239,68,68,.25); }
.badge-role     { background: rgba(255,255,255,.07); color: var(--muted); border: 1px solid var(--border); }

/* Chips */
.chip { display: inline-block; background: var(--card2); border: 1px solid var(--border); border-radius: 4px; padding: 1px 7px; font-family: 'SF Mono','Fira Code',monospace; font-size: 11px; margin: 1px 2px 1px 0; }
.num-row { margin-bottom: 6px; }

/* Item rows */
.item { font-size: 12px; margin-bottom: 3px; }
.item .key { color: var(--muted); }
.item .val { font-weight: 500; }
.item .val.mono { font-family: 'SF Mono','Fira Code',monospace; font-size: 11px; }
.muted { color: var(--muted); } .small { font-size: 11px; }

/* Step flow */
.step-flow { margin-top: 6px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px; }
.step-chip { background: rgba(255,255,255,.05); border: 1px solid var(--border); border-radius: 4px; padding: 2px 7px; font-size: 11px; }
.step-arr { color: var(--muted); font-size: 12px; padding: 0 2px; }

/* CTM notes */
.ctm-notes { margin-top: 8px; }
.ctm-note-item { font-size: 11px; color: var(--lime); background: rgba(132,204,22,.05); border-left: 2px solid rgba(132,204,22,.35); padding: 3px 8px; margin-bottom: 3px; border-radius: 0 4px 4px 0; }
.ctm-note { font-size: 11px; color: var(--lime); background: rgba(132,204,22,.05); border-left: 2px solid rgba(132,204,22,.35); padding: 4px 10px; border-radius: 0 4px 4px 0; }

/* Integration blocks */
.integ-block { padding: 8px 0; border-bottom: 1px solid rgba(45,63,86,.5); }
.integ-block:last-child { border-bottom: none; }
.integ-name { font-weight: 600; font-size: 13px; margin-bottom: 3px; }
.integ-detail { font-size: 11px; color: var(--muted); margin-left: 8px; }
.compat-green  { color: var(--green); font-size: 12px; }
.compat-yellow { color: var(--yellow); font-size: 12px; }
.compat-red    { color: var(--red); font-size: 12px; }

/* Data tables */
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th { text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .5px; padding: 5px 0; border-bottom: 1px solid var(--border); font-weight: 600; }
.data-table td { padding: 6px 0; border-bottom: 1px solid rgba(45,63,86,.4); vertical-align: top; padding-right: 12px; }
.data-table tr:last-child td { border-bottom: none; }
.color-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }

/* Checklist + score */
.checklist-section { display: grid; grid-template-columns: 210px 1fr; border-top: 1px solid var(--border); }
.score-col { padding: 16px 18px; background: rgba(132,204,22,.04); border-right: 1px solid var(--border); }
.checklist-col { padding: 16px 18px; background: var(--ctm-bg); }
.sub-hdr { font-size: 10px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); font-weight: 700; margin: 12px 0 6px; }
.score-card { background: var(--card2); border: 1px solid var(--border); border-radius: 10px; padding: 14px; text-align: center; }
.score-num { font-size: 52px; font-weight: 800; line-height: 1; }
.score-denom { font-size: 17px; color: var(--muted); }
.score-lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-top: 5px; }
.score-high { color: var(--green); }
.score-mid  { color: var(--yellow); }
.score-low  { color: var(--red); }
.warning  { background: rgba(245,158,11,.07); border: 1px solid rgba(245,158,11,.2); border-radius: 6px; padding: 7px 10px; margin-bottom: 5px; font-size: 11px; color: var(--yellow); }
.warning::before { content: "⚠ "; }
.no-warn  { font-size: 11px; color: var(--green); background: rgba(16,185,129,.07); border: 1px solid rgba(16,185,129,.2); border-radius: 6px; padding: 7px 10px; }
.no-warn::before { content: "✓ "; }
.checklist { list-style: none; }
.checklist li { padding: 7px 0; border-bottom: 1px solid rgba(45,63,86,.4); display: flex; align-items: flex-start; gap: 8px; font-size: 12px; }
.checklist li:last-child { border-bottom: none; }
.check-icon { flex-shrink: 0; width: 17px; height: 17px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 9px; font-weight: 800; margin-top: 1px; }
.check-req { background: rgba(14,165,233,.18); color: var(--blue); }
.check-rec { background: rgba(132,204,22,.18); color: var(--lime); }
.check-body { flex: 1; }
.check-prio { font-size: 10px; color: var(--muted); margin-left: 4px; }

.footer { text-align: center; padding: 28px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--border); margin-top: 12px; }
"""

JS = """
function openAll(btn)  { btn.closest('.company-block').querySelectorAll('.tracker-details').forEach(d => d.open = true); }
function closeAll(btn) { btn.closest('.company-block').querySelectorAll('.tracker-details').forEach(d => d.open = false); }
"""


# ─── Full HTML Page ───────────────────────────────────────────────────────────
def render_full_html(
    agencies: List[Dict],
    accounts_data: List[Dict],
    generated_at: str,
) -> str:
    total_accts = len(accounts_data)
    total_cos = sum(len(a["companies"]) for a in accounts_data)
    total_trackers = sum(len(c["mapped_trackers"]) for a in accounts_data for c in a["companies"])
    total_numbers = sum(c["inventory"]["total"] for a in accounts_data for c in a["companies"])
    avg_score = int(
        sum(c["score"]["score"] for a in accounts_data for c in a["companies"])
        / max(1, total_cos)
    )
    score_cls = "score-high" if avg_score >= 85 else ("score-mid" if avg_score >= 65 else "score-low")

    kpi = (
        f"<div class='kpi-grid'>"
        f"<div class='kpi'><div class='kpi-label'>Accounts</div><div class='kpi-val'>{total_accts}</div></div>"
        f"<div class='kpi'><div class='kpi-label'>Companies</div><div class='kpi-val'>{total_cos}</div></div>"
        f"<div class='kpi'><div class='kpi-label'>Trackers</div><div class='kpi-val'>{total_trackers}</div></div>"
        f"<div class='kpi'><div class='kpi-label'>Numbers</div><div class='kpi-val'>{total_numbers}</div></div>"
        f"<div class='kpi'><div class='kpi-label'>Avg Readiness</div>"
        f"<div class='kpi-val {score_cls}'>{avg_score}</div><div class='kpi-sub'>out of 100</div></div>"
        f"</div>"
    )

    toc_items = "".join(
        f"<li><a href='#{esc(re.sub(r'[^a-z0-9]+', '-', c['name'].lower()))}'>{esc(c['name'])}</a></li>"
        for a in accounts_data for c in a["companies"]
    )
    toc = (f"<div class='toc'><div class='toc-title'>Jump to Company</div>"
           f"<ul class='toc-list'>{toc_items}</ul></div>") if total_cos > 1 else ""

    agency_html = ""
    if agencies:
        rows = "".join(
            f"<div style='font-size:12px;margin-bottom:3px;'><strong>{esc(ag.get('name','Agency'))}</strong> "
            f"<span class='muted'>ID: {esc(ag.get('id',''))} · Numeric: {esc(str(ag.get('numeric_id','—')))}</span></div>"
            for ag in agencies
        )
        agency_html = (
            f"<div style='background:rgba(14,165,233,.06);border:1px solid var(--ctm-border);"
            f"border-radius:8px;padding:12px 16px;margin-bottom:20px;'>"
            f"<div class='plat-label ctm-label' style='margin-bottom:8px;'>Agencies</div>{rows}</div>"
        )

    body = ""
    for a in accounts_data:
        aname = a["account_detail"].get("name") or a["account_meta"].get("name") or "Account"
        aid = a["account_meta"].get("id") or "—"
        num_id = a["account_meta"].get("numeric_id") or "—"

        # Account-level sections (users + tags)
        users_html = render_users_section(a.get("users", []))
        tags_html = render_tags_section(a.get("tags", []))
        acct_extra = ""
        if users_html or tags_html:
            inner = ""
            if users_html:
                inner += f"<div class='acct-section-hdr'>👤 Users / Agents</div>{users_html}"
            if tags_html:
                inner += f"<div class='acct-section-hdr' style='border-top:1px solid var(--border);'>🏷 Call Tags</div>{tags_html}"
            acct_extra = f"<div class='acct-section'>{inner}</div>"

        cos_html = "".join(
            render_company_block(
                c["name"], c["mapped_trackers"], c["mapped_integrations"],
                c.get("notifications", []), c["score"], c["inventory"]
            )
            for c in a["companies"]
        ) or "<div class='muted' style='padding:16px;'>No companies found.</div>"

        body += (
            f"<div class='acct-label'>{esc(aname)}"
            f"<span class='sub'>Account ID: {esc(str(aid))} · Numeric: {esc(str(num_id))}</span></div>"
            f"{acct_extra}"
            f"{cos_html}"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CallRail → CTM Migration Comparison</title>
  <style>{CSS}</style>
</head>
<body>
<div class="header">
  <div>
    <h1><span class="cr">CallRail</span><span class="arr">→</span><span class="ctm">CTM</span> Migration Comparison</h1>
    <div class="header-meta">Generated {esc(generated_at)} · Internal use only</div>
  </div>
  <div class="header-right">
    {total_accts} Account(s) · {total_cos} Company/ies
    <div class="sub">CallTrackingMetrics migration plan</div>
  </div>
</div>
<div class="container">
  {kpi}
  {agency_html}
  {toc}
  {body}
</div>
<div class="footer">CallRail → CallTrackingMetrics · Internal document · {esc(generated_at)}</div>
<script>{JS}</script>
</body>
</html>"""


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="CallRail → CTM migration comparison HTML")
    parser.add_argument("--api-key",      default=os.getenv("CALLRAIL_API_KEY"))
    parser.add_argument("--out-dir",      default=".", help="Output directory (default: .)")
    parser.add_argument("--webhook-url",  default=os.getenv("MAKE_WEBHOOK_URL"))
    parser.add_argument("--max-workers",  type=int, default=10)
    parser.add_argument("--log-level",    default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("cr_ctm_compare")

    if not args.api_key:
        raise SystemExit("CALLRAIL_API_KEY required (env var or --api-key).")

    generated_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    logger.info("Fetching accessible accounts...")
    listing = list_accessible_accounts(args.api_key, logger)
    accounts_meta = listing.get("accounts", [])
    agencies_meta = listing.get("agencies", [])

    if not accounts_meta:
        raise SystemExit("No accounts found for this API token.")

    accounts_data: List[Dict] = []

    for account_meta in accounts_meta:
        numeric_id = account_meta.get("numeric_id")
        if not numeric_id:
            logger.warning("Skipping %s — no numeric_id", account_meta.get("name"))
            continue

        client = CallRailClient(str(numeric_id), args.api_key, logger)

        try:
            account_detail = client.get(".json")
        except Exception as exc:
            logger.error("Cannot load account %s: %s", account_meta.get("name"), exc)
            continue

        aname = account_detail.get("name") or account_meta.get("name") or "Account"

        # Account-level resources
        logger.info("  Fetching users for %s…", aname)
        users = client.try_paginate("/users.json", "users", {"per_page": 250})

        logger.info("  Fetching tags for %s…", aname)
        tags = client.try_paginate("/tags.json", "tags", {"per_page": 250})

        companies = client.paginate("/companies.json", "companies", {"per_page": 250})
        company_results: List[Dict] = []

        for company in companies:
            cname = company.get("name") or "Unnamed Company"
            logger.info("  Processing %s / %s", aname, cname)

            trackers = client.paginate(
                "/trackers.json", "trackers",
                {"company_id": company["id"], "per_page": 250}
            )
            detailed = fetch_tracker_details(client, trackers, logger, args.max_workers)

            integrations = client.try_paginate(
                "/integrations.json", "integrations",
                {"company_id": company["id"], "per_page": 250}
            )
            notifications = client.try_paginate(
                "/notifications.json", "notifications",
                {"company_id": company["id"], "per_page": 250}
            )

            mapped_trackers = [map_tracker_to_ctm(t) for t in detailed]
            mapped_integrations = map_integrations(integrations)
            inventory = summarize_inventory(detailed)
            score_data = compute_migration_score(mapped_trackers, mapped_integrations, users, tags)

            company_results.append({
                "name": cname,
                "mapped_trackers": mapped_trackers,
                "mapped_integrations": mapped_integrations,
                "notifications": notifications,
                "inventory": inventory,
                "score": score_data,
            })

        accounts_data.append({
            "account_meta": account_meta,
            "account_detail": account_detail,
            "users": users,
            "tags": tags,
            "companies": company_results,
        })

    logger.info("Rendering HTML…")
    html_out = render_full_html(agencies_meta, accounts_data, generated_at)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = generated_at.replace(":", "-").replace("+", "Z")[:19]
    out_path = out_dir / f"migration_comparison_{ts}.html"
    out_path.write_text(html_out, encoding="utf-8")
    logger.info("Saved → %s", out_path)

    if args.webhook_url:
        logger.info("Posting to webhook…")
        _SESSION.post(
            args.webhook_url,
            json={
                "subject": f"CTM Migration Comparison — {len(accounts_data)} Account(s)",
                "html": html_out,
                "generated_at": generated_at,
            },
            timeout=60,
        ).raise_for_status()
        logger.info("Webhook posted.")

    logger.info("Done.")


if __name__ == "__main__":
    main()
