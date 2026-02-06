from fastapi import FastAPI, Request
from fastapi.responses import Response, HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import asyncio
import httpx
import json
import os
from datetime import datetime
from contextlib import asynccontextmanager
from io import StringIO
import csv
from typing import Any
from deepgram import DeepgramClient, PrerecordedOptions
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from hubspot import HubSpot
from hubspot.crm.contacts import (
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
)

SUPABASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sales_calls (
    id SERIAL PRIMARY KEY,
    call_sid TEXT UNIQUE NOT NULL,
    phone_number TEXT,
    caller_name TEXT,
    practice_name TEXT,
    status TEXT,
    transcript TEXT,
    analysis JSONB,
    recording_url TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);
"""


def _safe_json_parse(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def get_supabase() -> SupabaseClient | None:
    global supabase
    if supabase is None and SUPABASE_URL and SUPABASE_KEY:
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            print("[SUPABASE] Client initialized")
        except Exception as exc:
            print(f"[SUPABASE ERROR] Failed to initialize client: {exc}")
            supabase = None
    return supabase


def _run_sync(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)


async def init_db():
    client = get_supabase()
    if not client:
        print("[SUPABASE] Missing SUPABASE_URL or SUPABASE_KEY. DB features disabled.")
        return
    try:
        await _run_sync(client.table("sales_calls").select("id").limit(1).execute)
        print("[SUPABASE] Connection test passed")
    except Exception as exc:
        print(f"[SUPABASE ERROR] Connection test failed: {exc}")
        print(f"[SUPABASE] Ensure table exists using SQL:\n{SUPABASE_SCHEMA_SQL}")


async def db_insert_call(call_sid: str, phone_number: str, caller_name: str, practice_name: str):
    client = get_supabase()
    if not client:
        return
    payload = {
        "call_sid": call_sid,
        "phone_number": phone_number,
        "caller_name": caller_name,
        "practice_name": practice_name,
        "status": "initiated",
    }
    try:
        await _run_sync(client.table("sales_calls").insert(payload).execute)
    except Exception as exc:
        print(f"[SUPABASE ERROR] Insert call failed: {exc}")


async def db_update_call_status(call_sid: str, status: str):
    client = get_supabase()
    if not client:
        return
    try:
        await _run_sync(client.table("sales_calls").update({"status": status}).eq("call_sid", call_sid).execute)
    except Exception as exc:
        print(f"[SUPABASE ERROR] Update call status failed: {exc}")


async def db_complete_call(call_sid: str, transcript: str, analysis: dict, recording_url: str | None):
    client = get_supabase()
    if not client:
        return
    payload = {
        "transcript": transcript,
        "analysis": analysis,
        "recording_url": recording_url,
        "status": "completed",
        "completed_at": datetime.now().isoformat(),
    }
    try:
        await _run_sync(client.table("sales_calls").update(payload).eq("call_sid", call_sid).execute)
    except Exception as exc:
        print(f"[SUPABASE ERROR] Complete call update failed: {exc}")


async def db_set_error(call_sid: str):
    await db_update_call_status(call_sid, "error")


async def db_recent_calls(limit: int = 10):
    client = get_supabase()
    if not client:
        return []
    try:
        resp = await _run_sync(client.table("sales_calls").select("call_sid,phone_number,caller_name,practice_name,status,created_at,transcript").order("created_at", desc=True).limit(limit).execute)
        return resp.data or []
    except Exception as exc:
        print(f"[SUPABASE ERROR] Fetch recent calls failed: {exc}")
        return []


async def db_export_calls():
    client = get_supabase()
    if not client:
        return []
    try:
        resp = await _run_sync(client.table("sales_calls").select("call_sid,phone_number,caller_name,practice_name,status,created_at,completed_at,transcript,analysis,recording_url").order("created_at", desc=True).execute)
        return resp.data or []
    except Exception as exc:
        print(f"[SUPABASE ERROR] Export calls query failed: {exc}")
        return []


async def db_get_call(call_sid: str):
    client = get_supabase()
    if not client:
        return None
    try:
        resp = await _run_sync(client.table("sales_calls").select("phone_number,caller_name,practice_name,transcript,analysis,created_at").eq("call_sid", call_sid).limit(1).execute)
        return (resp.data or [None])[0]
    except Exception as exc:
        print(f"[SUPABASE ERROR] Fetch call failed: {exc}")
        return None


def _map_deal_stage(conversion_likelihood: str) -> str:
    mapping = {
        "high": HUBSPOT_STAGE_HIGH,
        "medium": HUBSPOT_STAGE_MEDIUM,
        "low": HUBSPOT_STAGE_LOW,
        "none": HUBSPOT_STAGE_NONE,
    }
    return mapping.get((conversion_likelihood or "").lower(), HUBSPOT_STAGE_NONE)


def create_or_update_hubspot_contact(phone_number: str, caller_name: str, practice_name: str, analysis: dict, recording_url: str | None) -> str | None:
    if not hubspot_client:
        print("[HUBSPOT] HUBSPOT_API_KEY not set. Skipping contact sync.")
        return None
    print(f"[HUBSPOT] Upserting contact for phone={phone_number}")
    props = {
        "phone": phone_number,
        "firstname": caller_name or "Unknown",
        "company": practice_name or analysis.get("practice_name", "Unknown"),
        "practice_type": analysis.get("practice_type", "Unknown"),
        "conversion_likelihood": analysis.get("conversion_likelihood", "unknown"),
        "pain_points": ", ".join(analysis.get("pain_points", [])),
        "objections": ", ".join(analysis.get("objections", [])),
        "value_props_resonated": ", ".join(analysis.get("value_props_resonated", [])),
        "next_steps": analysis.get("next_steps", ""),
        "last_call_date": datetime.utcnow().isoformat(),
        "called_by": caller_name or "",
        "call_recording_url": recording_url or "",
    }
    try:
        search = PublicObjectSearchRequest(
            filter_groups=[{"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_number}]}],
            properties=["phone"],
            limit=1,
        )
        result = hubspot_client.crm.contacts.search_api.do_search(public_object_search_request=search)
        if result.results:
            contact_id = result.results[0].id
            hubspot_client.crm.contacts.basic_api.update(contact_id, simple_public_object_input=SimplePublicObjectInput(properties=props))
            print(f"[HUBSPOT] Updated contact {contact_id}")
            return contact_id

        created = hubspot_client.crm.contacts.basic_api.create(simple_public_object_input=SimplePublicObjectInput(properties=props))
        print(f"[HUBSPOT] Created contact {created.id}")
        return created.id
    except Exception as exc:
        print(f"[HUBSPOT ERROR] create_or_update_hubspot_contact failed: {exc}")
        return None


def add_contact_to_sales_pipeline(contact_id: str, phone_number: str, analysis: dict):
    if not hubspot_client or not contact_id:
        return
    stage = _map_deal_stage(analysis.get("conversion_likelihood", "none"))
    print(f"[HUBSPOT] Creating deal for contact={contact_id} stage={stage}")
    try:
        deal_props = {
            "dealname": f"Sales Lead - {phone_number}",
            "pipeline": HUBSPOT_PIPELINE_ID,
            "dealstage": stage,
        }
        deal = hubspot_client.crm.deals.basic_api.create(simple_public_object_input=SimplePublicObjectInput(properties=deal_props))
        assoc_spec = [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]
        hubspot_client.crm.associations.v4.basic_api.create("contacts", contact_id, "deals", deal.id, association_spec=assoc_spec)
        print(f"[HUBSPOT] Deal {deal.id} created and associated")
    except Exception as exc:
        print(f"[HUBSPOT ERROR] add_contact_to_sales_pipeline failed: {exc}")


def add_hubspot_note(contact_id: str, transcript: str, analysis: dict, recording_url: str | None):
    if not hubspot_client or not contact_id:
        return
    note_body = f"""Call summary: {analysis.get('summary', '')}
