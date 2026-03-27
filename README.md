# QuiqDrop

A Telegram bot that turns voice messages into structured Notion pages.

Send a voice note → get a clean, readable page in your Notion workspace.

**Current status:** voice → transcript works end-to-end. Notion saving is not built yet.

| Phase | What | Status |
|-------|------|--------|
| 0 | Repo, project structure, version control | done |
| 1 | API keys, accounts | done |
| 2 | Bot running on Railway (polling) | done |
| 3 | Voice download + Whisper transcription + text cleaning | done |
| 4 | AI structuring (GPT/Claude) | not built |
| 5 | Notion OAuth + page creation | not built |
| 6 | Settings (`/settings` command) | not built |

---

## Prerequisites

- Python 3.11+
- A Telegram account — get a bot token from [@BotFather](https://t.me/BotFather)
- An OpenAI account with API access (used for Whisper transcription; GPT needed in Phase 4)
- A Notion account — needed in Phase 5, not required yet

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

Open `.env` and fill in at minimum:

```
TELEGRAM_BOT_TOKEN=   # from BotFather
OPENAI_API_KEY=       # from platform.openai.com/api-keys
```

`config.py` validates all vars at startup — including Notion ones. Until Phase 5, use placeholders:

```
NOTION_CLIENT_ID=placeholder
NOTION_CLIENT_SECRET=placeholder
NOTION_REDIRECT_URI=https://placeholder/notion/callback
```

---

## Running locally

```bash
python src/bot.py
```

Uses polling — no public URL or webhook required. Just run it and send a voice message to your bot on Telegram.

**Useful env flag for debugging:**

```
ENABLE_TRANSCRIPT_CLEANING=false   # see raw Whisper output, no cleaning applied
ENABLE_TRANSCRIPT_CLEANING=true    # default — strips fillers, artifacts, repeated words
```

---

## How it works (current flow)

1. User sends a voice message on Telegram
2. Bot checks duration — rejects if over 5 minutes (before download, saves bandwidth)
3. Bot downloads the `.ogg` file to `/tmp/voice_{user_id}_{file_id}.ogg`
4. Bot checks file size — rejects if over 20 MB (Telegram doesn't expose size before download)
5. Audio is sent to OpenAI Whisper → returns raw transcript
6. If `ENABLE_TRANSCRIPT_CLEANING=true`, four passes run:
   - Strip bracket artifacts: `[inaudible]`, `[music]`, `[00:15]`
   - Remove fillers: `um`, `uh`, `hmm`, `you know`
   - Deduplicate function words: `I I think` → `I think`
   - Normalize whitespace and punctuation
7. Bot replies with the transcript
8. Temp file deleted — even if step 5 or 6 raised an exception

**What happens after step 7:** nothing yet. Phase 4 (AI structuring) and Phase 5 (Notion page creation) are not built.

---

## Deployment (Railway)

The `Procfile` declares a worker dyno:

```
worker: python src/bot.py
```

**Steps:**

1. Push to GitHub
2. Connect repo in [Railway](https://railway.app)
3. Add environment variables in the Railway dashboard (same keys as `.env`)
4. Deploy — Railway starts the worker automatically

**Why worker, not web:** polling needs no HTTP port. Worker dynos are simpler and cheaper for this use case.

**Logs:** Railway dashboard → Deployments → Logs, or `railway logs` via CLI.

---

## Testing

No automated tests yet.

**Manual checklist:**

- [ ] Send `/start` → bot replies with welcome message
- [ ] Send a voice note under 5 min → bot replies with transcript
- [ ] Send a voice note over 5 min → bot rejects with error message
- [ ] Send a text message → bot replies "Send me a voice note to get started"
- [ ] `ENABLE_TRANSCRIPT_CLEANING=false` + voice with "um uh" → fillers appear in reply
- [ ] `ENABLE_TRANSCRIPT_CLEANING=true` + same note → fillers stripped from reply
