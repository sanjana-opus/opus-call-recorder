# 🚀 OPUS B2B CALL RECORDER - DEPLOY IN 30 MINUTES

## What This Does:
- Click button on iPhone → Auto-calls practice
- Records entire call automatically
- Transcribes with Deepgram AI
- Analyzes with Claude (pain points, objections, next steps)
- Shows dashboard with all calls + transcripts

---

## ⚡ QUICK START (30 MIN)

### Step 1: Get Your Missing Credentials (5 min)

**1A. Twilio Auth Token:**
- Go to: https://console.twilio.com
- Under "Account Info", click the 👁️ eye icon next to "Auth Token"
- Copy it → Save somewhere

**1B. Your Anthropic API Key:**
- Go to: https://console.anthropic.com/settings/keys
- Copy your existing key (or create new one)
- Save it

---

### Step 2: Deploy to Railway (10 min)

**2A. Sign up for Railway:**
- Go to: https://railway.app
- Click "Login with GitHub"

**2B. Create New Project:**
- Click "New Project"
- Click "Deploy from GitHub repo"
- Connect your GitHub account
- Select "Create new repo from template"

**2C. Upload Files:**
You need to upload these 2 files to a GitHub repo:
1. `main.py` (the big Python file)
2. `requirements.txt` (the short dependencies file)

**Option 1 - Use GitHub Web Interface:**
- Go to github.com → Create new repo called "opus-call-recorder"
- Upload main.py and requirements.txt
- Go back to Railway → select that repo

**Option 2 - Use Command Line (if you have git):**
```bash
git init
git add main.py requirements.txt
git commit -m "Initial commit"
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

**2D. Add Environment Variables in Railway:**
Click on your project → Variables tab → Add these:

```
TWILIO_ACCOUNT_SID = ACfa1aad34f3b4f8bb5f928c001e47ec65
TWILIO_AUTH_TOKEN = (paste your Twilio auth token here)
ANTHROPIC_API_KEY = (paste your Anthropic API key here)
```

**2E. Deploy:**
- Click "Deploy"
- Wait ~2 minutes for build to complete
- Copy your Railway URL (looks like: https://opus-call-recorder-production.up.railway.app)

---

### Step 3: Configure Twilio Webhook (5 min)

**3A. Go to Twilio Phone Numbers:**
- https://console.twilio.com/us1/develop/phone-numbers/manage/incoming
- Click on your number: **+1 469 445 4221**

**3B. Set Voice Configuration:**
Scroll to "Voice Configuration" section:
- **A CALL COMES IN:** Select "Webhook"
- **URL:** `https://YOUR-RAILWAY-URL.up.railway.app/voice`
  (Replace YOUR-RAILWAY-URL with your actual Railway URL)
- **HTTP Method:** POST
- Click "Save configuration"

---

### Step 4: TEST IT! (10 min)

**4A. Open on Your iPhone:**
- Safari → Go to your Railway URL
- You'll see: "📞 Opus B2B Call Recorder"

**4B. Make Test Call:**
- Enter your own cell phone number (or a friend's)
- Select your name
- Click "🚀 Start Call"
- Your phone should ring in ~5 seconds
- Answer and say a few sentences
- Hang up

**4C. Check Results:**
- Wait 30-60 seconds
- Refresh the page
- Click "View Transcript & Analysis →"
- You should see:
  - Full transcript
  - AI analysis (pain points, objections, etc.)

---

## 📱 USING IT FOR REAL

### On iPhone:
1. Bookmark your Railway URL in Safari
2. Before calling a practice, open the app
3. Enter practice phone number
4. Click "Start Call"
5. Your phone rings → answer → you're connected to practice
6. Have conversation normally
7. After call, refresh page to see transcript + AI analysis

### On Desktop:
Same thing! Just open your Railway URL in Chrome/Safari

---

## 💰 COSTS

For **100 calls/day** (3,000 calls/month):
- Twilio: ~$117/month
- Deepgram: ~$39/month  
- Railway: ~$5/month
- **Total: ~$161/month**

Railway has $5/month free tier, so first month might be free!

---

## 🆘 TROUBLESHOOTING

**"Call not working"**
- Check Twilio webhook is set correctly
- Make sure you added environment variables in Railway
- Check Railway logs for errors

**"No transcript appearing"**
- Wait 60 seconds after call ends
- Refresh the page
- Check that Deepgram API key is working

**"Error 500"**
- Check Railway logs
- Verify all 3 environment variables are set
- Make sure Twilio auth token is correct

---

## 🎯 WHAT YOU GET

Every call automatically captures:
- ✅ Full transcript with speaker labels
- ✅ Practice name & type
- ✅ Pain points mentioned
- ✅ Objections raised  
- ✅ Which value props resonated
- ✅ Next steps agreed
- ✅ Conversion likelihood (high/medium/low)
- ✅ Key quotes from conversation
- ✅ 2-3 sentence summary

All stored in SQLite database (automatically created).

---

## 📞 SUPPORT

Questions? Problems? Stuck?
Contact: Sanjana

---

**NOW GO DEPLOY IT! You got this! 🚀**