Practice type: {analysis.get('practice_type', 'Unknown')}
Pain points: {', '.join(analysis.get('pain_points', []))}
Objections: {', '.join(analysis.get('objections', []))}
Next steps: {analysis.get('next_steps', '')}
Recording: {recording_url or 'N/A'}
Transcript: {transcript[:3000]}"""
    print(f"[HUBSPOT] Adding note for contact={contact_id}")
    try:
        note = hubspot_client.crm.objects.notes.basic_api.create(
            simple_public_object_input=SimplePublicObjectInput(properties={
                "hs_timestamp": datetime.utcnow().isoformat(),
                "hs_note_body": note_body,
            })
        )
        assoc_spec = [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]
        hubspot_client.crm.associations.v4.basic_api.create("notes", note.id, "contacts", contact_id, association_spec=assoc_spec)
        print(f"[HUBSPOT] Note {note.id} created")
    except Exception as exc:
        print(f"[HUBSPOT ERROR] add_hubspot_note failed: {exc}")


async def sync_hubspot(call_payload: dict, transcript: str, analysis: dict):
    if not HUBSPOT_API_KEY:
        print("[HUBSPOT] API key not configured, skipping sync")
        return
    try:
        contact_id = await _run_sync(
            create_or_update_hubspot_contact,
            call_payload.get("phone_number", ""),
            call_payload.get("caller_name", ""),
            call_payload.get("practice_name", ""),
            analysis,
            call_payload.get("recording_url"),
        )
        if contact_id:
            await _run_sync(add_contact_to_sales_pipeline, contact_id, call_payload.get("phone_number", ""), analysis)
            await _run_sync(add_hubspot_note, contact_id, transcript, analysis, call_payload.get("recording_url"))
    except Exception as exc:
        print(f"[HUBSPOT ERROR] sync_hubspot failed: {exc}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(lifespan=lifespan)

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "ACfa1aad34f3b4f8bb5f928c001e47ec65")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = "+14694454221"

# User phone numbers - map caller names to their phones
USER_PHONE_NUMBERS = {
    "Sanjana": "+12145188667",
    "Carolina": "+17865434900",
    "Pranjal": "+12145188667"
}

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "0426dca9f08f7c1d1621900e9f87cbd1c444f263")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
HUBSPOT_PIPELINE_ID = os.getenv("HUBSPOT_PIPELINE_ID", "default")
HUBSPOT_STAGE_HIGH = os.getenv("HUBSPOT_STAGE_HIGH", "appointment_scheduled")
HUBSPOT_STAGE_MEDIUM = os.getenv("HUBSPOT_STAGE_MEDIUM", "qualified_to_buy")
HUBSPOT_STAGE_LOW = os.getenv("HUBSPOT_STAGE_LOW", "presentation_scheduled")
HUBSPOT_STAGE_NONE = os.getenv("HUBSPOT_STAGE_NONE", "lead_status_open")

supabase: SupabaseClient | None = None
openai_client: OpenAI | None = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
hubspot_client: HubSpot | None = HubSpot(access_token=HUBSPOT_API_KEY) if HUBSPOT_API_KEY else None

# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
deepgram = DeepgramClient(DEEPGRAM_API_KEY)

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Opus B2B Call Recorder</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            .container {
                max-width: 600px;
                margin: 0 auto;
                background: white;
                border-radius: 20px;
                padding: 30px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            }
            h1 {
                color: #333;
                margin-bottom: 10px;
                font-size: 28px;
            }
            .subtitle {
                color: #666;
                margin-bottom: 30px;
                font-size: 14px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-bottom: 8px;
                color: #333;
                font-weight: 600;
                font-size: 14px;
            }
            input, select {
                width: 100%;
                padding: 12px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                font-size: 16px;
                transition: border 0.3s;
            }
            input:focus, select:focus {
                outline: none;
                border-color: #667eea;
            }
            button {
                width: 100%;
                padding: 15px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 18px;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.2s;
            }
            button:hover {
                transform: translateY(-2px);
            }
            button:active {
                transform: translateY(0);
            }
            #status {
                margin-top: 20px;
                padding: 15px;
                border-radius: 10px;
                display: none;
            }
            .status-success {
                background: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            .status-error {
                background: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
            .call-history {
                margin-top: 40px;
            }
            .call-card {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 10px;
                margin-bottom: 15px;
                border-left: 4px solid #667eea;
            }
            .call-header {
                display: flex;
                justify-content: space-between;
                margin-bottom: 10px;
            }
            .call-phone {
                font-weight: 600;
                color: #333;
            }
            .call-status {
                font-size: 12px;
                padding: 4px 8px;
                border-radius: 5px;
                background: #667eea;
                color: white;
            }
            .call-details {
                font-size: 14px;
                color: #666;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📞 Opus B2B Call Recorder</h1>
            <p class="subtitle">Your phone rings first → Then connects to practice</p>
            
            <form id="callForm">
                <div class="form-group">
                    <label>Practice Phone Number (who you're calling)</label>
                    <input type="tel" id="phone" placeholder="+12145551234" required>
                </div>
                
                <div class="form-group">
                    <label>Who's Calling?</label>
                    <select id="caller" required>
                        <option value="Sanjana">Sanjana (+1-214-518-8667)</option>
                        <option value="Carolina">Carolina (+1-786-543-4900)</option>
                        <option value="Pranjal">Pranjal</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Practice Name (optional)</label>
                    <input type="text" id="practice_name" placeholder="e.g., Dallas Dental Care">
                </div>
                
                <button type="submit">🚀 Start Call</button>
            </form>
            
            <div id="status"></div>
            
            <div class="call-history">
                <h2 style="margin-bottom: 20px; color: #333;">Recent Calls</h2>
                <div id="call_history">Loading...</div>
            </div>
        </div>
        
        <script>
            async function loadCallHistory() {
                const response = await fetch('/calls/recent');
                const calls = await response.json();
                
                const historyDiv = document.getElementById('call_history');
                if (calls.length === 0) {
                    historyDiv.innerHTML = '<p style="color: #999;">No calls yet. Make your first call!</p>';
                    return;
                }
                
                historyDiv.innerHTML = calls.map(call => `
                    <div class="call-card">
                        <div class="call-header">
                            <span class="call-phone">${call.phone_number}</span>
                            <span class="call-status">${call.status}</span>
                        </div>
                        <div class="call-details">
                            ${call.caller_name} • ${new Date(call.created_at).toLocaleString()}
                        </div>
                        ${call.transcript ? `
                            <div style="margin-top: 10px;">
                                <a href="/call/${call.call_sid}" style="color: #667eea; text-decoration: none;">
                                    View Transcript & Analysis →
                                </a>
                            </div>
                        ` : ''}
                    </div>
                `).join('');
            }
            
            document.getElementById('callForm').onsubmit = async (e) => {
                e.preventDefault();
                
                const statusDiv = document.getElementById('status');
                const caller = document.getElementById('caller').value;
                const phoneNumbers = {
                    'Sanjana': '+1-214-518-8667',
                    'Carolina': '+1-786-543-4900',
                    'Pranjal': '+1-214-518-8667'
                };
                
                statusDiv.style.display = 'block';
                statusDiv.className = '';
                statusDiv.innerHTML = `⏳ Initiating call... ${caller}'s phone (${phoneNumbers[caller]}) will ring first!`;
                
                try {
                    const response = await fetch('/start-call', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            phone_number: document.getElementById('phone').value,
                            caller_name: caller,
                            practice_name: document.getElementById('practice_name').value
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (response.ok) {
                        statusDiv.className = 'status-success';
                        statusDiv.innerHTML = `
                            ✅ ${caller}'s phone (${phoneNumbers[caller]}) is ringing now!<br>
                            Answer it, then you'll be connected to the practice.<br>
                            <small>Call ID: ${data.call_sid}</small>
                        `;
                        
                        document.getElementById('phone').value = '';
                        document.getElementById('practice_name').value = '';
                        
                        setTimeout(loadCallHistory, 2000);
                    } else {
                        throw new Error(data.detail || 'Failed to start call');
                    }
                } catch (error) {
                    statusDiv.className = 'status-error';
                    statusDiv.innerHTML = `❌ Error: ${error.message}`;
                }
            };
            
            loadCallHistory();
            setInterval(loadCallHistory, 30000);
        </script>
    </body>
    </html>
    """

