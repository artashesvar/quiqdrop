"""
notion_helper.py — All Notion API interactions live here.

Every function accepts a `token` argument — the user's personal Notion OAuth
access token — so each user reads and writes only their own workspace.

Notion text blocks have a 2 000-character limit per rich_text segment, so long
transcripts are automatically split into multiple paragraph blocks.
"""

import logging
from datetime import datetime

from notion_client import AsyncClient

logger = logging.getLogger(__name__)

# Maximum characters allowed in a single Notion rich_text segment
_NOTION_TEXT_LIMIT = 2000


def _client(token: str) -> AsyncClient:
    """Create a Notion client authenticated with the user's personal token."""
    return AsyncClient(auth=token)


def _split_text(text: str) -> list[str]:
    """
    Split a long string into chunks of at most _NOTION_TEXT_LIMIT characters.
    Notion rejects rich_text items that exceed this limit.
    """
    return [text[i: i + _NOTION_TEXT_LIMIT] for i in range(0, len(text), _NOTION_TEXT_LIMIT)]


def _paragraph_blocks_from_text(text: str) -> list[dict]:
    """
    Convert a (potentially long) string into a list of Notion paragraph blocks,
    splitting at the 2 000-char boundary when necessary.
    """
    blocks = []
    for chunk in _split_text(text):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        })
    return blocks


async def list_top_pages(token: str) -> list[dict]:
    """
    Return up to 20 pages that the user has shared with the integration.

    Each item in the returned list is a dict: {"id": "...", "name": "..."}.

    If the list is empty, the user hasn't shared any pages yet — remind them
    to use "Add Connections" inside Notion after authorising.
    """
    logger.info("Fetching Notion pages for user token …%s", token[-6:])

    notion = _client(token)
    response = await notion.search(
        filter={"value": "page", "property": "object"},
        page_size=20,
    )

    results = []
    for page in response.get("results", []):
        title = _get_page_title(page)
        results.append({"id": page["id"], "name": title or "Untitled"})

    logger.info("Found %d pages", len(results))
    return results


def _get_page_title(page: dict) -> str:
    """
    Pull the plain-text title out of a Notion page object.
    Notion stores titles differently depending on the page type, so we check
    several common locations.
    """
    props = page.get("properties", {})
    for key in ("title", "Title", "Name", "name"):
        prop = props.get(key)
        if not prop:
            continue
        rich_texts = prop.get("title", prop.get("rich_text", []))
        if rich_texts:
            return "".join(rt.get("plain_text", "") for rt in rich_texts)
    return ""


async def create_note_page(token: str, parent_id: str, transcript: str, summary: str) -> str:
    """
    Create a new Notion page under `parent_id` using the user's own token.

    Page structure:
        Title  : First 60 characters of transcript, or "Voice Note – {datetime}"
        Body   : Summary (paragraph blocks)
        Toggle : "Transcript" — expands to show the full original transcript

    Returns:
        The page title that was used (so the bot can echo it back to the user).
    """
    title_text = transcript[:60].strip() if len(transcript) >= 10 else None
    if not title_text:
        title_text = f"Voice Note – {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    logger.info("Creating Notion page: %s", title_text)

    notion = _client(token)
    body_blocks = _paragraph_blocks_from_text(summary)
    transcript_children = _paragraph_blocks_from_text(transcript)
    toggle_block = {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "Transcript"}}],
            "children": transcript_children,
        },
    }

    await notion.pages.create(
        parent={"page_id": parent_id},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title_text}}]
            }
        },
        children=body_blocks + [toggle_block],
    )

    return title_text
