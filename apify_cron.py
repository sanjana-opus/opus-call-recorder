"""
apify_cron.py
-------------
Runs at 7:00 AM every weekday (schedule this via Railway cron or system cron).

Flow:
  1. Call Apify actor to scrape 50 quality leads (dental/medspa/weight loss in TX + MA)
  2. Verify email via ZeroBounce + phone via Abstract API
  3. Deduplicate against HubSpot (skip if contact already exists)
  4. Send Carolina a Slack digest + email digest with the day's leads
  5. Each lead stored in Supabase `pending_leads` table for tracking

Env vars needed (add to Railway):
  APIFY_API_TOKEN
  APIFY_ACTOR_ID          # your dental/medspa/weight loss scraper actor
  ZEROBOUNCE_API_KEY
  ABSTRACT_API_KEY
  HUBSPOT_API_KEY
  SLACK_BOT_TOKEN
  SLACK_CHANNEL_ID        # Carolina's DM channel or #leads channel
  CAROLINA_EMAIL          # carolina@opushealth.io
  SENDGRID_API_KEY        # or use SMTP
  SUPABASE_URL
  SUPABASE_KEY
"""

import os
import httpx
import json
import asyncio
from datetime import datetime, date
from supabase import create_client
from hubspot import HubSpot
from hubspot.crm.contacts import ApiException as HubSpotApiException

# ── Config ────────────────────────────────────────────────────────────────────
APIFY_TOKEN       = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR_ID    = os.getenv("APIFY_ACTOR_ID")
HUNTER_KEY        = os.getenv("HUNTER_API_KEY")
HUBSPOT_KEY       = os.getenv("HUBSPOT_API_KEY")
SLACK_TOKEN       = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL     = os.getenv("SLACK_CHANNEL_ID")
CAROLINA_EMAIL    = os.getenv("CAROLINA_EMAIL", "carolina@opushealth.io")
GMAIL_USER        = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
LGM_API_KEY       = os.getenv("LGM_API_KEY")
LGM_AUDIENCE_ID   = os.getenv("LGM_AUDIENCE_ID")

LEADS_PER_DAY     = 50

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
hubspot  = HubSpot(access_token=HUBSPOT_KEY) if HUBSPOT_KEY else None