@app.post("/start-call")
async def start_call(request: Request):
    data = await request.json()
    practice_number = data.get("phone_number")  # The practice you're calling
    caller_name = data.get("caller_name")
    practice_name = data.get("practice_name", "")
    
    # Get the caller's phone number based on who's calling
    user_phone = USER_PHONE_NUMBERS.get(caller_name, "+12145188667")  # Default to Sanjana
    
    base_url = str(request.base_url).rstrip('/')
    
    # URL encode the practice number to ensure it passes correctly
    from urllib.parse import quote
    encoded_practice_number = quote(practice_number, safe='')
    
    print(f"[START-CALL] Caller: {caller_name} ({user_phone})")
    print(f"[START-CALL] Practice number: {practice_number}")
    print(f"[START-CALL] Encoded: {encoded_practice_number}")
    
    try:
        # Call the selected person's phone first, then connect to practice
        call = twilio_client.calls.create(
            to=user_phone,  # Call the selected person's phone!
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base_url}/voice?practice_number={encoded_practice_number}",  # Pass practice number as parameter
            status_callback=f"{base_url}/call-status",
            status_callback_event=['completed'],
            record=True,
            recording_status_callback=f"{base_url}/recording-ready"
        )
        
        print(f"[START-CALL] Call created: {call.sid}")
        
        await db_insert_call(call.sid, practice_number, caller_name, practice_name)
        return {"call_sid": call.sid, "status": "initiated"}
    
    except Exception as e:
        print(f"[START-CALL ERROR] {str(e)}")
        return {"error": str(e)}, 500

