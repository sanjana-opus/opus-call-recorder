from fastapi import FastAPI, Request, Form
from fastapi.responses import Response, HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
import anthropic
import httpx
import sqlite3
import json
import os
from datetime import datetime
from contextlib import asynccontextmanager
from deepgram import DeepgramClient, PrerecordedOptions

# Initialize database
def init_db():
    conn = sqlite3.connect('calls.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sales_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT UNIQUE,
            phone_number TEXT,
            caller_name TEXT,
            practice_name TEXT,
            status TEXT,
            transcript TEXT,
            analysis TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(lifespan=lifespan)

# Environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "ACfa1aad34f3b4f8bb5f928c001e47ec65")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = "+14694454221"
YOUR_PHONE_NUMBER = "+12145188667"
DEEPGRAM_API_KEY = "0426dca9f08f7c1d1621900e9f87cbd1c444f263"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Initialize clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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
                    <label>Your Name</label>
                    <select id="caller" required>
                        <option value="Carolina">Carolina</option>
                        <option value="Pranjal">Pranjal</option>
                        <option value="Sanjana">Sanjana</option>
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
                statusDiv.style.display = 'block';
                statusDiv.className = '';
                statusDiv.innerHTML = '⏳ Initiating call... YOUR phone will ring first!';
                
                try {
                    const response = await fetch('/start-call', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            phone_number: document.getElementById('phone').value,
                            caller_name: document.getElementById('caller').value,
                            practice_name: document.getElementById('practice_name').value
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (response.ok) {
                        statusDiv.className = 'status-success';
                        statusDiv.innerHTML = `
                            ✅ YOUR phone (+1-214-518-8667) is ringing now!<br>
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
    
    base_url = str(request.base_url).rstrip('/')
    
    try:
        # FIXED: Call YOUR phone first, then connect to practice
        call = twilio_client.calls.create(
            to=YOUR_PHONE_NUMBER,  # YOUR phone rings first!
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base_url}/voice?practice_number={practice_number}",  # Pass practice number as parameter
            status_callback=f"{base_url}/call-status",
            status_callback_event=['completed'],
            record=True,
            recording_status_callback=f"{base_url}/recording-ready"
        )
        
        conn = sqlite3.connect('calls.db')
        c = conn.cursor()
        c.execute("""
            INSERT INTO sales_calls (call_sid, phone_number, caller_name, practice_name, status)
            VALUES (?, ?, ?, ?, 'initiated')
        """, (call.sid, practice_number, caller_name, practice_name))
        conn.commit()
        conn.close()
        
        return {"call_sid": call.sid, "status": "initiated"}
    
    except Exception as e:
        return {"error": str(e)}, 500

@app.post("/voice")
async def voice(request: Request):
    """TwiML instructions: You answer, then dial the practice"""
    # Get the practice number from query params
    practice_number = request.query_params.get('practice_number', '')
    
    response = VoiceResponse()
    response.say("Connecting you to the practice now.", voice='Polly.Joanna')
    response.dial(practice_number)  # Dial the practice number
    
    return Response(content=str(response), media_type="application/xml")

@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid")
    status = form.get("CallStatus")
    
    conn = sqlite3.connect('calls.db')
    c = conn.cursor()
    c.execute("""
        UPDATE sales_calls 
        SET status = ?
        WHERE call_sid = ?
    """, (status, call_sid))
    conn.commit()
    conn.close()
    
    return {"status": "updated"}

@app.post("/recording-ready")
async def recording_ready(request: Request):
    form = await request.form()
    recording_sid = form.get("RecordingSid")
    call_sid = form.get("CallSid")
    recording_url = form.get("RecordingUrl")
    
    # Download recording
    full_url = f"https://api.twilio.com{recording_url}.mp3"
    
    async with httpx.AsyncClient() as client:
        auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        recording_response = await client.get(full_url, auth=auth)
        audio_data = recording_response.content
    
    # Transcribe with Deepgram
    try:
        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
        )
        
        response = deepgram.listen.rest.v("1").transcribe_file(
            {"buffer": audio_data, "mimetype": "audio/mp3"},
            options
        )
        
        transcript = response["results"]["channels"][0]["alternatives"][0]["transcript"]
        
        # Analyze with Claude
        analysis = analyze_sales_call(transcript)
        
        # Update database
        conn = sqlite3.connect('calls.db')
        c = conn.cursor()
        c.execute("""
            UPDATE sales_calls 
            SET transcript = ?, analysis = ?, status = 'completed', completed_at = ?
            WHERE call_sid = ?
        """, (transcript, json.dumps(analysis), datetime.now().isoformat(), call_sid))
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"Error processing recording: {e}")
        conn = sqlite3.connect('calls.db')
        c = conn.cursor()
        c.execute("""
            UPDATE sales_calls 
            SET status = 'error'
            WHERE call_sid = ?
        """, (call_sid,))
        conn.commit()
        conn.close()
    
    return {"status": "processed"}

def analyze_sales_call(transcript: str) -> dict:
    prompt = f"""Analyze this B2B sales call transcript for Opus Health (healthcare payments platform).

Transcript:
{transcript}

Extract and return as JSON:
{{
    "practice_name": "extracted name or 'Unknown'",
    "practice_type": "dental/weight loss/medical/other",
    "pain_points": ["list of specific pain points mentioned"],
    "objections": ["list of objections or concerns raised"],
    "value_props_resonated": ["which value props seemed to interest them"],
    "next_steps": "what was agreed upon for follow-up",
    "conversion_likelihood": "high/medium/low/none",
    "key_quotes": ["2-3 important quotes from the call"],
    "summary": "2-3 sentence summary of the call"
}}"""
    
    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        
        return json.loads(message.content[0].text)
    except:
        return {"error": "Analysis failed"}

@app.get("/calls/recent")
async def recent_calls():
    conn = sqlite3.connect('calls.db')
    c = conn.cursor()
    c.execute("""
        SELECT call_sid, phone_number, caller_name, practice_name, status, created_at, transcript
        FROM sales_calls
        ORDER BY created_at DESC
        LIMIT 10
    """)
    
    calls = []
    for row in c.fetchall():
        calls.append({
            "call_sid": row[0],
            "phone_number": row[1],
            "caller_name": row[2],
            "practice_name": row[3],
            "status": row[4],
            "created_at": row[5],
            "transcript": row[6] is not None
        })
    
    conn.close()
    return calls

@app.get("/call/{call_sid}", response_class=HTMLResponse)
async def view_call(call_sid: str):
    conn = sqlite3.connect('calls.db')
    c = conn.cursor()
    c.execute("""
        SELECT phone_number, caller_name, practice_name, transcript, analysis, created_at
        FROM sales_calls
        WHERE call_sid = ?
    """, (call_sid,))
    
    row = c.fetchone()
    conn.close()
    
    if not row:
        return "<h1>Call not found</h1>"
    
    phone, caller, practice, transcript, analysis_json, created = row
    analysis = json.loads(analysis_json) if analysis_json else {}
    
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
