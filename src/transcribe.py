# OpenAI Whisper integration
import asyncio
import logging
import os

from openai import AsyncOpenAI
import src.config as config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using OpenAI Whisper. Returns raw transcript text."""
    logger.info("Transcribing: %s", file_path)

    # Read file in a thread to avoid blocking the event loop (issue 5)
    audio_bytes = await asyncio.to_thread(_read_file, file_path)
    filename = os.path.basename(file_path)

    # Enforce a timeout so a slow/hung API call doesn't block the handler (issue 4)
    response = await asyncio.wait_for(
        _client.audio.transcriptions.create(
            model=config.WHISPER_MODEL,  # issue 6: moved to config
            file=(filename, audio_bytes),
        ),
        timeout=60.0,
    )

    transcript = response.text
    logger.info("Transcribed: %d chars", len(transcript))  # issue 2: length only, no content
    return transcript