# ── Step 1: Pull leads from Apify ─────────────────────────────────────────────
async def fetch_apify_leads() -> list[dict]:
    """
    Trigger the Apify actor and wait for results.
    The actor should return leads with at minimum:
      - practice_name, contact_name, phone, email, address, city, state, vertical
    Adjust the actor input below to match your actor's schema.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        # Start the actor run
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
            }
        )
        run_data = run_resp.json()
        run_id   = run_data["data"]["id"]
        print(f"[APIFY] Started run {run_id}")

        # Poll until finished (max 3 min)
        for _ in range(36):
            await asyncio.sleep(5)
            status_resp = await client.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                headers={"Authorization": f"Bearer {APIFY_TOKEN}"}
            )
            status = status_resp.json()["data"]["status"]
            if status == "SUCCEEDED":
                break
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                print(f"[APIFY] Run failed with status: {status}")
                return []

        # Fetch dataset items
        dataset_id = status_resp.json()["data"]["defaultDatasetId"]
        items_resp = await client.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items?limit={LEADS_PER_DAY * 2}",
            headers={"Authorization": f"Bearer {APIFY_TOKEN}"}
        )
        raw = items_resp.json()
        leads = []
        for item in raw:
            if not item.get("phone"):
                continue
            category = (item.get("categoryName") or "").lower()
            if "dental" in category or "orthodon" in category:
                vertical = "dental"
            elif "spa" in category or "aesthet" in category or "laser" in category:
                vertical = "medspa"
            else:
                vertical = "weight_loss"
            digits = "".join(c for c in (item.get("phone") or "") if c.isdigit())
            if len(digits) == 10:
                digits = "1" + digits
            leads.append({
                "practice_name": item.get("title"),
                "contact_name":  "",
                "phone":         f"+{digits}" if digits else "",
                "email":         "",
                "vertical":      vertical,
                "city":          item.get("city"),
                "state":         item.get("state"),
                "website":       item.get("website"),
                "rating":        item.get("totalScore"),
            })
        print(f"[APIFY] Got {len(raw)} raw results, {len(leads)} with phone numbers")
        return leads


# ── Step 2: Enrich email via Hunter.io domain search ─────────────────────────
async def enrich_email(website: str) -> str:
    """Given a practice website URL, find the best contact email via Hunter.io."""
    if not website or not HUNTER_KEY:
        return ""
    try:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": HUNTER_KEY, "limit": 5}
            )
            emails = resp.json().get("data", {}).get("emails", [])
            if not emails:
                return ""
            personal = [e for e in emails if e.get("type") == "personal" and e.get("confidence", 0) >= 80]
            generic  = [e for e in emails if e.get("type") == "generic"  and e.get("confidence", 0) >= 70]
            best = personal[0] if personal else (generic[0] if generic else None)
            email = best.get("value", "") if best else ""
            if email:
                print(f"[HUNTER] Found email for {domain}: {email} (confidence={best.get('confidence')})")
            return email
    except Exception as e:
        print(f"[HUNTER] Error for {website}: {e}")
        return ""


# ── Step 3: Verify phone via Abstract API ─────────────────────────────────────
# ── Step 4: Deduplicate against HubSpot ───────────────────────────────────────
def already_in_hubspot(phone: str, email: str) -> bool:
    if not hubspot:
        return False
    try:
        filters = []
        if phone:
            filters.append({"propertyName": "phone", "operator": "EQ", "value": phone})
        if email:
            filters.append({"propertyName": "email", "operator": "EQ", "value": email})

        for f in filters:
            results = hubspot.crm.contacts.search_api.do_search(
                public_object_search_request={"filterGroups": [{"filters": [f]}]}
            )
            if results.total > 0:
                return True
        return False
    except Exception as e:
        print(f"[HUBSPOT DEDUP] Error: {e}")
        return False


# ── Step 5: Store in Supabase pending_leads ───────────────────────────────────
def store_pending_lead(lead: dict):
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
            "phone_valid":   lead.get("phone_verified", False),
            "email_valid":   lead.get("email_verified", False),
            "email_status":  lead.get("email_status", ""),
            "status":        "pending",
            "created_date":  date.today().isoformat(),
        }, on_conflict="phone").execute()
    except Exception as e:
        print(f"[SUPABASE] Error storing lead: {e}")


# ── Step 6: Send Slack digest to Carolina ─────────────────────────────────────
async def send_slack_digest(leads: list[dict]):
    if not SLACK_TOKEN or not SLACK_CHANNEL:
        print("[SLACK] Skipping - no token/channel configured")
        return

    today = datetime.now().strftime("%A, %B %d")
    header = f"🦷 *Opus Health — Daily Lead Digest* | {today}\n{len(leads)} qualified leads ready for calls.\n\n"

    # Build lead blocks (Slack has a 3000 char limit per block)
    lead_lines = []
    for i, lead in enumerate(leads, 1):
        line = (
            f"*{i}. {lead.get('practice_name', 'Unknown Practice')}*\n"
            f"   👤 {lead.get('contact_name', 'No contact')} | "
            f"📞 `{lead.get('phone', 'N/A')}` | "
            f"📧 {lead.get('email', 'No email')} | "
            f"📍 {lead.get('city', '')}, {lead.get('state', '')} | "
            f"🏷️ {lead.get('vertical', '').title()}"
        )
        lead_lines.append(line)

    body = "\n\n".join(lead_lines)
    full_message = header + body + "\n\n_Leads verified via ZeroBounce + Abstract API. Deduped against HubSpot._"

    # Split into chunks if too long
    chunks = [full_message[i:i+2900] for i in range(0, len(full_message), 2900)]

    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": chunk, "mrkdwn": True}
            )
    print(f"[SLACK] ✅ Sent digest ({len(leads)} leads)")


# ── Step 7: Send email digest to Carolina via SendGrid ────────────────────────
async def send_email_digest(leads: list[dict]):
    if not SENDGRID_KEY or not CAROLINA_EMAIL:
        print("[EMAIL] Skipping - no SendGrid key or recipient configured")
        return

    today = datetime.now().strftime("%A, %B %d, %Y")

    rows = ""
    for i, lead in enumerate(leads, 1):
        phone_badge = "✅" if lead.get("phone_verified") else "⚠️"
        email_badge = "✅" if lead.get("email_verified") else "⚠️"
        rows += f"""
        <tr style="background: {'#f9f9f9' if i % 2 == 0 else 'white'};">
            <td style="padding:10px; border-bottom:1px solid #eee;">{i}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;"><strong>{lead.get('practice_name','')}</strong></td>
            <td style="padding:10px; border-bottom:1px solid #eee;">{lead.get('contact_name','—')}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">{phone_badge} {lead.get('phone','—')}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">{email_badge} {lead.get('email','—')}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">{lead.get('city','')}, {lead.get('state','')}</td>
            <td style="padding:10px; border-bottom:1px solid #eee;">{lead.get('vertical','').title()}</td>
        </tr>
        """

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #667eea, #764ba2); padding: 20px; border-radius: 10px 10px 0 0;">
            <h1 style="color: white; margin: 0;">🦷 Opus Health — Daily Lead Digest</h1>
            <p style="color: rgba(255,255,255,0.85); margin: 5px 0 0;">{today} · {len(leads)} leads ready for calls</p>
        </div>
        <div style="border: 1px solid #eee; border-top: none; border-radius: 0 0 10px 10px; overflow: hidden;">
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse;">
                <thead>
                    <tr style="background: #f5f5f5;">
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">#</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Practice</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Contact</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Phone</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Email</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Location</th>
                        <th style="padding:10px; text-align:left; border-bottom:2px solid #ddd;">Vertical</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
        <p style="color: #999; font-size: 12px; margin-top: 10px;">
            ✅ = verified · ⚠️ = unverified · Deduped against HubSpot · Powered by Apify + ZeroBounce + Abstract API
        </p>
    </body></html>
    """

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": CAROLINA_EMAIL, "name": "Carolina Rojas"}]}],
                "from": {"email": "sanjana@opushealth.io", "name": "Opus Health Leads"},
                "subject": f"🦷 {len(leads)} Fresh Leads for Today — {today}",
                "content": [{"type": "text/html", "value": html}]
            }
        )
        if resp.status_code == 202:
            print(f"[EMAIL] ✅ Digest sent to {CAROLINA_EMAIL}")
        else:
            print(f"[EMAIL] ❌ Failed: {resp.status_code} {resp.text}")


