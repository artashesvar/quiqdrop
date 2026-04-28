"""
transcribe.py — Sends an audio file to the OpenAI Whisper API and returns the transcript.

Whisper automatically detects the language, so we don't need to specify it.
"""

import os
import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


async def transcribe_audio(file_path: str) -> str:
    """
    Send the audio file at `file_path` to Whisper and return the transcribed text.
    The file must have a .ogg extension — Whisper uses it to detect the format.
    """
    logger.info("Sending audio to Whisper API: %s", file_path)

    with open(file_path, "rb") as audio_file:
        response = await _client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )

    transcript = response.text.strip()
    logger.info("Whisper returned %d characters", len(transcript))
    return transcript
