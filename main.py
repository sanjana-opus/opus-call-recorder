from fastapi import FastAPI, Request, Form
from fastapi.responses import Response, HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
import openai
from supabase import create_client, Client as SupabaseClient
import requests as hs_requests
import httpx
import json
import os
import re
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from deepgram import DeepgramClient, PrerecordedOptions
import csv
from io import StringIO

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", os.getenv("TWILIO_ACCOUNT_SID"))
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = "+14694454221"

USER_PHONE_NUMBERS = {
    "Sanjana": "+12145188667",
    "Carolina": "+17865434900",
    "Matilde": "+13054271554"
}

# API Keys
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
LGM_API_KEY     = os.getenv("LGM_API_KEY")
LGM_AUDIENCE_ID = os.getenv("LGM_AUDIENCE_ID")
HUNTER_API_KEY  = os.getenv("HUNTER_API_KEY")

# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
deepgram = DeepgramClient(DEEPGRAM_API_KEY)
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
def hs(method, path, **kwargs):
    """Direct HubSpot REST API call — no SDK needed."""
    url = f"https://api.hubapi.com{path}"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}
    resp = hs_requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}

hubspot_client = bool(HUBSPOT_API_KEY)  # True/False flag for backward compat

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        supabase.table('sales_calls').select("id").limit(1).execute()
        print("[STARTUP] ✅ Supabase connected")
    except Exception as e:
        print(f"[STARTUP] ⚠️ Supabase connection failed: {e}")
    
    if HUBSPOT_API_KEY:
        try:
            hs("GET", "/crm/v3/objects/contacts?limit=1")
            print("[STARTUP] ✅ HubSpot connected")
            ensure_hubspot_custom_properties()
        except Exception as e:
            print(f"[STARTUP] ⚠️ HubSpot connection failed: {e}")

    # Start background auto-send checker (fires every 60s)
    import asyncio
    task = asyncio.create_task(auto_send_checker())
    print("[STARTUP] ✅ Auto-send checker running (5-min review window)")

    yield

    task.cancel()