@app.post("/voice")
@app.get("/voice")
async def voice(request: Request):
    """TwiML instructions: You answer, then dial the practice"""
    # Get the practice number from query params
    practice_number = request.query_params.get('practice_number', '')
    
    print(f"[VOICE] Practice number received: {practice_number}")
    
    response = VoiceResponse()
    
    if not practice_number:
        response.say("Error: No practice number provided. Please try again.", voice='Polly.Joanna')
        print("[VOICE ERROR] No practice number in query params")
    else:
        response.say("Connecting you to the practice now.", voice='Polly.Joanna')
        response.dial(practice_number)
        print(f"[VOICE] Dialing {practice_number}")
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/call-status")
@app.get("/call-status")
async def call_status(request: Request):
    # Handle both GET (query params) and POST (form data)
    if request.method == "GET":
        call_sid = request.query_params.get("CallSid")
        status = request.query_params.get("CallStatus")
    else:
        form = await request.form()
        call_sid = form.get("CallSid")
        status = form.get("CallStatus")
    
    await db_update_call_status(call_sid, status)
    return {"status": "updated"}

@app.post("/recording-ready")
@app.get("/recording-ready")
async def recording_ready(request: Request):
    if request.method == "GET":
        recording_sid = request.query_params.get("RecordingSid")
        call_sid = request.query_params.get("CallSid")
        recording_url = request.query_params.get("RecordingUrl")
    else:
        form = await request.form()
        recording_sid = form.get("RecordingSid")
        call_sid = form.get("CallSid")
        recording_url = form.get("RecordingUrl")

    print(f"[RECORDING-READY] Call: {call_sid}, Recording: {recording_sid}")
    full_url = f"https://api.twilio.com{recording_url}.mp3" if recording_url and not recording_url.startswith("http") else f"{recording_url}.mp3"

    try:
        async with httpx.AsyncClient() as client:
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            recording_response = await client.get(full_url, auth=auth)
            recording_response.raise_for_status()
            audio_data = recording_response.content

        print(f"[RECORDING-READY] Downloaded {len(audio_data)} bytes")
        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
            diarize=True,
            punctuate=True,
            utterances=True,
        )
        response = deepgram.listen.rest.v("1").transcribe_file(
            {"buffer": audio_data, "mimetype": "audio/mp3"},
            options,
        )

        plain_transcript = response["results"]["channels"][0]["alternatives"][0]["transcript"]
        utterances = response.get("results", {}).get("utterances", [])
        if utterances:
            print("[RECORDING-READY] Using Deepgram utterances diarization")
            lines = []
            for utt in utterances:
                speaker_num = utt.get("speaker", 0)
                text = utt.get("transcript", "")
                speaker_label = "You" if speaker_num == 0 else "Practice"
                lines.append(f"**{speaker_label}:** {text}")
            transcript = "\n\n".join(lines)
        else:
            print("[RECORDING-READY] Utterances unavailable, falling back to plain transcript")
            transcript = plain_transcript

        analysis = analyze_sales_call(plain_transcript)
        await db_complete_call(call_sid, transcript, analysis, recording_url)
        print(f"[RECORDING-READY] Database updated for call {call_sid}")

        call_payload = await db_get_call(call_sid) or {}
        call_payload["recording_url"] = recording_url
        await sync_hubspot(call_payload, transcript, analysis)
    except Exception as e:
        print(f"[RECORDING-READY ERROR] {str(e)}")
        await db_set_error(call_sid)

    return {"status": "processed"}


