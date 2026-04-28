# Architecture Decisions

Decisions are listed in order of "most likely to be questioned by future me".

---

## Why Python (not Node, Go, etc.)

python-telegram-bot is mature and async-native. The OpenAI Python SDK has the best ergonomics for Whisper and GPT — file uploads, streaming, and error handling are all first-class. Node has decent bot libraries but the audio pipeline is more awkward. Go would be faster but the LLM + Telegram ecosystem is thin and immature. For an AI-heavy MVP, Python is the correct call.

---

## Why python-telegram-bot (not aiogram, Telebot, etc.)

python-telegram-bot 20.x is fully async, actively maintained, and has comprehensive docs and examples for every update type including voice. aiogram is also async and good, but has less documentation and a smaller English-language community. No compelling reason to switch.

---

## Why GitHub

Standard choice. Coolify integrates directly with GitHub for automatic deploys on push — no CI/CD config needed.

---

## Why Hetzner + Coolify (not Railway, Heroku, Fly.io, AWS, etc.)

Railway was the original host but its free trial ended after 30 days with no sustainable free tier. Hetzner CX22 at €4.49/month is cheaper than any comparable PaaS. Coolify runs on the Hetzner VPS and provides Railway-like DX: GitHub auto-deploy on push, env vars UI, build logs, and domain + SSL management — all self-hosted. Fly.io is excellent but requires Docker and more ops overhead. AWS is overkill for a single-process bot. Heroku killed its free tier in 2022 and is now more expensive. Hetzner + Coolify gives the best price-to-DX ratio for a long-running MVP.

---

## Why polling (not webhooks)

Webhooks require a stable public HTTPS URL and TLS termination. Polling works from a laptop, a Hetzner VPS, or anywhere else — zero URL config. The latency difference is irrelevant here: polling checks every second, but voice note processing takes 5-12 seconds anyway, so the extra second is invisible to users. When user volume grows into the thousands, reconsider webhooks. For an MVP, polling is zero-friction.

---

## Why async/await throughout

python-telegram-bot 20.x is built on asyncio — handlers must be async. The event loop handles concurrent users cleanly: one user's slow transcription does not block another user's message. The one place where blocking I/O is unavoidable (reading the audio file before sending to Whisper) is wrapped in `asyncio.to_thread()` to keep the loop free.

---

## Why OpenAI Whisper (not Deepgram, AssemblyAI, Google STT, etc.)

Whisper handles non-native English, mumbling, and background noise better than alternatives tested in early 2024. It is also the simplest integration — same API key as GPT, no separate account or billing. `whisper-1` is the only model OpenAI currently exposes via API; this decision may need revisiting when newer Whisper versions are made available.

---

## Why /tmp for voice file storage

The audio file only needs to exist for the duration of one Whisper API call — roughly 2-6 seconds. Uploading to S3 or a database before transcribing would add latency, cost, and complexity for zero benefit, since the file is always deleted immediately after transcription. `/tmp` is ephemeral by design and the right tool for short-lived scratch files. Risk: if the process is killed mid-transcription, the file leaks temporarily — but it is small and will be cleaned up on the next server restart.

---

## Why ENABLE_TRANSCRIPT_CLEANING is a flag (not always on)

Cleaning is nearly always an improvement, but in edge cases it could alter meaning — specifically the punctuation normalization and whitespace collapsing. More importantly, the flag lets us debug raw Whisper output during development without redeploying. Default is `true` because the cleaned output is better for 99% of voice notes. Set `false` in `.env` when you need to see exactly what Whisper returned.

---

## Why these specific cleaning rules (and not more aggressive ones)

**Bracket artifacts** (`[inaudible]`, `[music]`, `[00:15]`): Whisper outputs these for unclear audio segments and timestamps. In our context they are always noise — no voice note author wants `[inaudible]` in their Notion page.

**Filler removal** (`um`, `uh`, `hmm`, `you know`): Highest-value cleaning pass. These words carry no information and break readability. Removing them makes the transcript feel closer to written prose.

**Function-word-only deduplication**: Deduplicating all repeated words would destroy intentional speech patterns like "again and again" or "very very important". Limiting dedup to a fixed set of function words (`I`, `the`, `a`, `and`, etc.) catches accidental stutters without altering meaning. Content words are never touched.

**Whitespace and punctuation normalization**: Whisper sometimes produces `end.Start` (missing space after sentence), `good , point` (space before comma), or `good,.` (comma immediately before full stop — an artifact left by filler removal). These four regex rules fix all of those cases without making any semantic changes to the text.

---

## Why Claude Sonnet (not GPT-4, Gemini, etc.) for structuring

The project already uses the Anthropic API (ANTHROPIC_API_KEY). Claude Sonnet reliably follows strict output format instructions — specifically "return only valid JSON, no markdown fences, no preamble" — which is critical because the response is parsed directly with `json.loads()`. GPT-4 follows similar instructions well but adds a second vendor relationship and billing account for a task Claude handles just as well. Gemini was not evaluated. If the Anthropic API ever becomes unavailable, swapping the model in `structure.py` is a one-line change.

---

## Why ENABLE_AI_STRUCTURING is an optional flag (not always on)

Structuring costs money on every voice note — one Claude API call per message. During development and debugging it is useful to turn it off and get the raw or cleaned transcript without spending API credits. The flag also lets the bot degrade gracefully: if `ENABLE_AI_STRUCTURING=false`, the bot still saves a plain-transcript page to Notion, which is acceptable output. Default is `true` because structured output is the whole point of the product.

