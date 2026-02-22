"""
apify_cron_job.py
-----------------
Runs at 7:00 AM CST every weekday via Railway cron.

Flow:
  1. Apify scrapes leads (dental/medspa/weight loss in TX + MA)
  2. Hunter.io enriches email from practice website domain
  3. Deduplicate against BOTH Supabase pending_leads AND HubSpot
  4. Filter by location count (max 5) to hit ICP of 1-10M ARR
  5. Send Carolina email digest (Gmail SMTP) + optional Slack
  6. Store qualified leads in Supabase pending_leads

ICP targeting:
  - 1-5 locations (small-mid DSO, independent practices)
  - Dental, med spa, weight loss verticals
  - TX and MA markets
"""

from __future__ import annotations

import os
import ssl
import asyncio
import httpx
import smtplib
import requests as req_sync
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────────
APIFY_TOKEN    = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID")
HUNTER_KEY     = os.getenv("HUNTER_API_KEY")
HUBSPOT_KEY    = os.getenv("HUBSPOT_API_KEY")
SLACK_TOKEN    = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL  = os.getenv("SLACK_CHANNEL_ID")
CAROLINA_EMAIL = os.getenv("CAROLINA_EMAIL", "carolina@opushealth.io")
GMAIL_USER     = os.getenv("GMAIL_USER")
GMAIL_APP_PWD  = os.getenv("GMAIL_APP_PASSWORD")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
LGM_API_KEY    = os.getenv("LGM_API_KEY")
LGM_AUDIENCE_ID = os.getenv("LGM_AUDIENCE_ID")

LEADS_PER_DAY  = 50
MAX_LOCATIONS  = 5   # ICP filter — skip chains with more than this many locations

# ── Init ──────────────────────────────────────────────────────────────────────
for name, val in [("APIFY_API_TOKEN", APIFY_TOKEN), ("APIFY_ACTOR_ID", APIFY_ACTOR_ID),
                  ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)]:
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── HubSpot REST helper (no SDK) ──────────────────────────────────────────────
def hs(method: str, path: str, **kwargs) -> dict:
    if not HUBSPOT_KEY:
        return {}
    url = f"https://api.hubapi.com{path}"
    headers = {"Authorization": f"Bearer {HUBSPOT_KEY}", "Content-Type": "application/json"}
    resp = req_sync.request(method, url, headers=headers, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _safe_domain(url: str) -> str:
    if not url:
        return ""
    return url.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip()

def _e164(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}"


# ── Step 1: Fetch leads from Apify ───────────────────────────────────────────
async def fetch_apify_leads() -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=180) as client:
        run_resp = await client.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs",
            headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
            json={
                "searchStringsArray": [
                    "dental practice Dallas TX",
                    "dental practice Austin TX",
                    "dental practice Houston TX",
                    "dental practice San Antonio TX",
                    "dental practice Boston MA",
                    "med spa Dallas TX",
                    "med spa Austin TX",
                    "med spa Houston TX",
                    "med spa Boston MA",
                    "weight loss clinic Dallas TX",
                    "weight loss clinic Houston TX",
                    "weight loss clinic Austin TX",
                    "weight loss clinic Boston MA",
                ],
                "maxCrawledPlacesPerSearch": 8,
                "language": "en",
                "countryCode": "us",
            },
        )
        if run_resp.status_code >= 300:
            print(f"[APIFY] Failed to start: {run_resp.status_code}")
            return []

        run_id = run_resp.json().get("data", {}).get("id")
        print(f"[APIFY] Started run {run_id}")

        status_payload = None
        for attempt in range(40):
            await asyncio.sleep(5)
            sr = await client.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
            )
            status_payload = sr.json().get("data", {})
            status = status_payload.get("status")
            print(f"[APIFY] Status: {status} (attempt {attempt+1})")
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                print(f"[APIFY] Run failed: {status}")
                return []

        dataset_id = status_payload.get("defaultDatasetId")
        items_resp = await client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items?limit={LEADS_PER_DAY * 3}",
            headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
        )
        raw = items_resp.json() if isinstance(items_resp.json(), list) else []

        leads = []
        for item in raw:
            phone = _e164(item.get("phone") or "")
            if not phone:
                continue

            # ── ICP filter: skip large chains ─────────────────────────────
            # Apify returns additionalInfo or similar — use totalScore and
            # reviewsCount as proxies. More reliably: check if the name
            # contains chain indicators or if locationsCount is available.
            locations_count = (
                item.get("locationsCount")
                or item.get("additionalInfo", {}).get("locationsCount")
                or item.get("numberOfLocations")
                or 1  # default to 1 if unknown (independent practice)
            )
            try:
                locations_count = int(locations_count)
            except (TypeError, ValueError):
                locations_count = 1

            if locations_count > MAX_LOCATIONS:
                print(f"[ICP] Skipping {item.get('title')} — {locations_count} locations (too large)")
                continue

            category = (item.get("categoryName") or "").lower()
            if "dental" in category or "orthodon" in category:
                vertical = "dental"
            elif "spa" in category or "aesthet" in category or "laser" in category:
                vertical = "medspa"
            else:
                vertical = "weight_loss"

            leads.append({
                "practice_name":  item.get("title") or "",
                "contact_name":   "",
                "phone":          phone,
                "email":          "",
                "vertical":       vertical,
                "city":           item.get("city") or "",
                "state":          item.get("state") or "",
                "website":        item.get("website") or "",
                "rating":         item.get("totalScore"),
                "locations_count": locations_count,
            })

        print(f"[APIFY] Got {len(raw)} raw, {len(leads)} with phone + ICP filter passed")
        return leads


