# Transcript cleaning logic
import re
import logging

logger = logging.getLogger(__name__)

# Only deduplicate function words — content words like "very very" are intentional
_DEDUPE_WORDS = {"i", "the", "a", "an", "and", "that", "to", "is", "was", "it", "of", "in"}


def _remove_artifacts(text: str) -> str:
    """Remove [inaudible], [music], [noise], [00:15], and similar bracket artifacts."""
    return re.sub(r"\[[^\]]*\]", "", text)


def _remove_fillers(text: str) -> str:
    """Remove standalone filler sounds: um, uh, hmm, erm, er, you know."""
    text = re.sub(r"\b(um+|uh+|hmm*|erm?|you know)\b", "", text, flags=re.IGNORECASE)
    # Clean up orphaned commas left behind: ", ," → "," and leading ", "
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"^\s*,\s*", "", text)
    return text


def _remove_repeated_words(text: str) -> str:
    """Fix accidental word repetitions for function words only: 'I I think' → 'I think'."""
    def _replace(m: re.Match) -> str:
        word = m.group(1)
        return word if word.lower() in _DEDUPE_WORDS else m.group(0)

    return re.sub(r"\b(\w+)\s+\1\b", _replace, text, flags=re.IGNORECASE)


def _normalize_whitespace(text: str) -> str:
    """Collapse multiple spaces/newlines and fix spacing around punctuation."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s([,.!?])", r"\1", text)       # remove space before punctuation
    text = re.sub(r"([.!?])([A-Z])", r"\1 \2", text)  # ensure space after sentence end
    return text


def clean_transcript(raw_text: str) -> str:
    """Apply all cleaning rules in sequence. Returns cleaned text."""
    if not raw_text or not raw_text.strip():
        return raw_text

    original_len = len(raw_text)
    text = raw_text
    text = _remove_artifacts(text)
    text = _remove_fillers(text)
    text = _remove_repeated_words(text)
    text = _normalize_whitespace(text)

    removed = original_len - len(text)
    logger.info("Cleaning: %d→%d chars (%d removed)", original_len, len(text), removed)
    return text
