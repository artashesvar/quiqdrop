"""
summarize.py — Sends a transcript to the Anthropic Claude API and returns a concise summary.

We use the claude-haiku-4-5 model which is fast and cost-effective for summarisation tasks.
"""

import os
import logging

import anthropic

logger = logging.getLogger(__name__)

# Create a single async client that is reused across all calls
_client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# The exact model ID — do not change this string
MODEL = "claude-haiku-4-5"


async def summarize_text(transcript: str, language: str = "same") -> str:
    """
    Ask Claude to produce a concise summary of the given transcript.

    Args:
        transcript: The full text returned by Whisper.
        language:   "same"    → reply in the same language as the transcript.
                    "english" → always reply in English.

    Returns:
        A short summary string.

    Raises:
        Exception: Propagates any Anthropic API error so the caller can handle it.
    """
    if language == "english":
        prompt = (
            "Summarize the following transcript concisely in English. "
            f"Transcript: {transcript}"
        )
    else:
        # "same" — let Claude mirror the language of the transcript
        prompt = (
            "Summarize the following transcript concisely. "
            "Respond in the same language as the transcript. "
            f"Transcript: {transcript}"
        )

    logger.info("Sending transcript to Claude (%s), language=%s", MODEL, language)

    message = await _client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    summary = message.content[0].text.strip()
    logger.info("Claude returned summary of %d characters", len(summary))
    return summary
