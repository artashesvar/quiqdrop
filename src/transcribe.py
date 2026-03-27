# OpenAI Whisper integration
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import AsyncOpenAI
import src.config as config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


async def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using OpenAI Whisper. Returns raw transcript text."""
    logger.info("Transcribing: %s", file_path)
    with open(file_path, "rb") as audio_file:
        response = await _client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
    transcript = response.text
    logger.info("Transcript (%d chars): %.80s...", len(transcript), transcript)
    return transcript