app = FastAPI(lifespan=lifespan)

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
            input,  {
                width: 100%;
                padding: 12px;
                border: 2px solid #e0e0e0;
                border-radius: 10px;
                font-size: 16px;
                transition: border 0.3s;
            }
            input:focus, :focus {
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
                margin-bottom: 10px;
            }
            button:hover {
                transform: translateY(-2px);
            }
            button:active {
                transform: translateY(0);
            }
            .export-btn {
                background: linear-gradient(135deg, #43cea2 0%, #185a9d 100%);
                font-size: 16px;
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
                        <option value="Matilde">Matilde (+1-305-427-1554)</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Practice Name (optional)</label>
                    <input type="text" id="practice_name" placeholder="e.g., Dallas Dental Care">
                </div>
                
                <button type="submit">🚀 Start Call</button>
            </form>
            
            <button class="export-btn" onclick="window.open('/admin/export-csv', '_blank')">
                📥 Download All Calls (CSV)
            </button>
            
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
                
                historyDiv.innerHTML = calls.map(call => {
                    const statusColors = {
                        'pending_review': '#e67e00',
                        'confirmed': '#28a745',
                        'sent': '#28a745',
                        'discarded': '#6c757d',
                        'completed': '#667eea',
                        'initiated': '#aaa',
                        'error': '#dc3545'
                    };
                    const statusColor = statusColors[call.status] || '#667eea';
                    const isPending = call.status === 'pending_review';
                    return `
                    <div class="call-card" style="${isPending ? 'border-left:4px solid #e67e00;' : ''}">
                        <div class="call-header">
                            <span class="call-phone">${call.phone_number}</span>
                            <span class="call-status" style="background:${statusColor}">${isPending ? '⏳ Review Now' : call.status}</span>
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
                `;
                }).join('');
            }
            
            document.getElementById('callForm').onsubmit = async (e) => {
                e.preventDefault();
                
                const statusDiv = document.getElementById('status');
                const caller = document.getElementById('caller').value;
                const phoneNumbers = {
                    'Sanjana': '+1-214-518-8667',
                    'Carolina': '+1-786-543-4900'
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
    practice_number = data.get("phone_number")
    caller_name = data.get("caller_name")
    practice_name = data.get("practice_name", "")
    
    user_phone = USER_PHONE_NUMBERS.get(caller_name, "+12145188667")
    base_url = str(request.base_url).rstrip('/')
    
    from urllib.parse import quote
    encoded_practice_number = quote(practice_number, safe='')
    
    print(f"[START-CALL] Caller: {caller_name} ({user_phone})")
    print(f"[START-CALL] Practice number: {practice_number}")
    
    try:
        call = twilio_client.calls.create(
            to=user_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base_url}/voice?practice_number={encoded_practice_number}",
            status_callback=f"{base_url}/call-status",
            status_callback_event=['completed'],
            record=True,
            recording_channels="dual",            # separate channel per leg: 0=rep, 1=practice
            recording_status_callback=f"{base_url}/recording-ready"
        )
        
        supabase.table('sales_calls').insert({
            "call_sid": call.sid,
            "phone_number": practice_number,
            "caller_name": caller_name,
            "practice_name": practice_name,
            "status": "initiated"
        }).execute()
        
        print(f"[SUPABASE] ✅ Call {call.sid} stored")
        
        return {"call_sid": call.sid, "status": "initiated"}
    
    except Exception as e:
        print(f"[START-CALL ERROR] {str(e)}")
        return {"error": str(e)}, 500

@app.post("/voice")
@app.get("/voice")
async def voice(request: Request):
    practice_number = request.query_params.get('practice_number', '')
    
    print(f"[VOICE] Practice number received: {practice_number}")
    
    response = VoiceResponse()
    
    if not practice_number:
        response.say("Error: No practice number provided.", voice='Polly.Joanna')
    else:
        response.say("Connecting you to the practice now.", voice='Polly.Joanna')
        response.dial(practice_number)
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/call-status")
@app.get("/call-status")
async def call_status(request: Request):
    if request.method == "GET":
        call_sid = request.query_params.get("CallSid")
        status = request.query_params.get("CallStatus")
    else:
        form = await request.form()
        call_sid = form.get("CallSid")
        status = form.get("CallStatus")
    
    try:
        supabase.table('sales_calls').update({
            "status": status
        }).eq('call_sid', call_sid).execute()
        print(f"[SUPABASE] ✅ Updated status for {call_sid}: {status}")
    except Exception as e:
        print(f"[SUPABASE] ❌ Error updating status: {e}")
    
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
    
    # Handle both relative and absolute URLs from Twilio
    if recording_url.startswith("http"):
        full_recording_url = recording_url
    else:
        full_recording_url = f"https://api.twilio.com{recording_url}"
    
    download_url = f"{full_recording_url}.mp3"
    
    print(f"[RECORDING-READY] Downloading from: {download_url}")
    
    async with httpx.AsyncClient() as client:
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        recording_response = await client.get(download_url, auth=auth)
        audio_data = recording_response.content
    
    print(f"[RECORDING-READY] Downloaded {len(audio_data)} bytes")
    
    try:
        print("[RECORDING-READY] Starting Deepgram transcription with diarization...")
        
        options = PrerecordedOptions(
            model="nova-3",
            smart_format=True,
            multichannel=True,   # dual-channel: channel 0 = rep, channel 1 = practice
            punctuate=True,
            utterances=True,
        )
        
        response = deepgram.listen.rest.v("1").transcribe_file(
            {"buffer": audio_data, "mimetype": "audio/mp3"},
            options
        )
        
        response_dict = response.to_dict()

        # ── Dual-channel transcript assembly ────────────────────────────────
        # Twilio dual-channel: channel 0 = outbound leg (rep), channel 1 = inbound leg (practice).
        # Each channel has its own alternatives list. We interleave by word start_time
        # so the final transcript reads in chronological order.
        channels = response_dict.get("results", {}).get("channels", [])

        # Build plain transcript from channel 0 for OpenAI analysis (rep+practice merged)
        all_words_plain = []
        for ch_idx, ch in enumerate(channels):
            alts = ch.get("alternatives", [])
            if alts:
                for w in alts[0].get("words", []):
                    all_words_plain.append((w.get("start", 0), w.get("word", "")))
        all_words_plain.sort(key=lambda x: x[0])
        plain_transcript = " ".join(w for _, w in all_words_plain)

        # Build diarized transcript by interleaving utterances from both channels
        # Each utterance tagged with its channel so speaker label is deterministic
        all_utterances = []
        for ch_idx, ch in enumerate(channels):
            alts = ch.get("alternatives", [])
            if alts:
                for utt in alts[0].get("paragraphs", {}).get("paragraphs", []):
                    for sentence in utt.get("sentences", []):
                        all_utterances.append({
                            "channel": ch_idx,
                            "start": sentence.get("start", 0),
                            "text": sentence.get("text", "").strip()
                        })

        # Fallback: if paragraphs/sentences not available, use top-level words grouped by channel
        if not all_utterances:
            for ch_idx, ch in enumerate(channels):
                alts = ch.get("alternatives", [])
                if alts and alts[0].get("transcript"):
                    # Use the whole channel transcript as one block, timed at 0 offset per channel
                    # We'll interleave by checking utterances from response root
                    pass

            # Last resort: use root-level utterances with channel field
            root_utterances = response_dict.get("results", {}).get("utterances", [])
            for utt in root_utterances:
                all_utterances.append({
                    "channel": utt.get("channel", 0),
                    "start": utt.get("start", 0),
                    "text": utt.get("transcript", "").strip()
                })

        # Sort all utterances chronologically
        all_utterances.sort(key=lambda x: x["start"])

        # Filter out Twilio system audio — whisper + any very short noise utterances
        WHISPER_PHRASES = ["connecting you to the practice", "connecting you now", "please hold"]
        all_utterances = [
            u for u in all_utterances
            if len(u["text"].strip()) > 2  # drop single-word noise like "Okay." at t=0
            and not any(w in u["text"].lower() for w in WHISPER_PHRASES)
        ]

        # Merge consecutive utterances from same channel that are within 1.5s of each other
        # This groups short back-to-back sentences into natural speaking turns
        merged = []
        for utt in all_utterances:
            if (merged
                and merged[-1]["channel"] == utt["channel"]
                and (utt["start"] - merged[-1]["end"]) < 1.5):
                merged[-1]["text"] += " " + utt["text"]
                merged[-1]["end"]   = utt.get("end", utt["start"])
            else:
                merged.append({
                    "channel": utt["channel"],
                    "start":   utt["start"],
                    "end":     utt.get("end", utt["start"]),
                    "text":    utt["text"]
                })
        all_utterances = merged

        if all_utterances:
            formatted_lines = []
            for utt in all_utterances:
                text = utt["text"].strip()
                if not text:
                    continue
                # Channel 0 = rep (outbound), Channel 1 = practice (inbound)
                speaker_label = "Rep:" if utt["channel"] == 0 else "Practice:"
                formatted_lines.append(f"{speaker_label} {text}")
            transcript = "\n".join(formatted_lines)
            rep_count = sum(1 for u in all_utterances if u["channel"] == 0)
            prac_count = sum(1 for u in all_utterances if u["channel"] == 1)
            print(f"[RECORDING-READY] ✅ Dual-channel diarization: {rep_count} rep turns, {prac_count} practice turns (after merge)")
        else:
            transcript = plain_transcript
            print(f"[RECORDING-READY] ⚠️ No utterances found, using plain transcript")

        
        print(f"[RECORDING-READY] Transcript received: {len(transcript)} characters")
        
        print("[RECORDING-READY] Starting OpenAI analysis...")
        analysis = analyze_sales_call(plain_transcript)
        
        print("[RECORDING-READY] Analysis complete")
        
        try:
            storage_path = f"recordings/{call_sid}.mp3"
            supabase.storage.from_('call-recordings').upload(
                storage_path,
                audio_data,
                {"content-type": "audio/mpeg"}
            )
            
            storage_url = supabase.storage.from_('call-recordings').get_public_url(storage_path)
            print(f"[SUPABASE] ✅ Recording uploaded: {storage_url}")
            final_recording_url = storage_url
        except Exception as e:
            print(f"[SUPABASE] ⚠️ Storage upload failed: {e}, using Twilio URL")
            final_recording_url = full_recording_url
        
        # Save as pending_review — dashboard shows review card, auto-sends after 5 min
        auto_send_at = (datetime.now() + timedelta(minutes=5)).isoformat()
        supabase.table('sales_calls').update({
            "transcript": transcript,
            "analysis": analysis,
            "recording_url": final_recording_url,
            "status": "pending_review",
            "completed_at": datetime.now().isoformat(),
            "auto_send_at": auto_send_at
        }).eq('call_sid', call_sid).execute()
        
        print(f"[REVIEW] ⏳ Call {call_sid} pending review — auto-sends at {auto_send_at}")
        
    except Exception as e:
        print(f"[RECORDING-READY ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        
        try:
            supabase.table('sales_calls').update({"status": "error"}).eq('call_sid', call_sid).execute()
        except:
            pass
    
    return {"status": "processed"}

def push_to_hubspot_lgm(call_sid: str, analysis_override: dict = None):
    """
    Shared by: confirm endpoint (manual) + auto-send background task (5 min timeout).
    Accepts an optional analysis_override so edited fields from the review card are used.
    """
    try:
        call_data = supabase.table('sales_calls').select("*").eq('call_sid', call_sid).execute()
        if not call_data.data:
            print(f"[PUSH] Call {call_sid} not found")
            return

        call_record = call_data.data[0]
        # Don't double-send if already confirmed or sent
        if call_record.get('status') in ("confirmed", "sent", "error"):
            print(f"[PUSH] Skipping {call_sid} — already {call_record.get('status')}")
            return

        phone_number  = call_record.get('phone_number')
        caller_name   = call_record.get('caller_name')
        practice_name = call_record.get('practice_name')
        final_recording_url = call_record.get('recording_url', '')

        # Use override if provided (edited from review card), else use stored analysis
        raw_analysis = call_record.get('analysis') or {}
        analysis = analysis_override if analysis_override else (
            raw_analysis if isinstance(raw_analysis, dict) else json.loads(raw_analysis or '{}')
        )

        # Mark as confirmed immediately to prevent double-send race
        supabase.table('sales_calls').update({"status": "confirmed"}).eq('call_sid', call_sid).execute()

        practice_name = practice_name or analysis.get('practice_name', 'Unknown')

        if hubspot_client and phone_number:
            print(f"[PUSH] Starting HubSpot sync for {call_sid}...")
            hubspot_result = create_or_update_hubspot_contact(
                phone_number=phone_number,
                practice_name=practice_name,
                caller_name=caller_name,
                analysis=analysis,
                call_sid=call_sid,
                recording_url=final_recording_url
            )

            if hubspot_result.get("action") in ["created", "updated"]:
                contact_id = hubspot_result["contact_id"]
                add_contact_to_sales_pipeline(contact_id, analysis)
                add_hubspot_note(contact_id, analysis, final_recording_url, call_sid)
                if analysis.get("conversion_likelihood") != "none":
                    # Use email already resolved during HubSpot sync — avoids phone format lookup failures
                    resolved_email = hubspot_result.get("email") or get_email_for_phone(phone_number)
                    enroll_in_lgm_audience(
                        contact_id=contact_id, email=resolved_email,
                        phone=phone_number, practice_name=practice_name, analysis=analysis
                    )
                else:
                    print(f"[PUSH] Skipping LGM — explicit hard NO")
                print(f"[PUSH] ✅ Complete for contact {contact_id}")

        supabase.table('sales_calls').update({"status": "sent"}).eq('call_sid', call_sid).execute()

    except Exception as e:
        print(f"[PUSH ERROR] {call_sid}: {e}")
        import traceback
        traceback.print_exc()


async def auto_send_checker():
    """Background task — runs every 60s, auto-sends any pending_review calls past their 5-min window."""
    import asyncio
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now().isoformat()
            result = supabase.table('sales_calls').select("call_sid, auto_send_at").eq(
                'status', 'pending_review'
            ).lte('auto_send_at', now).execute()
            for row in (result.data or []):
                print(f"[AUTO-SEND] ⏰ Auto-sending {row['call_sid']} (review window expired)")
                push_to_hubspot_lgm(row['call_sid'])
        except Exception as e:
            print(f"[AUTO-SEND ERROR] {e}")


def analyze_sales_call(transcript: str) -> dict:
    prompt = f"""You are analyzing a B2B outbound sales call for Opus Health — a healthcare payments platform that automates HSA/FSA billing for dental practices, med spas, and weight loss clinics.

The call is between an Opus Health sales rep and a front desk / office manager at a practice.

IMPORTANT: The transcript may have imperfect speaker labels due to automated diarization. Use context clues — the rep introduces themselves, mentions Opus Health, and pitches the product. The practice side answers the phone, gives their name, and responds to the pitch.

Transcript:
{transcript}

Extract the following. Be specific — use exact names, numbers, and phrases from the transcript. If something is genuinely not mentioned, use null (not "Not mentioned").

Return ONLY a valid JSON object with these exact fields:
{{
  "practice_name": "exact practice name said in call, or null",
  "contact_name": "first name of the person who answered, or null",
  "contact_title": "their title/role if mentioned, or null",
  "practice_type": "dental | medspa | weight_loss | other",
  "pain_points": ["array of specific problems or needs they mentioned"],
  "objections": ["array of reasons they hesitated or said no"],
  "value_props_resonated": ["which Opus Health benefits got a positive response"],
  "next_steps": "single string — exact next action e.g. 'Manager Hope calls back, ext 2001' or 'Send email to bearcreekfamilydentistry@yahoo.com'",
  "callback_name": "name of person to call back if mentioned, or null",
  "callback_extension": "phone extension if mentioned, or null",
  "email_mentioned": "email address spoken in the call if any, or null",
  "conversion_likelihood": "high (very interested, asked for pricing/demo/next steps) | medium (open, asked to email or call back, wants to discuss with manager) | low (soft brush-off: busy, in meeting, office manager unavailable, asked to send email — did NOT say no) | none (explicit hard NO only: said not interested, told us to stop calling, already happy with current solution, asked to be removed)",
  "key_quotes": ["1-3 direct quotes from the practice side only"],
  "summary": "2-3 sentences covering what happened, who was spoken to, and what was agreed"
}}"""
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a sales call analyzer. Always respond with valid JSON only, no other text."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=2000,
            temperature=0.3
        )
        
        response_text = response.choices[0].message.content
        
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            raise
            
    except Exception as e:
        print(f"[ANALYSIS ERROR] {str(e)}")
        return {
            "practice_name": "Analysis error",
            "practice_type": "Unknown",
            "pain_points": [],
            "objections": [],
            "value_props_resonated": [],
            "next_steps": "Analysis failed",
            "conversion_likelihood": "unknown",
            "key_quotes": [],
            "summary": f"Error analyzing call: {str(e)}"
        }

def get_email_for_phone(phone_number: str) -> str:
    """
    Look up email for this phone number.
    1. Check Supabase pending_leads (leads from Apify cron)
    2. Fallback: check HubSpot (covers manually-imported contacts like EQ Dental, Bear Creek)
    """
    try:
        digits = ''.join(c for c in phone_number if c.isdigit())
        result = supabase.table("pending_leads").select("email, email_valid").or_(
            f"phone.eq.{phone_number},phone.eq.+{digits},phone.eq.+1{digits[-10:]}"
        ).limit(1).execute()
        if result.data and result.data[0].get("email_valid"):
            return result.data[0]["email"]
    except Exception as e:
        print(f"[SUPABASE] Could not fetch email for {phone_number}: {e}")

    # Fallback: check HubSpot for contacts imported manually
    if HUBSPOT_API_KEY:
        try:
            search = hs("POST", "/crm/v3/objects/contacts/search", json={
                "filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_number}]}],
                "properties": ["email"]
            })
            results = search.get("results", [])
            if results and results[0].get("properties", {}).get("email"):
                email = results[0]["properties"]["email"]
                print(f"[HUBSPOT] Found email via fallback lookup: {email}")
                return email
        except Exception as e:
            print(f"[HUBSPOT] Email fallback lookup failed for {phone_number}: {e}")

    return ""


def enroll_in_lgm_audience(contact_id: str, email: str, phone: str, practice_name: str, analysis: dict):
    """
    1. Sets lgm_ready = true on HubSpot contact (for tracking)
    2. Directly pushes lead to LGM audience via API (no HubSpot workflow needed)
    """
    import requests as req

    likelihood    = analysis.get("conversion_likelihood", "low")
    practice_type = analysis.get("practice_type", "unknown")

    # Step 1: Update HubSpot contact
    if HUBSPOT_API_KEY and contact_id:
        try:
            hs("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": {
                "lgm_ready":             "true",   # matches HubSpot enum: "true"/"false"
                "lgm_enrolled_date":     datetime.now().strftime("%Y-%m-%d"),
                "last_call_disposition": likelihood,
                "practice_vertical":     practice_type,
                "call_next_steps":       str(analysis.get("next_steps", "")),
                "call_summary":          analysis.get("summary", ""),
            }})
            print(f"[LGM] HubSpot contact {contact_id} flagged lgm_ready=true")
        except Exception as e:
            print(f"[LGM] HubSpot update failed: {e}")

    # Step 2: Push directly to LGM audience via API
    if not LGM_API_KEY or not LGM_AUDIENCE_ID:
        print("[LGM] Skipping LGM push - LGM_API_KEY or LGM_AUDIENCE_ID not set")
        return

    # Build firstname: first word of contact name (e.g. "Hope" not "Hope Smith")
    # Fall back to practice name if no contact extracted, "there" as last resort
    contact_name = analysis.get("contact_name") or ""
    if contact_name and contact_name.lower() not in ("null", "unknown", ""):
        lgm_firstname = contact_name.split()[0]  # first name only
    else:
        lgm_firstname = practice_name or "there"
    lgm_lastname  = ""  # always blank — avoids "Hi Practice," in sequences

    lgm_payload = {
        "audience":    LGM_AUDIENCE_ID,
        "firstname":   lgm_firstname,
        "lastname":    lgm_lastname,
        "phone":       phone,
        "companyName": practice_name or "Unknown Practice",
    }
    if email and "@" in email:
        lgm_payload["proEmail"] = email  # LGM API field: proEmail (confirmed from CSV export)

    print(f"[LGM] Payload: {lgm_payload}")

    try:
        resp = req.post(
            "https://apiv2.lagrowthmachine.com/flow/leads",
            params={"apikey": LGM_API_KEY},
            json=lgm_payload,
            timeout=15
        )
        print(f"[LGM] Response {resp.status_code}: {resp.text}")
        data = resp.json()
        if resp.status_code == 200:
            print(f"[LGM] ✅ Lead pushed successfully - {data.get('message', 'OK')}")
        else:
            print(f"[LGM] ❌ Push failed: {resp.status_code} {data}")
    except Exception as e:
        print(f"[LGM] LGM API error: {e}")


def ensure_hubspot_custom_properties():
    """
    Idempotently creates all custom contact properties Opus Health needs.
    Safe to call on every deploy — HubSpot ignores duplicates (409 = already exists).
    """
    custom_props = [
        {"name": "contact_type", "label": "Contact Type", "type": "enumeration", "fieldType": "select",
         "options": [
             {"label": "Practice", "value": "Practice", "displayOrder": 0, "hidden": False},
             {"label": "Partner",  "value": "Partner",  "displayOrder": 1, "hidden": False},
             {"label": "Other",    "value": "Other",    "displayOrder": 2, "hidden": False},
         ]},
        {"name": "sales_lead_type", "label": "Sales - Lead Type", "type": "enumeration", "fieldType": "select",
         "options": [
             {"label": "Dental Practice",    "value": "Dental Practice",    "displayOrder": 0, "hidden": False},
             {"label": "Med Spa",            "value": "Med Spa",            "displayOrder": 1, "hidden": False},
             {"label": "Weight Loss Clinic", "value": "Weight Loss Clinic", "displayOrder": 2, "hidden": False},
             {"label": "Other",              "value": "Other",              "displayOrder": 3, "hidden": False},
         ]},
        {"name": "lgm_ready",             "label": "LGM Ready",            "type": "enumeration", "fieldType": "select",
         "options": [
             {"label": "Yes", "value": "true",  "displayOrder": 0, "hidden": False},
             {"label": "No",  "value": "false", "displayOrder": 1, "hidden": False},
         ]},
        {"name": "practice_vertical",     "label": "Practice Vertical",    "type": "string", "fieldType": "text"},
        {"name": "last_call_disposition", "label": "Last Call Disposition", "type": "string", "fieldType": "text"},
        {"name": "call_summary",          "label": "Call Summary",         "type": "string", "fieldType": "textarea"},
        {"name": "call_next_steps",       "label": "Call Next Steps",      "type": "string", "fieldType": "textarea"},
        {"name": "lgm_enrolled_date",     "label": "LGM Enrolled Date",    "type": "string", "fieldType": "text"},
    ]

    created, skipped, failed = [], [], []
    for prop in custom_props:
        payload = {
            "name":      prop["name"],
            "label":     prop["label"],
            "type":      prop["type"],
            "fieldType": prop["fieldType"],
            "groupName": "contactinformation",
        }
        if "options" in prop:
            payload["options"] = prop["options"]
        try:
            hs("POST", "/crm/v3/properties/contacts", json=payload)
            created.append(prop["name"])
        except Exception as e:
            if "409" in str(e) or "already exists" in str(e).lower():
                skipped.append(prop["name"])
            else:
                failed.append(f"{prop['name']}: {e}")

    print(f"[HUBSPOT] Properties — created: {created}, already existed: {skipped}, failed: {failed}")
    return {"created": created, "skipped": skipped, "failed": failed}


def create_or_update_hubspot_contact(phone_number: str, practice_name: str, caller_name: str, analysis: dict, call_sid: str, recording_url: str):
    if not HUBSPOT_API_KEY:
        print("[HUBSPOT] Skipping - no API key configured")
        return {"action": "skipped"}

    # Pull enriched email from Supabase pending_leads
    email = get_email_for_phone(phone_number)
    # Also check if OpenAI extracted an email from the call itself
    if not email:
        email = analysis.get("email_mentioned") or ""

    # Resolve best practice name: UI input > analysis extraction
    resolved_name = practice_name or ""
    analysis_name = analysis.get("practice_name") or ""
    if analysis_name and analysis_name not in ("Not mentioned", "Unknown", "null", ""):
        resolved_name = analysis_name

    # Build contact name from what was said in the call
    # If we have a real person's name from the call, use firstname only, blank lastname
    # If no contact name found, fall back to practice name in firstname, blank lastname
    raw_contact = analysis.get("contact_name") or ""
    # Use only the first word of the contact name (e.g. "Hope" not "Hope Smith")
    # Fall back to practice name if no contact name extracted
    # Never set lastname — avoids "Hi Practice," or "Hi Smith," in LGM sequences
    contact_first = raw_contact.split()[0] if raw_contact and raw_contact.lower() not in ("null", "unknown", "") else (resolved_name or "Unknown")
    contact_last  = ""

    # Map practice_type to our custom property values
    practice_type = analysis.get("practice_type", "other")

    # Map practice_type → sales_lead_type display label
    lead_type_map = {
        "dental":      "Dental Practice",
        "medspa":      "Med Spa",
        "weight_loss": "Weight Loss Clinic",
        "other":       "Other",
    }
    sales_lead_type = lead_type_map.get(practice_type, "Dental Practice")

    # next_steps can be string or list
    next_steps_raw = analysis.get("next_steps") or ""
    if isinstance(next_steps_raw, list):
        next_steps = "; ".join(str(x) for x in next_steps_raw if x)
    else:
        next_steps = str(next_steps_raw) if next_steps_raw else ""

    properties = {
        "phone":                 phone_number,
        "firstname":             contact_first,
        "lastname":              contact_last,
        "company":               resolved_name or "Unknown Practice",
        "lifecyclestage":        "lead",
        "hs_lead_status":        "OPEN",
        "contact_type":          "Practice",
        "sales_lead_type":       sales_lead_type,
        "hs_pipeline":           "default",
        "lgm_ready":             "false",     # default false — flipped to "true" on LGM enrollment
        "practice_vertical":     practice_type,
        "last_call_disposition": analysis.get("conversion_likelihood", ""),
        "call_summary":          analysis.get("summary", ""),
        "call_next_steps":       next_steps,
    }
    if email:
        properties["email"] = email
        print(f"[HUBSPOT] Email: {email}")

    print(f"[HUBSPOT] Writing: name={resolved_name}, contact={contact_first}, type={practice_type}, likelihood={analysis.get('conversion_likelihood')}")

    try:
        # Search by phone
        search = hs("POST", "/crm/v3/objects/contacts/search", json={
            "filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": phone_number}]}]
        })
        results = search.get("results", [])

        # If not found by phone, try email
        if not results and email:
            search2 = hs("POST", "/crm/v3/objects/contacts/search", json={
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}]
            })
            results = search2.get("results", [])

        if results:
            contact_id = results[0]["id"]
            hs("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties})
            print(f"[HUBSPOT] ✅ Updated contact {contact_id}")
            return {"action": "updated", "contact_id": contact_id, "email": email or ""}
        else:
            created = hs("POST", "/crm/v3/objects/contacts", json={"properties": properties})
            contact_id = created["id"]
            print(f"[HUBSPOT] ✅ Created contact {contact_id}")
            return {"action": "created", "contact_id": contact_id, "email": email or ""}

    except Exception as e:
        err_msg = str(e)
        try:
            if hasattr(e, 'response') and e.response is not None:
                err_body = e.response.json()
                err_msg = f"{e} | detail: {err_body}"
        except Exception:
            pass
        print(f"[HUBSPOT] ❌ Error: {err_msg}")
        return {"action": "failed", "error": err_msg}

def add_contact_to_sales_pipeline(contact_id: str, analysis: dict):
    if not HUBSPOT_API_KEY:
        return {"success": False, "error": "No HubSpot key"}

    try:
        likelihood = analysis.get("conversion_likelihood", "low")
        stage_mapping = {
            "high":   "appointmentscheduled",
            "medium": "qualifiedtobuy",
            "low":    "presentationscheduled",
            "none":   "leadstatusopen"
        }
        deal = hs("POST", "/crm/v3/objects/deals", json={"properties": {
            "dealname":  f"Sales Call - {contact_id}",
            "dealstage": stage_mapping.get(likelihood, "leadstatusopen"),
            "pipeline":  "default",
            "closedate": (datetime.now() + timedelta(days=30)).isoformat()
        }})
        deal_id = deal["id"]
        # Associate deal to contact
        hs("PUT", f"/crm/v4/objects/deals/{deal_id}/associations/contacts/{contact_id}", json=[
            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}
        ])
        print(f"[HUBSPOT] ✅ Created deal {deal_id}")
        return {"success": True, "deal_id": deal_id}
    except Exception as e:
        print(f"[HUBSPOT] ❌ Deal creation error: {str(e)}")
        return {"success": False, "error": str(e)}

