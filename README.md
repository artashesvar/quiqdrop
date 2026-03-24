# Voice-to-Notion Bot

A Telegram bot that turns your voice messages into organised Notion notes — in your own Notion workspace.

Send a voice message → the bot transcribes it → summarises it → saves it to **your** Notion. Each user connects their own Notion account.

---

## What you'll need

- A Telegram account
- An OpenAI account (for transcription)
- An Anthropic account (for summarisation)
- A Notion account
- A free [Railway](https://railway.app) account (for hosting)

---

## Step-by-step setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the **bot token**. This is your `TELEGRAM_BOT_TOKEN`.

---

### 2. Get an OpenAI API key

1. Go to [platform.openai.com](https://platform.openai.com) → **API keys** → **Create new secret key**.
2. Copy the key. This is your `OPENAI_API_KEY`.

---

### 3. Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com) → **Settings → API Keys → Create Key**.
2. Copy the key. This is your `ANTHROPIC_API_KEY`.

---

### 4. Create a Notion integration (public, with OAuth)

This bot uses OAuth so that **each user connects their own Notion account**. This requires a *public* Notion integration (not an internal one).

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New integration**.
2. Give it a name (e.g. "Voice Bot") and select your workspace.
3. Under **Integration type**, select **Public** (not Internal).
4. Fill in the required fields:
   - **Website**: your Railway app URL (you can add this later)
   - **Privacy policy URL**: same as above (Railway URL + `/privacy` is fine as a placeholder)
5. Save. You'll see a **Client ID** and **Client Secret** — copy both.
   - `Client ID` → `NOTION_CLIENT_ID`
   - `Client Secret` → `NOTION_CLIENT_SECRET`
6. Under **Redirect URIs**, add your Railway callback URL:
   ```
   https://your-app.up.railway.app/notion/callback
   ```
   *(You'll get this URL after deploying to Railway in Step 5. You can come back and add it.)*

---

### 5. Deploy to Railway

1. Push this project to a GitHub repository (make sure `.env` is in `.gitignore`).
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Select your repository.
4. In the Railway dashboard, go to your service → **Variables** → add these variables:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `OPENAI_API_KEY` | from OpenAI |
   | `ANTHROPIC_API_KEY` | from Anthropic |
   | `NOTION_CLIENT_ID` | from Notion integration |
   | `NOTION_CLIENT_SECRET` | from Notion integration |
   | `NOTION_REDIRECT_URI` | `https://your-app.up.railway.app/notion/callback` |

5. After Railway deploys, copy your app's public URL (shown in the Railway dashboard under **Domains**). Go back to your Notion integration and add that URL as a Redirect URI:
   ```
   https://your-app.up.railway.app/notion/callback
   ```

---

### 6. First use in Telegram

1. Open your bot → send `/start`.
2. Send `/connect` → tap the "Connect Notion" button → log in and allow access.
3. Send `/setfolder` → pick the Notion page where notes will be saved.
4. Send `/setlanguage` → choose summary language (optional — defaults to same as voice).
5. Send a voice message — done! Check your Notion for the new note.

> Your settings are saved permanently — you only need to go through setup once.

---

## Local development

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd quiqdrop

# 2. Create a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up your environment variables
cp .env.example .env
# Edit .env and fill in all keys

# 5. For local OAuth testing, expose your local server with ngrok:
#    ngrok http 8080
#    Then set NOTION_REDIRECT_URI=https://{ngrok-id}.ngrok.io/notion/callback
#    And add that URL to your Notion integration's Redirect URIs

# 6. Run the bot
python main.py
```

---

## How it works

```
User sends voice message
        │
        ▼
Download .ogg from Telegram
        │
        ▼
OpenAI Whisper → transcript text
        │
        ▼
Anthropic Claude (claude-haiku-4-5) → summary
        │
        ▼
Notion API (user's own token) → new page created
        │
        ▼
Bot replies: "✅ Saved to Notion!"
```

---

## Project structure

```
quiqdrop/
├── main.py               # Entry point — runs web server + Telegram bot
├── bot/
│   ├── handlers.py       # /start, /connect, /setfolder, /setlanguage, voice
│   ├── oauth.py          # Notion OAuth flow (URL builder + token exchange)
│   ├── transcribe.py     # Whisper API (speech → text)
│   ├── summarize.py      # Claude API (text → summary)
│   ├── notion_helper.py  # Notion API (list pages, create notes)
│   └── db.py             # SQLite (saves per-user token, folder, language)
├── requirements.txt
├── Procfile
├── railway.toml
├── nixpacks.toml         # Installs ffmpeg on Railway
└── .env.example
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `/connect` button doesn't work | Make sure `NOTION_REDIRECT_URI` is set and matches the Redirect URI in your Notion integration |
| `/setfolder` shows an empty list | Re-run `/connect` — you may not have selected any pages when authorising |
| Bot doesn't respond | Check Railway logs — the bot token may be wrong |
| "Couldn't transcribe" error | Check your `OPENAI_API_KEY` |
| "Couldn't save to Notion" | Run `/connect` again to refresh your Notion token |