def analyze_sales_call(transcript: str) -> dict:
    prompt = f"""Analyze this B2B sales call transcript for Opus Health (a healthcare payments platform that helps practices with HSA/FSA billing).

The call is between a sales representative from Opus Health and a dental/medical practice.

Transcript:
{transcript}

Carefully extract the following information. If something is not mentioned or unclear, use "Not mentioned" or "Unknown" rather than making assumptions:

Extract and return as JSON:
{{
    "practice_name": "The actual name of the practice if mentioned, or 'Not mentioned'",
    "practice_type": "dental/weight loss/medical/veterinary/other - determine from context",
    "pain_points": ["Specific problems or frustrations the practice mentioned about their current payment/billing process. Be specific. If none mentioned, use empty array"],
    "objections": ["Specific concerns, hesitations, or reasons for not moving forward that were explicitly stated. If none, use empty array"],
    "value_props_resonated": ["Which benefits of Opus Health seemed to interest them based on their responses. Be specific. If none clear, use empty array"],
    "next_steps": "Specific actions agreed upon (e.g., 'Send email with pricing', 'Schedule demo for next week', 'Call back in 2 days'). If none agreed, say 'No specific next steps agreed'",
    "conversion_likelihood": "high/medium/low/none - high if they committed to next steps or showed strong interest, medium if interested but cautious, low if polite but not interested, none if rejected",
    "key_quotes": ["1-3 notable things the practice representative said, word-for-word if possible. Focus on meaningful statements about their needs or interest level. If transcript is too short or unclear, use empty array"],
    "summary": "2-3 sentence summary: What was discussed, how did the practice respond, and what's the outcome/status"
}}

Be specific and accurate. Only include information that is actually present in the transcript."""

    fallback = {
        "practice_name": "Analysis error",
        "practice_type": "Unknown",
        "pain_points": [],
        "objections": [],
        "value_props_resonated": [],
        "next_steps": "Analysis failed",
        "conversion_likelihood": "unknown",
        "key_quotes": [],
        "summary": "Error analyzing call",
    }
    if not openai_client:
        fallback["summary"] = "OPENAI_API_KEY missing"
        return fallback

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4-turbo-preview",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        print(f"[ANALYSIS ERROR] {str(e)}")
        fallback["summary"] = f"Error analyzing call: {str(e)}"
        return fallback