def add_hubspot_note(contact_id: str, analysis: dict, recording_url: str, call_sid: str):
    if not HUBSPOT_API_KEY:
        return {"success": False}

    try:
        pain_points = analysis.get("pain_points", [])
        objections  = analysis.get("objections", [])
        value_props = analysis.get("value_props_resonated", [])
        key_quotes  = analysis.get("key_quotes", [])
        next_steps  = analysis.get("next_steps", "No next steps defined")

        pp_str = "".join(f"• {p}\n" for p in pain_points) or "None mentioned"
        obj_str = "".join(f"• {o}\n" for o in objections) or "None raised"
        vp_str  = "".join(f"• {v}\n" for v in value_props) or "None identified"
        kq_str  = "".join(f'"{q}"\n' for q in key_quotes) or "None captured"
        conv    = analysis.get("conversion_likelihood", "unknown").upper()
        summary = analysis.get("summary", "No summary available")
        pname   = analysis.get("practice_name", "Unknown")
        ptype   = analysis.get("practice_type", "Unknown")
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M")

        note_body = (
            f"📞 Call Summary - {ts}\n\n"
            f"🏥 Practice: {pname}\n"
            f"📋 Type: {ptype}\n"
            f"📊 Conversion Likelihood: {conv}\n\n"
            f"📝 Summary:\n{summary}\n\n"
            f"😣 Pain Points:\n{pp_str}\n"
            f"🚫 Objections:\n{obj_str}\n"
            f"✅ Value Props That Resonated:\n{vp_str}\n"
            f"➡️ Next Steps:\n{next_steps}\n\n"
            f"💬 Key Quotes:\n{kq_str}\n"
            f"🎙️ Recording: {recording_url}"
        ).strip()

        note = hs("POST", "/crm/v3/objects/notes", json={"properties": {
            "hs_timestamp": str(int(datetime.now().timestamp() * 1000)),
            "hs_note_body": note_body
        }})
        note_id = note["id"]
        hs("PUT", f"/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}", json=[
            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 214}
        ])
        print(f"[HUBSPOT] ✅ Added note to contact {contact_id}")
        return {"success": True, "note_id": note_id}
    except Exception as e:
        print(f"[HUBSPOT] ❌ Note creation error: {str(e)}")
        return {"success": False, "error": str(e)}

