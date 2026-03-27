# OpenAI Whisper integration
import asyncio
import logging
import os

from openai import AsyncOpenAI
import src.config as config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)  # One shared client for the lifetime of the process — avoids re-creating connections per call


def _read_file(path: str) -> bytes:
    """Read audio bytes synchronously. Named function (not lambda) so asyncio.to_thread can call it."""
    with open(path, "rb") as f:
        return f.read()


async def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using OpenAI Whisper. Returns raw transcript text."""
    logger.info("Transcribing: %s", file_path)

    # File I/O is blocking — run in a thread to avoid stalling the event loop
    audio_bytes = await asyncio.to_thread(_read_file, file_path)
    filename = os.path.basename(file_path)

    # Whisper API can silently hang on unusual audio — 60s covers even a 5-min note
    response = await asyncio.wait_for(
        _client.audio.transcriptions.create(
            model=config.WHISPER_MODEL,
            file=(filename, audio_bytes),
        ),
        timeout=60.0,
    )

    transcript = response.text
    logger.info("Transcribed: %d chars", len(transcript))  # issue 2: length only, no content
    return transcript