@app.get("/calls/recent")
async def recent_calls():
    rows = await db_recent_calls(limit=10)
    calls = []
    for row in rows:
        calls.append({
            "call_sid": row.get("call_sid"),
            "phone_number": row.get("phone_number"),
            "caller_name": row.get("caller_name"),
            "practice_name": row.get("practice_name"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "transcript": bool(row.get("transcript")),
        })
    return calls


@app.get("/admin/export-csv")
async def export_csv():
    """Export all calls data as CSV - includes transcripts and analysis"""
    rows = await db_export_calls()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Call ID', 'Phone Number', 'Caller', 'Practice Name',
        'Status', 'Created At', 'Completed At', 'Transcript',
        'Practice Type', 'Pain Points', 'Objections',
        'Value Props Resonated', 'Next Steps', 'Conversion Likelihood',
        'Key Quotes', 'Summary', 'Recording URL'
    ])

    for row in rows:
        analysis = _safe_json_parse(row.get("analysis"))
        writer.writerow([
            row.get("call_sid", ""),
            row.get("phone_number", ""),
            row.get("caller_name", ""),
            row.get("practice_name", ""),
            row.get("status", ""),
            row.get("created_at", ""),
            row.get("completed_at", ""),
            row.get("transcript", ""),
            analysis.get('practice_type', ''),
            '; '.join(analysis.get('pain_points', [])),
            '; '.join(analysis.get('objections', [])),
            '; '.join(analysis.get('value_props_resonated', [])),
            analysis.get('next_steps', ''),
            analysis.get('conversion_likelihood', ''),
            '; '.join(analysis.get('key_quotes', [])),
            analysis.get('summary', ''),
            row.get('recording_url', ''),
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=opus_calls_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        }
    )


