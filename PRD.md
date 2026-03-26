# PRD — Voice → Notion Brain Dump Bot (Telegram)

## 1. 🧠 Problem Statement (JTBD)

### Core Job
When I have a thought in moments where typing is hard or impossible, I want to quickly capture it via voice and have it automatically structured and stored so I can revisit it later without friction.

### Contexts
- Walking / commuting
- Between meetings
- Lying down / low-energy moments
- Driving (hands busy, cognitive bandwidth limited)

### Pain Points
- Voice notes in Telegram are hard to revisit
- Raw transcripts are too messy to be useful
- Switching apps (e.g., separate tools like voice note apps) adds friction
- Ideas get lost because:
  - Capture is inconsistent
  - Organization is delayed → never happens

---

## 2. 🧍 Day in the Life

### ❌ Before
Artashes has an idea while walking:
- Opens Telegram → sends voice note to self
- Later: sees a list of voice messages → "meh"
- Doesn't replay → idea is effectively lost

### ✅ After
- Opens Telegram bot
- Sends voice (30–120 sec)
- Within ~5 seconds:
  - Bot replies: "Saved to Notion ✅"
- In Notion:
  - A new page appears with:
    - Clean title
    - Summary
    - Structured notes (if applicable)
    - Full transcript
    - Audio file

Later:
- Browses Notion → ideas are readable, scannable, usable

---

## 3. 💡 Solution Overview

A Telegram bot that:
1. Receives voice messages
2. Transcribes + structures them using AI
3. Creates a new child page inside a selected Notion page

### Core Principle
> Zero-friction capture → automatic structuring → future readability

---

## 4. 🧩 Core UX Flow

### First-Time Setup
1. User opens bot
2. Bot prompts: "Connect your Notion workspace"
3. User authenticates Notion
4. Bot asks: "Select the page where your notes will be saved"
5. User selects one page (parent page)
6. Bot confirms: "You're all set. Send a voice note anytime 🎤"

### Main Flow (MVP)
1. User sends voice message
2. Bot:
   - Downloads audio
   - Transcribes
   - Runs structuring prompt
   - Creates Notion page
3. Bot replies: "Saved to Notion ✅"

### Settings Flow
Commands:
- `/settings`
  - Change Notion page
  - Reconnect Notion

---

## 5. 🧱 Output Structure in Notion

Each voice note creates a **New Child Page**:

**Title:** AI-generated (based on intent)

**Content:**
```
📝 Summary
<2–4 sentence concise summary>

💡 Key Points
- Bullet points (ideas, insights)

✅ Action Items (if detected)
- Task 1
- Task 2

🧠 Decisions (if detected)
- Decision statements

📄 Full Transcript
<raw transcript>

🔊 Audio
<attached file>
```

---

## 6. ⚙️ AI Processing Logic

### Step 1 — Transcription
- Input: voice (≤ 3 minutes)
- Output: raw text

### Step 2 — Structuring Prompt
Prompt should:
- Generate:
  - Title
  - Summary
  - Structured sections only if relevant
- Avoid over-structuring

---

## 7. 🧩 Jobs to Be Done (Detailed)

### Main Job
Capture thought → store → make it usable later

### Sub-Jobs

**Setup Phase**
- Connect Notion account
- Select destination page

**Capture Phase**
- Open Telegram quickly
- Record and send voice
- Trust system will process it

**Processing Phase (system)**
- Transcribe audio
- Extract meaning
- Structure content
- Create Notion page

**Review Phase (later, outside MVP)**
- Browse notes
- Scan summaries
- Dive into details if needed

---

## 8. 🚀 MVP Scope

### Included
- Telegram bot
- Notion integration
- Voice → transcript
- AI structuring
- New page creation
- Audio file attachment
- Single destination page
- Settings to change page

### Excluded (for now)
- Tagging
- Search
- Multiple destinations per message
- Modes (idea/task/etc.)
- Playback inside Telegram
- Collaboration / sharing
- Weekly summaries

---

## 9. ⚡ Performance Expectations

**Target:** 3 min voice → processed in 4–5 seconds

**Reality check:**
- Transcription alone may take 2–6 seconds
- AI structuring: 2–4 seconds
- Notion API: 1–2 seconds

**Expected realistic latency: 6–12 seconds**

**Recommendation:** Show intermediate feedback:
- "Processing…" → "Saved ✅"

---

## 10. 🏗️ System Design (High-Level)

### Components
- Telegram Bot API
- Backend (Node.js / Python)
- Speech-to-text (e.g., Whisper)
- LLM for structuring
- Notion API

### Flow
```
Voice → Telegram → Backend
                      ↓
               Transcription
                      ↓
              AI Structuring
                      ↓
          Notion Page Creation
                      ↓
         Confirmation → Telegram
```

---

## 11. ⚠️ Risks & Edge Cases

1. **Messy Input** — User rambles → output may be weak
   - Mitigation: strong prompt design

2. **Over-structuring** — AI invents structure where none exists
   - Mitigation: "only if present" instruction

3. **Latency frustration**
   - Mitigation: feedback message

4. **Notion API friction** — Permissions / page selection UX can be clunky

5. **Title quality** — Bad titles = poor future retrieval

---

## 12. 🔥 Why This Could Work

- Removing friction at the highest drop-off moment
- Competes not with tools, but with:
  - Laziness
  - Forgetfulness
- Strong combo:
  - Telegram (already used)
  - Notion (already used)
  - AI (adds real value)

---

## 13. 🧠 Sharp Insight (Important)

This is **NOT**: "voice notes to Notion"

This **IS**: "Turning messy thinking into usable knowledge automatically"

> If you get the output quality right, this becomes addictive.
> If not → it becomes another graveyard.
