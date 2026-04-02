# QuiqDrop

A Telegram bot that turns voice messages into structured Notion pages.

Send a voice note → get a clean, structured page in your Notion workspace.

| Phase | What | Status |
|-------|------|--------|
| 0 | Repo, project structure, version control | done |
| 1 | API keys, accounts | done |
| 2 | Bot running on Railway (polling) | done |
| 3 | Voice download + Whisper transcription + text cleaning | done |
| 4A | AI structuring (Claude) | done |
| 4B | Notion OAuth + page creation | done |
| 5 | Settings / polish | done |
| 6A | Daily + weekly reminders with user preferences | done |

---

## Prerequisites

- Python 3.11+
- A Telegram account — get a bot token from [@BotFather](https://t.me/BotFather)
- An OpenAI account with API access — used for Whisper transcription
- An Anthropic account with API access — used for Claude structuring
- A Notion account — you'll create a public integration and connect your workspace via the bot

---

## Setup

```bash
git clone https://github.com/your-username/quiqdrop.git
cd quiqdrop
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in all required values:

```
TELEGRAM_BOT_TOKEN=     # from BotFather on Telegram
OPENAI_API_KEY=         # from platform.openai.com/api-keys
ANTHROPIC_API_KEY=      # from console.anthropic.com/settings/keys
NOTION_CLIENT_ID=       # from your Notion public integration (see below)
NOTION_CLIENT_SECRET=   # from your Notion public integration (see below)
NOTION_REDIRECT_URI=    # full callback URL, e.g. https://your-app.railway.app/oauth/notion/callback
PORT=8080
```

`config.py` validates all required vars at startup and raises a clear error if any are missing.

---

## Connecting Notion (required before voice notes work)

QuiqDrop uses Notion's public OAuth — each user connects their own workspace.

**One-time setup by you (the developer):**

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and click **New integration**
2. Set the integration type to **Public** (not Internal)
3. Under **OAuth Domain & URIs**, add your redirect URI — e.g. `https://your-app.railway.app/oauth/notion/callback`
   - For local dev: use a tunnel like ngrok — `https://{ngrok-id}.ngrok.io/oauth/notion/callback`
4. Copy the **OAuth Client ID** and **OAuth Client Secret** into your `.env`

**Per user (including yourself), done in Telegram:**

1. Send `/connect` to the bot
2. Click the "Connect Notion Workspace" button — authorise in the browser
3. After authorising, return to Telegram — the bot sends a page picker
4. Select where to save your notes (top-level page, or drill into a subpage)
5. Done — send a voice note to test

> **Important:** In Notion, you must share at least one page with the QuiqDrop integration before connecting. Go to the page → Share → invite the integration by name.

---

## Running locally

```bash
python src/bot.py
```

This starts three things on the same asyncio event loop:
- **Telegram polling** — receives messages from Telegram
- **aiohttp web server** on `PORT` — handles the Notion OAuth callback at `/oauth/notion/callback`
- **Reminder scheduler** — background task that wakes every hour, sends 9am reminders to users in their local timezone

Both must be running for the full flow to work. If testing locally, make sure your `NOTION_REDIRECT_URI` points to your local tunnel URL.

**Env flags for debugging:**

```
ENABLE_TRANSCRIPT_CLEANING=false    # see raw Whisper output, no cleaning
ENABLE_TRANSCRIPT_CLEANING=true     # default — strip fillers, artifacts, repeated words

ENABLE_AI_STRUCTURING=false         # skip Claude — bot replies with plain transcript
ENABLE_AI_STRUCTURING=true          # default — Claude structures the transcript before saving
```

---

## Commands

| Command | What it does |
|---------|--------------|
| `/start` | Greet user, show connection status |
| `/connect` | Start Notion OAuth flow — opens a browser link |
| `/settings` | Show current workspace + destination page; buttons to change page, disconnect, or manage reminders |
| `/disconnect` | Remove your Notion connection from the bot |

**Reminders (via `/settings` → ⏰ Reminders):**

- **Daily reminder** — sent at 9am your local time; lists the notes you captured the previous day
- **Weekly reminder** — sent Monday at 9am your local time; lists notes from the previous week (Mon–Sun)
- Both reminders are enabled by default and can be toggled on/off from the ⏰ Reminders submenu in `/settings`
- If 0 notes were captured, the bot still sends a gentle nudge to record something
- Timezone defaults to UTC; see Known Limitations below

---

## How it works (current flow)

1. Bot checks the user is connected and has a destination page selected — rejects early if not
2. Bot checks voice duration — rejects if over 5 minutes (before download, saves bandwidth)
3. Bot downloads the `.ogg` file to `/tmp/voice_{user_id}_{file_id}.ogg`
4. Bot checks file size — rejects if over 20 MB (Telegram doesn't expose size before download)
5. Audio is sent to OpenAI Whisper → returns raw transcript
6. If `ENABLE_TRANSCRIPT_CLEANING=true`, four cleaning passes run:
   - Strip bracket artifacts: `[inaudible]`, `[music]`, `[00:15]`
   - Remove fillers: `um`, `uh`, `hmm`, `you know`
   - Deduplicate function words: `I I think` → `I think`
   - Normalize whitespace and punctuation
7. If `ENABLE_AI_STRUCTURING=true`, transcript is sent to Claude Sonnet:
   - Returns: title, summary, key points, and (if present) action items and decisions
   - On structuring failure: falls back to plain transcript
8. A new child page is created in the user's chosen Notion parent page:
   - Summary section, Key Points, Action Items (checkbox), Decisions
   - Full transcript in a toggle at the bottom
9. Bot replies with the structured summary and a link to the Notion page
10. Temp audio file deleted — even if any earlier step raised an exception

---

## Deployment (Railway)

The `Procfile` declares a worker dyno:

```
worker: python src/bot.py
```

**Steps:**

1. Push to GitHub
2. Connect repo in [Railway](https://railway.app)
3. Add all environment variables in the Railway dashboard (same keys as `.env`)
4. Deploy — Railway starts the worker automatically

**Why worker, not web:** polling needs no HTTP port exposed to Railway's router. The internal aiohttp server on `PORT` handles only the Notion OAuth redirect — Railway routes that via the public URL you set as `NOTION_REDIRECT_URI`.

**Logs:** Railway dashboard → Deployments → Logs, or `railway logs` via CLI.

**Important:** The SQLite database lives at `/tmp/quiqdrop.db` — it resets on every redeploy. All users must re-connect after a deploy. This is an acceptable MVP trade-off; migrate to persistent storage later.

---

## Known Limitations

- **Reminder timing**: The scheduler checks every hour, so reminders are sent within 60 minutes of 9am — not at exactly 9:00. This is acceptable for daily/weekly nudges.
- **Timezone format**: Timezones must be set directly in the database in the format `UTC`, `UTC+4`, `UTC-5`, etc. There is no in-bot timezone setup UI yet (planned for Phase 6B).
- **Weekly day**: Weekly reminders are always sent on Monday. Day-of-week customisation is planned for a future phase.
- **Reminder time**: Reminders are always sent at 9am. Time-of-day customisation is planned for a future phase.
- **Blocked user retry**: If a user blocks the bot, delivery failures are counted. After 7 consecutive failed deliveries, reminders are automatically disabled for that user to avoid repeated errors.

---

## Testing

No automated tests yet.

**Manual checklist:**

**Phase 2-3 (transcription):**
- [ ] Send `/start` → bot replies with welcome or connection status
- [ ] Send a voice note under 5 min (without connecting Notion first) → bot prompts to use /connect
- [ ] Send a voice note over 5 min → bot rejects with duration error
- [ ] `ENABLE_TRANSCRIPT_CLEANING=false` + voice with "um uh" → fillers appear in reply
- [ ] `ENABLE_TRANSCRIPT_CLEANING=true` + same note → fillers stripped

**Phase 4B (Notion connection):**
- [ ] Send `/connect` → bot sends OAuth link
- [ ] Click link → authorise in browser → return to Telegram
- [ ] Bot sends page picker with your Notion pages
- [ ] Select a top-level page → bot confirms destination
- [ ] Select a top-level page with subpages → bot shows subpage list → select one → confirms
- [ ] Send `/settings` → shows workspace name + selected page + Change Page / Disconnect buttons
- [ ] Click "Change Page" → page picker appears again
- [ ] Click "Disconnect" → confirm → bot confirms disconnection
- [ ] Send `/start` after disconnect → bot shows "not connected" message

**Phase 4A (AI structuring + Notion save):**
- [ ] `ENABLE_AI_STRUCTURING=true` + send voice → bot replies with title, summary, key points + Notion link
- [ ] `ENABLE_AI_STRUCTURING=false` + send voice → bot replies with plain transcript + Notion link
- [ ] Open Notion page → verify Summary, Key Points, Full Transcript toggle are present
- [ ] Voice with action items ("I need to call John") → Action Items section appears in Notion
- [ ] Voice with decisions ("We decided to use Railway") → Decisions section appears in Notion
- [ ] Revoke Notion access from Notion settings → send voice → bot reports token expired, prompts /connect

**Phase 6A (Reminders):**
- [ ] Send `/settings` → "⏰ Reminders" button appears in keyboard
- [ ] Click "⏰ Reminders" → reminder status screen shows daily ✅ and weekly ✅ enabled
- [ ] Click "Daily: ON ✅" → status flips to OFF ❌, database updated
- [ ] Click "Daily: OFF ❌" → status flips back to ON ✅
- [ ] Click "Weekly: ON ✅" → status flips to OFF ❌
- [ ] Click "« Back" → returns to main /settings view
- [ ] Restart bot → reminder preferences persist (SQLite survives in-session)
- [ ] With `time_zone = "UTC"` in DB and current UTC hour = 9 → daily reminder arrives in Telegram
- [ ] With notes created yesterday → daily reminder lists them with titles and Notion URLs
- [ ] With 0 notes yesterday → daily reminder sends "No ideas captured yesterday" message
- [ ] On a Monday with UTC hour = 9 → weekly reminder also sent listing last week's notes