@app.get("/call/{call_sid}", response_class=HTMLResponse)
async def view_call(call_sid: str):
    row = await db_get_call(call_sid)
    if not row:
        return "<h1>Call not found</h1>"

    phone = row.get("phone_number")
    caller = row.get("caller_name")
    practice = row.get("practice_name")
    transcript = row.get("transcript")
    analysis = _safe_json_parse(row.get("analysis"))
    created = row.get("created_at")
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Call Details - {phone}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #f5f5f5;
                padding: 20px;
                margin: 0;
            }}
            .container {{
                max-width: 900px;
                margin: 0 auto;
                background: white;
                border-radius: 10px;
                padding: 30px;
            }}
            h1 {{ color: #333; margin-bottom: 5px; }}
            .meta {{ color: #666; margin-bottom: 30px; }}
            .section {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
            }}
            .section h2 {{
                margin-top: 0;
                color: #667eea;
            }}
            .badge {{
                display: inline-block;
                padding: 5px 10px;
                border-radius: 5px;
                font-size: 12px;
                font-weight: 600;
                margin-right: 10px;
            }}
            .high {{ background: #d4edda; color: #155724; }}
            .medium {{ background: #fff3cd; color: #856404; }}
            .low {{ background: #f8d7da; color: #721c24; }}
            ul {{ line-height: 1.8; }}
            .transcript {{
                white-space: pre-wrap;
                line-height: 1.8;
                color: #333;
            }}
            .back {{
                display: inline-block;
                margin-bottom: 20px;
                color: #667eea;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/" class="back">← Back to Dashboard</a>
            <h1>Call: {phone}</h1>
            <div class="meta">
                By {caller} • {practice or 'Unknown Practice'} • {created}
            </div>
            
            <div class="section">
                <h2>📊 AI Analysis</h2>
                <p><strong>Practice:</strong> {analysis.get('practice_name', 'Unknown')}</p>
                <p><strong>Type:</strong> {analysis.get('practice_type', 'Unknown')}</p>
                <p><strong>Conversion Likelihood:</strong> 
                    <span class="badge {analysis.get('conversion_likelihood', 'low')}">
                        {analysis.get('conversion_likelihood', 'Unknown').upper()}
                    </span>
                </p>
                <p><strong>Summary:</strong> {analysis.get('summary', 'No summary available')}</p>
            </div>
            
            <div class="section">
                <h2>💬 Key Quotes</h2>
                <ul>
                    {''.join(f'<li>{quote}</li>' for quote in analysis.get('key_quotes', []))}
                </ul>
            </div>
            
            <div class="section">
                <h2>😣 Pain Points</h2>
                <ul>
                    {''.join(f'<li>{pain}</li>' for pain in analysis.get('pain_points', []))}
                </ul>
            </div>
            
            <div class="section">
                <h2>🚫 Objections</h2>
                <ul>
                    {''.join(f'<li>{obj}</li>' for obj in analysis.get('objections', []))}
                </ul>
            </div>
            
            <div class="section">
                <h2>✅ Value Props That Resonated</h2>
                <ul>
                    {''.join(f'<li>{vp}</li>' for vp in analysis.get('value_props_resonated', []))}
                </ul>
            </div>
            
            <div class="section">
                <h2>📝 Next Steps</h2>
                <p>{analysis.get('next_steps', 'None specified')}</p>
            </div>
            
            <div class="section">
                <h2>📄 Full Transcript</h2>
                <div class="transcript">{transcript or 'Transcript not available yet'}</div>
            </div>
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
