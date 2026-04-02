# AI structuring: converts cleaned transcript → structured JSON via Claude API
import asyncio
import json
import logging
import time
from anthropic import AsyncAnthropic, APIError

import src.config as config

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1000  # structured JSON output is compact — 1000 tokens covers even a 5-min note
_MAX_INPUT_CHARS = 8000  # truncation threshold — 8000 chars covers ~1200 words, well beyond any real voice note

_SYSTEM_PROMPT = """\
You are a note-taking assistant. Your job is to extract structure from a voice note transcript.

Return ONLY a valid JSON object — no markdown fences, no explanation, no preamble. Nothing else.

The JSON must have these fields:
  "title"      — 5 to 7 word summary of the main topic (string)
  "summary"    — 2 to 3 sentence overview of what was said (string)
  "key_points" — list of the most important insights (array of strings, at least 1)

Add these fields ONLY if the evidence is explicit in the transcript:
  "action_items" — tasks that were explicitly committed to using phrases like "need to",
                   "should", "must", "have to", "I'll", "we'll" (array of strings)
  "decisions"    — choices that were explicitly stated using "decided", "going with",
                   "chose", "we are using" (array of strings)

Rules:
- Omit "action_items" entirely if none are explicitly mentioned — do not add an empty array.
- Omit "decisions" entirely if none are explicitly stated — do not add an empty array.
- Do not invent structure. If the transcript is rambling or unclear, do your best with what's there.
- key_points should be insights, not just restated facts. Max 5 points.
- Each action item and decision should be one concise sentence.
"""


class StructuringError(Exception):
    """Raised when structuring fails for any reason (API error, bad JSON, missing fields)."""


async def structure_transcript(transcript: str) -> dict:
    """
    Send a cleaned transcript to Claude and return a structured dict.

    Returns a dict with keys: title, summary, key_points, and optionally
    action_items and decisions.

    Raises StructuringError on API failure, malformed JSON, or missing required fields.
    """
    transcript = transcript.strip()

    # Short-circuit for very short transcripts — not worth an API call
    word_count = len(transcript.split())
    if word_count < 10:
        logger.info("Transcript too short (%d words) — returning minimal structure", word_count)
        return {"title": "Voice note", "summary": transcript, "key_points": []}

    # Truncate very long transcripts to cap cost and stay well within context limits
    if len(transcript) > _MAX_INPUT_CHARS:
        logger.warning(
            "Transcript truncated from %d to %d chars for structuring",
            len(transcript), _MAX_INPUT_CHARS,
        )
        transcript = transcript[:_MAX_INPUT_CHARS]

    logger.info("Structuring transcript (len=%d)", len(transcript))
    t0 = time.monotonic()

    try:
        response = await asyncio.wait_for(
            _client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": transcript}],
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.error("Anthropic API call timed out after 30s")
        raise StructuringError("API timeout") from None
    except APIError as e:
        logger.error("Anthropic API error during structuring: %s", e)
        raise StructuringError(f"API error: {e}") from e

    if not response.content:
        raise StructuringError("Empty response from API")
    raw = response.content[0].text.strip()
    # Defensive strip: Claude occasionally wraps output in ```json fences despite the prompt
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    logger.debug("Raw structuring response: %s", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Malformed JSON from structuring API. Raw response: %s", raw)
        raise StructuringError(f"Malformed JSON: {e}") from e

    # Validate required fields
    for field in ("title", "summary"):
        if not data.get(field):
            logger.error("Missing required field '%s' in structured output: %s", field, data)
            raise StructuringError(f"Missing required field: {field}")

    # Ensure key_points is a list (even if empty)
    if "key_points" not in data:
        data["key_points"] = []

    elapsed = time.monotonic() - t0
    logger.info(
        "Structuring complete in %.2fs — action_items=%s decisions=%s",
        elapsed,
        bool(data.get("action_items")),
        bool(data.get("decisions")),
    )

    return data