# ── Step 2: Hunter email enrichment ──────────────────────────────────────────
async def enrich_email(website: str) -> str:
    if not website or not HUNTER_KEY:
        return ""
    domain = _safe_domain(website)
    if not domain:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": HUNTER_KEY, "limit": 5},
            )
            emails = resp.json().get("data", {}).get("emails", [])
            personal = [e for e in emails if e.get("type") == "personal" and e.get("confidence", 0) >= 80]
            generic  = [e for e in emails if e.get("type") == "generic"  and e.get("confidence", 0) >= 70]
            best = personal[0] if personal else (generic[0] if generic else None)
            email = best.get("value", "") if best else ""
            if email:
                print(f"[HUNTER] Found email for {domain}: {email} (confidence={best.get('confidence')})")
            return email
    except Exception as e:
        print(f"[HUNTER] Error for {domain}: {e}")
        return ""


# ── Step 3: Dedup against Supabase AND HubSpot ───────────────────────────────
def already_seen(phone: str, email: str) -> bool:
    """Check Supabase pending_leads first (fast/cheap), then HubSpot (slower)."""

    # 1. Supabase dedup by phone
    try:
        result = supabase.table("pending_leads").select("id").eq("phone", phone).limit(1).execute()
        if result.data:
            return True
    except Exception as e:
        print(f"[DEDUP] Supabase phone check error: {e}")

    # 2. Supabase dedup by email
    if email:
        try:
            result = supabase.table("pending_leads").select("id").eq("email", email).limit(1).execute()
            if result.data:
                return True
        except Exception as e:
            print(f"[DEDUP] Supabase email check error: {e}")

    # 3. HubSpot dedup by phone
    if HUBSPOT_KEY:
        try:
            search = hs("POST", "/crm/v3/objects/contacts/search", json={
                "filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone}]}],
                "limit": 1
            })
            if search.get("total", 0) > 0:
                return True
        except Exception as e:
            print(f"[DEDUP] HubSpot phone check error: {e}")

        # 4. HubSpot dedup by email
        if email:
            try:
                search = hs("POST", "/crm/v3/objects/contacts/search", json={
                    "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
                    "limit": 1
                })
                if search.get("total", 0) > 0:
                    return True
            except Exception as e:
                print(f"[DEDUP] HubSpot email check error: {e}")

    return False


# ── Step 4: Store in Supabase ─────────────────────────────────────────────────
def store_lead(lead: Dict[str, Any]) -> None:
    try:
        supabase.table("pending_leads").upsert({
            "practice_name": lead.get("practice_name", ""),
            "contact_name":  lead.get("contact_name", ""),
            "phone":         lead.get("phone", ""),
            "email":         lead.get("email", ""),
            "vertical":      lead.get("vertical", ""),
            "city":          lead.get("city", ""),
            "state":         lead.get("state", ""),
            "website":       lead.get("website", ""),
            "google_rating": lead.get("rating"),
            "phone_valid":   bool(lead.get("phone")),
            "email_valid":   bool(lead.get("email")),
            "email_status":  lead.get("email_status", ""),
            "status":        "pending",
            "created_date":  date.today().isoformat(),
            "notes":         f"locations={lead.get('locations_count', 1)}",
        }, on_conflict="phone").execute()
    except Exception as e:
        print(f"[SUPABASE] Store error: {e}")


