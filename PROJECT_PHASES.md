# QuiqDrop — Project Phases

This document outlines the development phases used to build QuiqDrop, a Telegram bot that captures voice notes and saves them to Notion with AI structuring.

Use this as a reference for understanding the project architecture or building similar bots.

---

## Phase 0 — Prepare AI Assistant & Version Control

**Steps:**
1. Create your GitHub repo — this is where all your code lives
2. Define roles in Claude Code — e.g. "You are a Telegram bot backend engineer..." so it knows its job
3. Define skills/commands — shortcuts you can call during the project (e.g. "review this", "write tests", "explain this code")
4. Push initial structure to GitHub — empty repo, but it is tracked from day one

**Goal:** Claude Code knows its role, GitHub is ready, and you are not starting from a blank slate every session.

---

## Phase 1 — Set Up Workspace

**Steps:**
1. Pick your tools — decide what language and framework will run the bot
2. Create accounts — Telegram, Notion, OpenAI, Anthropic
3. Get API keys — collect all access credentials

**Goal:** Everything you need is registered and ready.

---

## Phase 2 — Make the Bot Exist

**Steps:**
1. Create the bot — register it on Telegram via @BotFather
2. Start the server — code that runs 24/7
3. Receive a message — bot reads what you send

**Goal:** You can send the bot a message and it responds.

---

## Phase 3 — Turn Voice Into Text

**Steps:**
1. Download audio — grab the voice file from Telegram
2. Transcribe it — OpenAI Whisper listens, returns text
3. Clean the text — fix obvious noise (remove filler words, artifacts)

**Goal:** Voice note → readable raw transcript.

---

## Phase 4 — Make It Smart

**Steps:**
1. Write the prompt — instruct the AI how to structure the note
2. Get the structure — title, summary, bullets, action items, decisions
3. Push to Notion — create the page via API with OAuth integration

**Goal:** Messy voice note → clean, usable Notion page.

---

## Phase 5 — Ship It and Survive Real Use

**Steps:**
1. Host the bot — running on Hetzner VPS managed via Coolify (auto-deploy from GitHub)
2. Test edge cases — break it on purpose, fix what breaks
3. Use it daily — actually capture thoughts with it

**Goal:** Production-ready bot that works reliably.

---

## Implementation Notes

- Each phase builds on the previous one
- Phases 4 and 5 may require sub-phases for complex features
- This structure can be adapted for similar voice-to-text-to-storage bots
- Estimated timeline: 1 week for full implementation with Claude Code assistance