@app.get("/calls/recent")
async def recent_calls():
    try:
        result = supabase.table('sales_calls').select(
            "call_sid, phone_number, caller_name, practice_name, status, created_at, transcript"
        ).order('created_at', desc=True).limit(10).execute()
        
        calls = []
        for row in result.data:
            calls.append({
                "call_sid": row['call_sid'],
                "phone_number": row['phone_number'],
                "caller_name": row['caller_name'],
                "practice_name": row['practice_name'],
                "status": row['status'],
                "created_at": row['created_at'],
                "transcript": row['transcript'] is not None
            })
        
        return calls
    except Exception as e:
        print(f"[SUPABASE] ❌ Error fetching calls: {e}")
        return []

@app.get("/admin/export-csv")
async def export_csv():
    try:
        result = supabase.table('sales_calls').select("*").order('created_at', desc=True).execute()
        
        output = StringIO()
        writer = csv.writer(output)
        
        writer.writerow([
            'Call ID', 'Phone Number', 'Caller', 'Practice Name', 
            'Status', 'Created At', 'Completed At', 'Transcript',
            'Practice Type', 'Pain Points', 'Objections', 
            'Value Props Resonated', 'Next Steps', 'Conversion Likelihood',
            'Key Quotes', 'Summary', 'Recording URL'
        ])
        
        for row in result.data:
            call_sid = row['call_sid']
            phone = row['phone_number']
            caller = row['caller_name']
            practice = row['practice_name']
            status = row['status']
            created = row['created_at']
            completed = row.get('completed_at', '')
            transcript = row.get('transcript', '')
            analysis_data = row.get('analysis', {})
            rec_url = row.get('recording_url', '')
            
            if isinstance(analysis_data, str):
                try:
                    analysis = json.loads(analysis_data)
                except:
                    analysis = {}
            else:
                analysis = analysis_data or {}
            
            practice_type = analysis.get('practice_type', '')
            pain_points = '; '.join(analysis.get('pain_points', []))
            objections = '; '.join(analysis.get('objections', []))
            value_props = '; '.join(analysis.get('value_props_resonated', []))
            next_steps = analysis.get('next_steps', '')
            likelihood = analysis.get('conversion_likelihood', '')
            quotes = '; '.join(analysis.get('key_quotes', []))
            summary = analysis.get('summary', '')
            
            writer.writerow([
                call_sid, phone, caller, practice, status, created, completed,
                transcript or '', practice_type, pain_points, objections,
                value_props, next_steps, likelihood, quotes, summary, rec_url
            ])
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=opus_calls_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            }
        )
    except Exception as e:
        print(f"[CSV EXPORT ERROR] {e}")
        return {"error": str(e)}

