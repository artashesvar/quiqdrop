"""
summarize.py — Sends a transcript to the Anthropic Claude API and returns a concise summary.
"""

import os
import logging

import anthropic

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-haiku-4-5"


async def summarize_text(transcript: str, language: str = "same") -> str:
    """
    Ask Claude to produce a concise summary of the given transcript.

    language: "same"    → reply in the same language as the transcript
              "english" → always reply in English
    """
    if language == "english":
        prompt = (
            "Summarize the following transcript concisely in English. "
            f"Transcript: {transcript}"
        )
    else:
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
