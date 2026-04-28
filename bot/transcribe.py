"""
transcribe.py — Sends an audio file to the OpenAI Whisper API and returns the transcript.

Whisper is very good at recognising speech in many languages — it automatically
detects the language, so we don't need to specify it here.
"""

import os
import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Create a single async client that is reused across all calls
_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


async def transcribe_audio(file_path: str) -> str:
    """
    Send the audio file at `file_path` to Whisper and return the transcribed text.

    Args:
        file_path: Absolute path to the .ogg voice file downloaded from Telegram.

    Returns:
        The transcribed text as a plain string.

    Raises:
        Exception: Propagates any OpenAI API error so the caller can handle it.
    """
    logger.info("Sending audio to Whisper API: %s", file_path)

    with open(file_path, "rb") as audio_file:
        response = await _client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            # Telegram voice messages are Opus inside an OGG container.
            # Whisper accepts this format natively — no conversion needed.
        )

    transcript = response.text.strip()
    logger.info("Whisper returned %d characters", len(transcript))
    return transcript