@app.post("/call/{call_sid}/confirm")
async def confirm_call(call_sid: str, request: Request):
    """
    Confirm review and push to HubSpot + LGM.
    Accepts optional edited fields in request body — any field sent overrides AI analysis.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # If edited fields were sent, merge them into the stored analysis
    analysis_override = None
    if body:
        call_data = supabase.table('sales_calls').select("analysis").eq('call_sid', call_sid).execute()
        if call_data.data:
            raw = call_data.data[0].get('analysis') or {}
            stored = raw if isinstance(raw, dict) else json.loads(raw or '{}')
            stored.update({k: v for k, v in body.items() if v is not None and v != ""})
            analysis_override = stored
            # Persist the edited analysis
            supabase.table('sales_calls').update({"analysis": analysis_override}).eq('call_sid', call_sid).execute()

    push_to_hubspot_lgm(call_sid, analysis_override)
    return {"status": "confirmed", "call_sid": call_sid}


@app.post("/call/{call_sid}/discard")
async def discard_call(call_sid: str):
    """Skip HubSpot + LGM for this call entirely."""
    supabase.table('sales_calls').update({"status": "discarded"}).eq('call_sid', call_sid).execute()
    print(f"[REVIEW] ❌ Discarded {call_sid}")
    return {"status": "discarded", "call_sid": call_sid}


@app.get("/call/{call_sid}", response_class=HTMLResponse)
async def view_call(call_sid: str):
    try:
        result = supabase.table('sales_calls').select("*").eq('call_sid', call_sid).execute()
        if not result.data:
            return "<h1>Call not found</h1>"
        row = result.data[0]
    except Exception as e:
        return f"<h1>Error: {e}</h1>"

    phone         = row.get('phone_number', '')
    caller        = row.get('caller_name', '')
    practice_ui   = row.get('practice_name', '')
    transcript    = row.get('transcript', '')
    recording_url = row.get('recording_url', '')
    status        = row.get('status', '')
    auto_send_at  = row.get('auto_send_at', '')
    raw_analysis  = row.get('analysis') or {}
    analysis      = raw_analysis if isinstance(raw_analysis, dict) else json.loads(raw_analysis or '{}')

    # ── HubSpot field population preview ────────────────────────────────────
    raw_contact   = analysis.get('contact_name') or ''
    firstname     = raw_contact.split()[0] if raw_contact and raw_contact.lower() not in ('null','unknown','') else (analysis.get('practice_name') or practice_ui or '')
    practice_type = analysis.get('practice_type', 'other')
    lead_type_map = {'dental':'Dental Practice','medspa':'Med Spa','weight_loss':'Weight Loss Clinic','other':'Other'}
    sales_lead_type = lead_type_map.get(practice_type, 'Dental Practice')
    email_val     = analysis.get('email_mentioned') or ''
    likelihood    = analysis.get('conversion_likelihood', '')
    next_steps    = analysis.get('next_steps') or ''
    if isinstance(next_steps, list):
        next_steps = '; '.join(next_steps)
    summary       = analysis.get('summary', '')
    contact_title = analysis.get('contact_title') or ''
    callback_name = analysis.get('callback_name') or ''

    def field_row(label, value, editable_key=None, warn=False):
        if value:
            badge   = '<span class="badge ok">✓ Will populate</span>'
            display = f'<span class="fval">{value}</span>'
        else:
            badge   = '<span class="badge miss">✗ Missing</span>'
            display = '<span class="fval empty">—</span>'
        edit = f'<input class="edit-input" data-key="{editable_key}" placeholder="Edit…" value="{value or ""}">' if editable_key else ''
        warn_icon = ' ⚠️' if warn and not value else ''
        return f'<tr><td class="flabel">{label}{warn_icon}</td><td>{badge}</td><td>{display}{edit}</td></tr>'

    hs_rows = (
        field_row('First Name',         firstname,                          'contact_name')   +
        field_row('Practice Name',      analysis.get('practice_name') or practice_ui, 'practice_name') +
        field_row('Phone',              phone)                                                +
        field_row('Email',              email_val,                          'email_mentioned', warn=True) +
        field_row('Contact Title',      contact_title,                      'contact_title')  +
        field_row('Sales - Lead Type',  sales_lead_type)                                      +
        field_row('Contact Type',       'Practice')                                           +
        field_row('Pipeline',           'Sales (default)')                                    +
        field_row('Conversion',         likelihood)                                           +
        field_row('Next Steps',         next_steps,                         'next_steps')     +
        field_row('Summary',            (summary[:80] + '…') if len(summary) > 80 else summary, 'summary') +
        field_row('Callback Name',      callback_name,                      'callback_name')
    )

    lgm_will_run = likelihood != 'none'
    lgm_status   = '✅ Will enroll in LGM campaign' if lgm_will_run else '⛔ Will NOT enroll — explicit hard NO'
    lgm_color    = '#d4edda' if lgm_will_run else '#f8d7da'

    countdown_js = f'const autoSendAt = new Date("{auto_send_at}");' if (status == 'pending_review' and auto_send_at) else ''

    recording_html = f'<a href="{recording_url}" style="color:#667eea">⬇ Download MP3</a>' if recording_url else '—'

    transcript_html = ''
    for line in (transcript or '').split('\n'):
        line = line.strip()
        if not line:
            continue
        is_rep = line.startswith('Rep:')
        cls    = 'rep' if is_rep else 'practice'
        label  = '🎤 Rep' if is_rep else '🏥 Practice'
        text   = line[line.index(':')+1:].strip()
        transcript_html += f'<div class="utt {cls}"><span class="sp">{label}</span><span class="ut">{text}</span></div>'

    if status == 'pending_review':
        bar_bg  = '#e67e00'
        bar_content = f'''
            <div>
                <strong>⏳ Pending Review</strong>
                <span id="countdown" style="margin-left:12px;font-size:13px;opacity:.85"></span>
                <span style="font-size:12px;opacity:.7;margin-left:8px">— auto-sends if not reviewed</span>
            </div>
            <div class="review-actions">
                <button class="btn-discard" onclick="doDiscard()">❌ Discard</button>
                <button class="btn-confirm" onclick="doConfirm()">✅ Confirm &amp; Send</button>
            </div>'''
    elif status in ('confirmed', 'sent'):
        bar_bg  = '#28a745'
        bar_content = '<strong>✅ Sent to HubSpot &amp; LGM</strong>'
    elif status == 'discarded':
        bar_bg  = '#6c757d'
        bar_content = '<strong>❌ Discarded — not sent</strong>'
    else:
        bar_bg  = '#555'
        bar_content = f'<strong>Status: {status}</strong>'

    pain_html = '<br>'.join('• ' + p for p in (analysis.get('pain_points') or [])) or '—'
    obj_html  = '<br>'.join('• ' + o for o in (analysis.get('objections')   or [])) or '—'
    conv_cls  = 'ok' if likelihood in ('high','medium') else 'miss'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Review — {practice_ui or phone}</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh}}
        #review-bar{{position:sticky;top:0;z-index:100;background:{bar_bg};color:white;padding:14px 24px;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
        .review-inner{{max-width:1020px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
        .review-actions{{display:flex;gap:10px}}
        .btn-confirm{{background:white;color:#28a745;border:none;padding:10px 22px;border-radius:8px;font-weight:700;font-size:14px;cursor:pointer}}
        .btn-discard{{background:rgba(255,255,255,.2);color:white;border:1px solid rgba(255,255,255,.5);padding:10px 18px;border-radius:8px;font-size:14px;cursor:pointer}}
        .wrap{{max-width:1020px;margin:0 auto;padding:24px 16px}}
        .back{{color:#667eea;text-decoration:none;font-size:14px;display:inline-block;margin-bottom:16px}}
        h1{{color:#1a1a2e;font-size:22px;margin-bottom:4px}}
        .meta{{color:#666;font-size:13px;margin-bottom:20px}}
        .grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
        @media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
        .card{{background:white;border-radius:12px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
        .card h2{{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#888;margin-bottom:14px}}
        table{{width:100%;border-collapse:collapse}}
        .flabel{{font-size:13px;color:#444;padding:7px 8px 7px 0;width:38%;vertical-align:top;font-weight:500}}
        .fval{{font-size:13px;color:#1a1a2e}}
        .fval.empty{{color:#bbb}}
        .badge{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;white-space:nowrap;margin-right:6px}}
        .badge.ok{{background:#d4edda;color:#155724}}
        .badge.miss{{background:#f8d7da;color:#721c24}}
        .edit-input{{border:1.5px solid #667eea;border-radius:6px;padding:5px 8px;font-size:12px;width:100%;margin-top:5px;display:none;outline:none}}
        .edit-input.active{{display:block}}
        .lgm-box{{padding:12px 16px;border-radius:8px;font-size:13px;font-weight:600;background:{lgm_color};margin-top:14px}}
        .edit-hint{{font-size:11px;color:#aaa;margin-top:8px}}
        .utt{{display:flex;gap:10px;padding:8px 12px;border-radius:8px;margin-bottom:5px;align-items:baseline}}
        .utt.rep{{background:#eef2ff;border-left:3px solid #667eea}}
        .utt.practice{{background:#f0fdf4;border-left:3px solid #43cea2}}
        .sp{{font-size:11px;font-weight:700;text-transform:uppercase;min-width:76px;color:#555;flex-shrink:0}}
        .ut{{font-size:13px;color:#333;line-height:1.5}}
        #toast{{position:fixed;bottom:24px;right:24px;background:#333;color:white;padding:12px 20px;border-radius:8px;font-size:13px;display:none;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.2)}}
    </style>
</head>
<body>
<div id="review-bar"><div class="review-inner">{bar_content}</div></div>
<div class="wrap">
    <a href="/" class="back">← Dashboard</a>
    <h1>{practice_ui or analysis.get('practice_name') or phone}</h1>
    <div class="meta">Called by {caller} &nbsp;·&nbsp; {phone} &nbsp;·&nbsp; {str(row.get('created_at',''))[:16]} &nbsp;·&nbsp; {recording_html}</div>

    <div class="grid">
        <div class="card" style="grid-column:1/-1">
            <h2>📋 HubSpot Fields — click any editable field to fix before sending</h2>
            <table>{hs_rows}</table>
            <div class="lgm-box">{lgm_status}</div>
            <p class="edit-hint">Fields marked ⚠️ are important but missing. Click a value to edit inline.</p>
        </div>

        <div class="card">
            <h2>🤖 AI Analysis</h2>
            <table>
                <tr><td class="flabel">Practice Type</td><td class="fval">{practice_type.title()}</td></tr>
                <tr><td class="flabel">Conversion</td><td><span class="badge {conv_cls}">{likelihood.upper() if likelihood else '—'}</span></td></tr>
                <tr><td class="flabel">Summary</td><td class="fval" style="font-size:12px;line-height:1.5">{summary or '—'}</td></tr>
                <tr><td class="flabel">Pain Points</td><td class="fval" style="font-size:12px">{pain_html}</td></tr>
                <tr><td class="flabel">Objections</td><td class="fval" style="font-size:12px">{obj_html}</td></tr>
                <tr><td class="flabel">Next Steps</td><td class="fval" style="font-size:12px">{next_steps or '—'}</td></tr>
            </table>
        </div>

        <div class="card">
            <h2>📄 Transcript</h2>
            {transcript_html or '<span style="color:#bbb;font-size:13px">Not available yet</span>'}
        </div>
    </div>
</div>
<div id="toast"></div>
<script>
{countdown_js}
if (typeof autoSendAt !== 'undefined') {{
    function tick() {{
        const diff = Math.max(0, Math.round((autoSendAt - new Date()) / 1000));
        const el = document.getElementById('countdown');
        if (!el) return;
        if (diff <= 0) {{ el.textContent = 'Auto-sending now…'; setTimeout(() => location.reload(), 3000); return; }}
        const m = Math.floor(diff/60), s = diff%60;
        el.textContent = 'Auto-sends in ' + m + ':' + s.toString().padStart(2,'0');
    }}
    tick(); setInterval(tick, 1000);
}}
document.querySelectorAll('.fval').forEach(el => {{
    const inp = el.nextElementSibling;
    if (inp && inp.classList.contains('edit-input')) {{
        el.style.cursor = 'pointer';
        el.title = 'Click to edit';
        el.addEventListener('click', () => inp.classList.toggle('active'));
    }}
}});
function toast(msg) {{
    const t = document.getElementById('toast');
    t.textContent = msg; t.style.display = 'block';
    setTimeout(() => t.style.display='none', 3000);
}}
function getEdits() {{
    const e = {{}};
    document.querySelectorAll('.edit-input').forEach(i => {{ if(i.value.trim()) e[i.dataset.key]=i.value.trim(); }});
    return e;
}}
async function doConfirm() {{
    const btn = document.querySelector('.btn-confirm');
    if (btn) btn.disabled = true;
    toast('Sending…');
    const r = await fetch('/call/{call_sid}/confirm', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(getEdits())}});
    if (r.ok) {{ toast('✅ Sent to HubSpot & LGM!'); setTimeout(() => location.reload(), 1500); }}
    else {{ toast('❌ Error — check Render logs'); if(btn) btn.disabled=false; }}
}}
async function doDiscard() {{
    if (!window.confirm('Discard this call? It will NOT be sent to HubSpot or LGM.')) return;
    await fetch('/call/{call_sid}/discard', {{method:'POST'}});
    toast('Discarded'); setTimeout(() => location.reload(), 1500);
}}
</script>
</body>
</html>"""