# ── Step 5: Email digest via Gmail SMTP ───────────────────────────────────────
def _build_html(leads: List[Dict[str, Any]]) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    rows = ""
    for i, lead in enumerate(leads, 1):
        website = lead.get("website") or ""
        ws_cell = f'<a href="{website}">{website}</a>' if website else "—"
        locs = lead.get("locations_count", 1)
        rows += f"""
        <tr style="background:{'#f9f9f9' if i%2==0 else 'white'};">
            <td style="padding:9px;border-bottom:1px solid #eee;">{i}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;"><strong>{lead.get('practice_name','')}</strong></td>
            <td style="padding:9px;border-bottom:1px solid #eee;">{lead.get('phone','—')}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;">{lead.get('email','—')}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;">{lead.get('city','')}, {lead.get('state','')}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;">{str(lead.get('vertical','')).title()}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;text-align:center;">{locs}</td>
            <td style="padding:9px;border-bottom:1px solid #eee;font-size:11px;">{ws_cell}</td>
        </tr>"""

    return f"""<html><body style="font-family:Arial,sans-serif;max-width:1100px;margin:0 auto;">
    <div style="background:#111827;padding:18px;border-radius:12px 12px 0 0;">
        <h1 style="color:white;margin:0;font-size:20px;">🦷 Opus Health — Daily Lead Digest</h1>
        <p style="color:rgba(255,255,255,0.8);margin:5px 0 0;font-size:13px;">{today} · {len(leads)} leads ready for calls · Max {MAX_LOCATIONS} locations (ICP filter)</p>
    </div>
    <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;overflow:hidden;">
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
            <thead><tr style="background:#f3f4f6;">
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">#</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Practice</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Phone</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Email</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Location</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Vertical</th>
                <th style="padding:9px;text-align:center;border-bottom:2px solid #e5e7eb;">Locations</th>
                <th style="padding:9px;text-align:left;border-bottom:2px solid #e5e7eb;">Website</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    <p style="color:#6b7280;font-size:12px;margin-top:12px;">
        Deduped against Supabase + HubSpot · Email via Hunter.io · ICP: ≤{MAX_LOCATIONS} locations · Stored as pending leads
    </p>
    </body></html>"""


async def send_email_digest(leads: List[Dict[str, Any]]) -> None:
    if not GMAIL_USER or not GMAIL_APP_PWD or not CAROLINA_EMAIL:
        print("[EMAIL] Skipping - missing Gmail credentials or CAROLINA_EMAIL")
        return

    today = datetime.now().strftime("%A, %B %d, %Y")
    msg = MIMEMultipart("alternative")
    msg["From"]    = GMAIL_USER
    msg["To"]      = CAROLINA_EMAIL
    msg["Subject"] = f"🦷 {len(leads)} Fresh Leads — {today}"
    msg.attach(MIMEText(_build_html(leads), "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(GMAIL_USER, GMAIL_APP_PWD)
            server.sendmail(GMAIL_USER, [CAROLINA_EMAIL], msg.as_string())
        print(f"[EMAIL] ✅ Digest sent to {CAROLINA_EMAIL}")
    except Exception as e:
        print(f"[EMAIL] ❌ Failed: {e}")


# ── Step 6: Slack digest (optional) ──────────────────────────────────────────
async def send_slack_digest(leads: List[Dict[str, Any]]) -> None:
    if not SLACK_TOKEN or not SLACK_CHANNEL:
        print("[SLACK] Skipping - no token/channel configured")
        return

    today = datetime.now().strftime("%A, %B %d")
    lines = [f"🦷 *Opus Health — Daily Lead Digest* | {today}\n{len(leads)} leads ready.\n"]
    for i, lead in enumerate(leads, 1):
        locs = lead.get("locations_count", 1)
        lines.append(
            f"*{i}. {lead.get('practice_name','')}* ({locs} loc)\n"
            f"   📞 `{lead.get('phone','')}` | 📧 {lead.get('email','—')} | "
            f"📍 {lead.get('city','')}, {lead.get('state','')} | {str(lead.get('vertical','')).title()}"
        )

    full = "\n\n".join(lines)
    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in [full[i:i+2900] for i in range(0, len(full), 2900)]:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": chunk, "mrkdwn": True},
            )
    print(f"[SLACK] ✅ Sent ({len(leads)} leads)")


# ── Main ──────────────────────────────────────────────────────────────────────
async def run_daily_cron() -> None:
    print("\n" + "="*60)
    print(f"[CRON] Starting — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

    raw_leads = await fetch_apify_leads()
    if not raw_leads:
        print("[CRON] No leads from Apify. Exiting.")
        return

    qualified: List[Dict[str, Any]] = []

    for lead in raw_leads:
        if len(qualified) >= LEADS_PER_DAY:
            break

        phone = lead.get("phone", "")
        if not phone:
            continue

        # Enrich email first so we can dedup on both phone AND email
        email = await enrich_email(lead.get("website", "") or "")
        lead["email"] = email
        lead["email_status"] = "hunter" if email else "not_found"

        # Dedup against Supabase + HubSpot
        if already_seen(phone, email):
            print(f"[DEDUP] Skipping {lead.get('practice_name')} — already in pipeline")
            continue

        store_lead(lead)
        qualified.append(lead)

    print(f"\n[CRON] ✅ {len(qualified)} qualified leads\n")

    if not qualified:
        print("[CRON] Nothing new today.")
        return

    await asyncio.gather(
        send_slack_digest(qualified),
        send_email_digest(qualified),
    )

    print(f"\n[CRON] Done at {datetime.now().strftime('%I:%M %p')}")


if __name__ == "__main__":
    asyncio.run(run_daily_cron())
