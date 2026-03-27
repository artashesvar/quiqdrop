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

Standard choice. Railway integrates directly with GitHub for automatic deploys on push — no CI/CD config needed.

---

## Why Railway (not Heroku, Fly.io, AWS, etc.)

Heroku killed its free tier in 2022 and is now more expensive than Railway for the same specs. Fly.io is excellent but requires Docker and more ops overhead for what is a single-process bot. AWS is complete overkill for an MVP that runs one Python process. Railway deploys directly from GitHub, exposes env vars cleanly, and has a usable free trial. Good enough for Phases 0-5.

---

## Why polling (not webhooks)

Webhooks require a stable public HTTPS URL and TLS termination. Polling works from a laptop, a Railway worker dyno, or anywhere else — zero URL config. The latency difference is irrelevant here: polling checks every second, but voice note processing takes 5-12 seconds anyway, so the extra second is invisible to users. When user volume grows into the thousands, reconsider webhooks. For an MVP, polling is zero-friction.

---

## Why async/await throughout

python-telegram-bot 20.x is built on asyncio — handlers must be async. The event loop handles concurrent users cleanly: one user's slow transcription does not block another user's message. The one place where blocking I/O is unavoidable (reading the audio file before sending to Whisper) is wrapped in `asyncio.to_thread()` to keep the loop free.

---

## Why OpenAI Whisper (not Deepgram, AssemblyAI, Google STT, etc.)

Whisper handles non-native English, mumbling, and background noise better than alternatives tested in early 2024. It is also the simplest integration — same API key as GPT, no separate account or billing. `whisper-1` is the only model OpenAI currently exposes via API; this decision may need revisiting when newer Whisper versions are made available.

---

## Why /tmp for voice file storage on Railway

Railway worker dynos have ephemeral local storage at `/tmp`. The audio file only needs to exist for the duration of one Whisper API call — roughly 2-6 seconds. Uploading to S3 or a database before transcribing would add latency, cost, and complexity for zero benefit, since the file is always deleted immediately after transcription. Risk: if the process is killed mid-transcription, the file leaks temporarily — but `/tmp` is cleared on dyno restart regardless.

---

## Why ENABLE_TRANSCRIPT_CLEANING is a flag (not always on)

Cleaning is nearly always an improvement, but in edge cases it could alter meaning — specifically the punctuation normalization and whitespace collapsing. More importantly, the flag lets us debug raw Whisper output during development without redeploying. Default is `true` because the cleaned output is better for 99% of voice notes. Set `false` in `.env` when you need to see exactly what Whisper returned.

---

## Why these specific cleaning rules (and not more aggressive ones)

**Bracket artifacts** (`[inaudible]`, `[music]`, `[00:15]`): Whisper outputs these for unclear audio segments and timestamps. In our context they are always noise — no voice note author wants `[inaudible]` in their Notion page.

**Filler removal** (`um`, `uh`, `hmm`, `you know`): Highest-value cleaning pass. These words carry no information and break readability. Removing them makes the transcript feel closer to written prose.

**Function-word-only deduplication**: Deduplicating all repeated words would destroy intentional speech patterns like "again and again" or "very very important". Limiting dedup to a fixed set of function words (`I`, `the`, `a`, `and`, etc.) catches accidental stutters without altering meaning. Content words are never touched.

**Whitespace and punctuation normalization**: Whisper sometimes produces `end.Start` (missing space after sentence), `good , point` (space before comma), or `good,.` (comma immediately before full stop — an artifact left by filler removal). These four regex rules fix all of those cases without making any semantic changes to the text.