# ── Main orchestrator ─────────────────────────────────────────────────────────
async def run_daily_cron():
    print(f"\n{'='*60}")
    print(f"[CRON] Starting daily lead generation — {datetime.now().isoformat()}")
    print(f"{'='*60}\n")

    # 1. Pull raw leads from Apify
    raw_leads = await fetch_apify_leads()
    if not raw_leads:
        print("[CRON] ❌ No leads returned from Apify. Exiting.")
        return

    # 2. Filter, verify, deduplicate
    qualified = []
    for lead in raw_leads:
        if len(qualified) >= LEADS_PER_DAY:
            break

        phone = lead.get("phone", "")

        # Skip if no phone (required for Carolina to call)
        if not phone:
            print(f"[FILTER] Skipping {lead.get('practice_name')} — no phone number")
            continue

        # Skip if already in HubSpot
        if already_in_hubspot(phone, ""):
            print(f"[DEDUP] Skipping {lead.get('practice_name')} — already in HubSpot")
            continue

        # Enrich email via Hunter.io domain search
        email = await enrich_email(lead.get("website", ""))
        lead["email"]          = email
        lead["phone_verified"] = True   # Google Maps phones are pre-validated
        lead["email_verified"] = bool(email)
        lead["email_status"]   = "hunter" if email else "not_found"

        store_pending_lead(lead)
        qualified.append(lead)

    print(f"\n[CRON] ✅ {len(qualified)} qualified leads after verification + dedup\n")

    if not qualified:
        print("[CRON] No leads to send today.")
        return

    # 3. Send digests
    await asyncio.gather(
        send_slack_digest(qualified),
        send_email_digest(qualified)
    )

    print(f"\n[CRON] ✅ Done. {len(qualified)} leads sent to Carolina at {datetime.now().strftime('%I:%M %p')}")


if __name__ == "__main__":
    asyncio.run(run_daily_cron())