@app.post("/test/hubspot-lgm")
async def test_hubspot_lgm():
    """
    Fire a fake completed call through the full HubSpot + LGM pipeline.
    Use this to verify field mapping without making a real call.
    DELETE this contact from HubSpot after verifying.
    """
    fake_analysis = {
        "practice_name": "Test Dental Co",
        "contact_name": "Hope",
        "contact_title": "Office Manager",
        "practice_type": "dental",
        "pain_points": ["manual HSA paperwork", "patients unaware of HSA/FSA benefits"],
        "objections": ["need to talk to manager"],
        "value_props_resonated": ["automated billing", "increased average payments by 124%"],
        "next_steps": "Send email to test@testdentalco.com",
        "callback_name": "Hope",
        "callback_extension": None,
        "email_mentioned": "test@testdentalco.com",
        "conversion_likelihood": "medium",
        "key_quotes": ["We deal with that a lot", "Sure, send us the info"],
        "summary": "TEST RECORD - Spoke with Hope, office manager. Interested but needs manager approval. Requested email follow-up."
    }

    fake_phone = "+10000000001"
    fake_call_sid = f"TEST_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"[TEST] Starting test pipeline with call_sid={fake_call_sid}")

    result = create_or_update_hubspot_contact(
        phone_number=fake_phone,
        practice_name="Test Dental Co",
        caller_name="Sanjana",
        analysis=fake_analysis,
        call_sid=fake_call_sid,
        recording_url="https://example.com/test-recording.mp3"
    )

    if result.get("action") in ["created", "updated"]:
        contact_id = result["contact_id"]
        pipeline_result = add_contact_to_sales_pipeline(contact_id, fake_analysis)
        note_result = add_hubspot_note(contact_id, fake_analysis, "https://example.com/test.mp3", fake_call_sid)
        enroll_in_lgm_audience(
            contact_id=contact_id,
            email="test@testdentalco.com",
            phone=fake_phone,
            practice_name="Test Dental Co",
            analysis=fake_analysis
        )
        return {
            "status": "✅ Test complete",
            "hubspot": result,
            "pipeline": pipeline_result,
            "note": note_result,
            "check": {
                "hubspot_contact_id": contact_id,
                "verify_fields": ["firstname", "lastname", "sales_lead_type", "lgm_ready", "contact_type"],
                "lgm_audience": LGM_AUDIENCE_ID
            }
        }
    else:
        return {"status": "❌ HubSpot contact creation failed", "result": result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