---

## Why 8000-char input cap for structuring

A 5-minute voice note at average speech rate (~130 words/min) produces roughly 650 words — about 4000 characters. 8000 chars is double that, comfortably covering even fast speakers or dense technical content. Claude's context window is far larger, but we truncate defensively: it bounds per-call cost, keeps latency predictable, and handles any edge case where Whisper produces unexpectedly long output. The truncation is logged as a warning so it is visible if it ever fires in practice.

---

## Why Notion public OAuth (not an internal integration)

Internal Notion integrations are scoped to a single workspace — the one the developer owns. QuiqDrop is a multi-user bot: each user connects their own personal or team Notion workspace. This requires a public integration, which enables the standard OAuth 2.0 flow: user clicks a link, authorises in their browser, and Notion issues a per-user access token. The token is then stored in SQLite and used for all subsequent API calls on behalf of that user.

---

## Why SQLite + aiosqlite (not Postgres, Redis, etc.)

The bot runs as a single process on one Hetzner VPS. There is no horizontal scaling, no need for cross-process shared state, and no concurrent writes that would stress SQLite's locking model. aiosqlite wraps SQLite with an async interface so database calls never block the event loop. Adding Postgres would require a separate managed service, connection pooling, and a migration story — none of which add value at MVP scale. Revisit when user count or deployment architecture changes.

---

## Why /data for the SQLite database

The DB lives at `/data/quiqdrop.db` on the Hetzner server disk, which is persistent across redeploys and restarts — unlike `/tmp` which is ephemeral. The path is configurable via the `DB_PATH` env var (defaults to `/data/quiqdrop.db` in production; set `DB_PATH=/tmp/quiqdrop.db` in `.env` for local dev). This means users never need to re-authorise after a deployment.

---

## Why aiohttp for the OAuth callback web server

aiohttp is already a direct dependency — `notion.py` uses `aiohttp.ClientSession` for the Notion token exchange HTTP call. Adding Flask or FastAPI to handle one GET route would be a net increase in dependencies and process complexity. aiohttp's `web.Application` runs on the same asyncio event loop as the Telegram bot, requires no threading, and integrates cleanly with the existing async codebase. One route, one file, zero new packages.

---

## Why polling and web server run in the same process

The bot needs two concurrent long-running tasks: Telegram polling (outbound HTTP long-poll) and the aiohttp web server (inbound HTTP for the OAuth callback). Running them in the same asyncio event loop via `asyncio.gather` / `web.AppRunner` is simpler than two separate processes — no IPC, no message queues, no port coordination. python-telegram-bot 20.x exposes clean `async with` lifecycle hooks that compose naturally with aiohttp's runner. The only coupling point is `ptb_app` being passed into the OAuth callback handler so it can send Telegram messages after token exchange.

---

## Why send a reminder even when 0 notes were captured

Skipping the reminder when there's nothing to show would mean users only hear from the bot when they're already engaged. The most valuable moment to nudge someone is precisely when they haven't captured anything — it's a gentle prompt to get started. The "0 notes" message is intentionally short and non-intrusive; it does not feel like a failure notification. Users who find even the nudge annoying can disable reminders entirely from `/settings`.

---

## Why a scheduled background loop (not on-demand or exact-time scheduling)

A `while True: ... await asyncio.sleep(3600)` loop in an asyncio task is the simplest correct implementation for this problem. It requires no external scheduler (Celery, APScheduler, cron), no additional process, no Redis broker, and no new dependencies — the reminder scheduler runs in the same event loop as the bot and the OAuth server.

Exact-time scheduling (sleeping until the next 9:00:00 boundary) would add complexity with little benefit: daily and weekly reminders are not time-sensitive to the minute. A 1-hour granularity means users get their reminder anywhere between 9:00 and 9:59 local time, which is entirely acceptable for a "morning nudge" product. If exact delivery times become a product requirement, the sleep logic can be updated without changing anything else.

---

## Why 7 failed deliveries before disabling reminders

Seven days of consecutive failures is a reasonable signal that the user has blocked the bot and is not coming back. Fewer retries (e.g. 3) risks disabling reminders for users who temporarily lose connectivity or have a short Telegram outage. More retries means failed API calls accumulate for blocked users indefinitely. Seven aligns with "one week of daily attempts" — a natural unit — and keeps the failure window bounded. If the user reconnects and interacts with the bot, the failure counter is reset on the next successful delivery.

---

## Why UTC as the default timezone (not asking during setup)

Asking for timezone during the `/connect` or first-run flow adds friction to the most important moment: getting the user connected and sending their first voice note. UTC is a safe, unambiguous default — users in UTC+0 and those who haven't noticed the feature get sensible behaviour. Users who care about receiving reminders at exactly 9am local time can set their timezone directly in the database. A proper in-bot timezone picker is planned for a future phase (6B) once the reminder feature is proven useful.

---

## Why Monday for the weekly reminder (not user-configurable)

Monday morning is the canonical "start of the work week" across most cultures and is the most natural time to review what you captured last week and set intentions for the coming week. Offering day-of-week selection adds a settings screen, a DB column, and UI complexity for a feature that is still unproven. The weekly reminder is already opt-in; users who want a different day can disable it. If demand for customisation emerges, it is a straightforward addition to the `/settings` reminders submenu.
