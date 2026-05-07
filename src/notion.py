# Notion API integration — OAuth token exchange, page search, page creation
from __future__ import annotations
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import aiohttp
from notion_client import AsyncClient
from notion_client.errors import APIResponseError, APIErrorCode

import src.config as config

logger = logging.getLogger(__name__)

# Shared async client — no default auth; token passed per-request via auth= kwarg.
# This lets one client instance serve all users without re-instantiation.
_notion = AsyncClient()

# Notion API enforces a 2000-char limit per rich_text content block
_RICH_TEXT_LIMIT = 2000

# Conservative chunk size — leaves headroom for unicode, escape sequences, and any
# character growth in serialization. We pick on a sentence/word boundary when possible.
_CHUNK_SIZE = 1900


def _split_for_rich_text(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """
    Split a string into chunks ≤ chunk_size, preferring sentence then word boundaries.
    Used to keep paragraph/list blocks under Notion's 2000-char rich_text limit.
    Returns at least one chunk (possibly empty if input was empty).
    """
    text = text or ""
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        cut = chunk_size
        # Prefer sentence boundary (. ! ? followed by space) within the last 200 chars
        boundary = max(
            remaining.rfind(". ", cut - 200, cut),
            remaining.rfind("! ", cut - 200, cut),
            remaining.rfind("? ", cut - 200, cut),
        )
        if boundary == -1:
            # Fall back to word boundary
            boundary = remaining.rfind(" ", cut - 200, cut)
        if boundary == -1:
            # Hard cut — no whitespace anywhere reasonable
            boundary = cut
        else:
            boundary += 1  # include the space/punctuation in the previous chunk
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _paragraphs_for(text: str) -> list[dict]:
    """Build one or more paragraph blocks for a string, splitting at the rich_text limit."""
    return [_paragraph(chunk) for chunk in _split_for_rich_text(text) if chunk]


def _bullets_for(text: str) -> list[dict]:
    """Build one or more bulleted_list_item blocks, splitting if the text exceeds the limit."""
    return [_bullet(chunk) for chunk in _split_for_rich_text(text) if chunk]


def _todos_for(text: str) -> list[dict]:
    """Build one or more to_do blocks, splitting if the text exceeds the limit."""
    return [_todo(chunk) for chunk in _split_for_rich_text(text) if chunk]

# Shared aiohttp session for OAuth token exchange — avoids reopening a connection
# pool per /oauth/notion/callback. Created lazily so it lives in the right event loop.
_aiohttp_session: aiohttp.ClientSession | None = None


async def _get_aiohttp_session() -> aiohttp.ClientSession:
    global _aiohttp_session
    if _aiohttp_session is None or _aiohttp_session.closed:
        _aiohttp_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _aiohttp_session


async def close_aiohttp_session() -> None:
    """Close the shared session. Call on shutdown."""
    global _aiohttp_session
    if _aiohttp_session is not None and not _aiohttp_session.closed:
        await _aiohttp_session.close()
        _aiohttp_session = None


_NOTION_WEB_PREFIX = "https://www.notion.so/"


def _bouncer_secret() -> bytes:
    """Derive a stable HMAC key from the bot token. The bot token is required and
    secret already, so reusing it avoids introducing a new env var."""
    return hashlib.sha256(config.TELEGRAM_BOT_TOKEN.encode("utf-8")).digest()


def _bouncer_payload(path: str, query: str) -> bytes:
    """Canonical bytes signed for a bouncer URL: '<path>?<query>' (no ? if empty)."""
    canonical = f"{path}?{query}" if query else path
    return canonical.encode("utf-8")


def bouncer_sign(path: str, query: str = "") -> str:
    """Return a short HMAC tag for a bouncer (path, query). 12 bytes (96 bits) is
    plenty against an online attacker — they can't bulk-grind signatures."""
    mac = hmac.new(_bouncer_secret(), _bouncer_payload(path, query), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:12]).decode("ascii").rstrip("=")


def bouncer_verify(path: str, query: str, sig: str) -> bool:
    """Constant-time signature check."""
    if not sig:
        return False
    expected = bouncer_sign(path, query)
    return hmac.compare_digest(sig, expected)


def app_redirect_url(notion_url: str) -> str:
    """
    Wrap a notion.so page URL in our /r/{path}?sig=… bouncer so tapping it on
    mobile opens the Notion app (notion:// scheme) instead of forcing a browser
    login. The HMAC signature ensures the bouncer only redirects to URLs we
    actually generated — closes the open-redirector hole on our domain.
    Falls back to the original https URL if the prefix doesn't match (safety net).
    """
    if not notion_url.startswith(_NOTION_WEB_PREFIX):
        # Notion can in principle return notion.site / enterprise-domain URLs we don't
        # cover. Log so we know when this path triggers — silent fallback hides UX bugs.
        logger.warning(
            "app_redirect_url: URL does not start with %s — falling back unwrapped: %.120s",
            _NOTION_WEB_PREFIX, notion_url,
        )
        return notion_url
    parsed = urlparse(notion_url)
    # parsed.path starts with "/" — strip leading "/" to mirror the {path:.*} match.
    path = parsed.path.lstrip("/")
    query = parsed.query  # may be ""
    sig = bouncer_sign(path, query)
    bouncer_query = f"{query}&sig={sig}" if query else f"sig={sig}"
    return f"{config.BASE_URL}/r/{path}?{bouncer_query}"


class NotionError(Exception):
    """Base for all Notion-related errors."""


class NotionOAuthError(NotionError):
    """Token exchange with Notion OAuth endpoint failed."""


class NotionUnauthorizedError(NotionError):
    """Access token is invalid or has been revoked. User must reconnect."""


class NotionPageNotFoundError(NotionError):
    """Parent page does not exist or is no longer accessible. User must pick a new one."""


def _extract_title(page: dict) -> str:
    """
    Extract the plain-text title from a Notion page object.
    The property name for the title varies by workspace — scan for type=='title'.
    """
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            if parts:
                return parts[0].get("plain_text", "Untitled")
    return "Untitled"


def _rt(content: str) -> list[dict]:
    """Build a single-element rich_text array with the given content string."""
    return [{"type": "text", "text": {"content": content}}]


def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(text)},
    }


def _todo(text: str) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": _rt(text), "checked": False},
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


async def exchange_token(code: str) -> tuple[str, str]:
    """
    Exchange an OAuth authorisation code for a Notion access token.
    Returns (access_token, workspace_name).
    Raises NotionOAuthError on failure.
    """
    url = "https://api.notion.com/v1/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.NOTION_REDIRECT_URI,
    }
    auth = aiohttp.BasicAuth(login=config.NOTION_CLIENT_ID, password=config.NOTION_CLIENT_SECRET)

    session = await _get_aiohttp_session()
    async with session.post(url, json=payload, auth=auth) as resp:
        body = await resp.json()
        if resp.status != 200:
            # Do NOT log the code — it's single-use and sensitive
            logger.error("Notion token exchange failed: status=%d body=%s", resp.status, body)
            raise NotionOAuthError(f"Token exchange failed ({resp.status}): {body.get('error')}")

    # Defensive: don't blindly index — Notion outage edge cases or proxy oddities
    # have produced 200 responses without the expected fields. Surface as our error type.
    access_token = body.get("access_token")
    if not access_token or not isinstance(access_token, str):
        logger.error("Notion token exchange: 200 OK but missing/invalid access_token: %s", body)
        raise NotionOAuthError("Token exchange returned no access_token")
    workspace_name = body.get("workspace_name") or "Notion"
    logger.info("OAuth token exchange successful (workspace: %s)", workspace_name)
    return access_token, workspace_name


async def search_pages(token: str) -> list[dict]:
    """
    Return up to 10 pages accessible to the integration.
    Each entry: {"id": str, "title": str, "is_top_level": bool}.
    is_top_level is True when the page's parent is the workspace root (not another page).
    Raises NotionUnauthorizedError or NotionError on failure.
    """
    try:
        result = await _notion.search(
            filter={"value": "page", "property": "object"},
            page_size=10,
            auth=token,
        )
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            raise NotionUnauthorizedError("Token invalid or revoked") from e
        raise NotionError(f"search_pages failed: {e.code} — {e}") from e

    pages = [
        {
            "id": page["id"],
            "title": _extract_title(page),
            "is_top_level": page.get("parent", {}).get("type") == "workspace",
        }
        for page in result.get("results", [])
    ]
    logger.info("search_pages returned %d pages (%d top-level)",
                len(pages), sum(1 for p in pages if p["is_top_level"]))
    return pages


async def fetch_child_pages(token: str, parent_id: str) -> list[dict]:
    """
    Return immediate child pages of parent_id (max 8).
    Uses blocks.children.list and filters for type == "child_page" only.
    Raises NotionUnauthorizedError or NotionError on failure.
    """
    try:
        resp = await _notion.blocks.children.list(block_id=parent_id, auth=token)
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            raise NotionUnauthorizedError("Token invalid or revoked") from e
        raise NotionError(f"fetch_child_pages failed: {e.code} — {e}") from e

    children = [
        {"id": block["id"], "title": block["child_page"]["title"]}
        for block in resp.get("results", [])
        if block.get("type") == "child_page"
    ][:8]  # cap at 8 — leaves room for "Save here" + "Back" buttons
    logger.info("fetch_child_pages for %s returned %d child pages", parent_id, len(children))
    return children


_FETCH_PAGE_CAP = 3  # max API pages fetched per call (300 blocks) — covers all realistic cases


async def fetch_child_pages_in_range(
    token: str,
    parent_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    """
    Return child pages of parent_id whose created_time falls within [start_dt, end_dt].
    Both datetimes must be UTC-aware.
    Returns list of dicts with keys: title, url.

    Paginates through Notion's blocks.children.list (max 100 per page) up to
    _FETCH_PAGE_CAP pages. Logs a warning if the cap is hit.

    Raises NotionUnauthorizedError or NotionError on API failure.
    """
    all_blocks: list[dict] = []
    cursor: str | None = None
    pages_fetched = 0

    while pages_fetched < _FETCH_PAGE_CAP:
        kwargs: dict = {"block_id": parent_id, "auth": token}
        if cursor:
            kwargs["start_cursor"] = cursor
        try:
            resp = await _notion.blocks.children.list(**kwargs)
        except APIResponseError as e:
            if e.code == APIErrorCode.Unauthorized:
                raise NotionUnauthorizedError("Token invalid or revoked") from e
            raise NotionError(f"fetch_child_pages_in_range failed: {e.code} — {e}") from e

        all_blocks.extend(resp.get("results", []))
        pages_fetched += 1

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    else:
        logger.warning(
            "fetch_child_pages_in_range: hit %d-page cap for parent %s — some blocks may be missing",
            _FETCH_PAGE_CAP, parent_id,
        )

    results = []
    for block in all_blocks:
        if block.get("type") != "child_page":
            continue
        raw_ts = block.get("created_time", "")
        try:
            created = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Could not parse created_time %r on block %s", raw_ts, block.get("id"))
            continue
        if start_dt <= created <= end_dt:
            page_id = block["id"].replace("-", "")
            results.append({
                "title": block["child_page"]["title"] or "Untitled",
                "url": f"https://notion.so/{page_id}",
            })

    logger.info(
        "fetch_child_pages_in_range for %s: %d pages in range [%s, %s] (fetched %d block pages)",
        parent_id, len(results),
        start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        pages_fetched,
    )
    return results


async def create_page(
    token: str,
    parent_id: str,
    structured: dict,
    transcript: str,
) -> tuple[str, str]:
    """
    Create a child Notion page under parent_id with structured content.
    Returns (page_id, page_url).
    Raises NotionUnauthorizedError if token is revoked.
    Raises NotionPageNotFoundError if parent page is deleted or archived.
    Raises NotionError on all other API failures.
    """
    # Defensive: never trust dict shape — the structured payload comes from Claude
    # or our own _default_structured fallback.
    summary = (structured.get("summary") or "").strip()
    title = (structured.get("title") or "Voice note").strip()

    children: list[dict] = [_heading2("Summary")]
    children.extend(_paragraphs_for(summary))
    children.append(_divider())

    if structured.get("key_points"):
        children.append(_heading2("Key Points"))
        for point in structured["key_points"]:
            children.extend(_bullets_for(point))

    if structured.get("action_items"):
        children.append(_heading2("Action Items"))
        for item in structured["action_items"]:
            children.extend(_todos_for(item))

    if structured.get("decisions"):
        children.append(_heading2("Decisions"))
        for decision in structured["decisions"]:
            children.extend(_bullets_for(decision))

    # Transcript goes in a toggle — full text preserved across paragraph blocks so
    # long voice notes don't lose data. Each paragraph is ≤_CHUNK_SIZE chars.
    transcript_blocks = _paragraphs_for(transcript) or [_paragraph("")]
    children.extend([
        _divider(),
        {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": _rt("Full Transcript"),
                "children": transcript_blocks,
            },
        },
    ])

    # Title: Notion page titles are also rich_text — split if absurdly long.
    title_chunks = _split_for_rich_text(title, _CHUNK_SIZE)
    title_rich_text = [{"type": "text", "text": {"content": chunk}} for chunk in title_chunks]
    properties = {
        "title": {"title": title_rich_text}
    }

    try:
        response = await _notion.pages.create(
            parent={"type": "page_id", "page_id": parent_id},
            properties=properties,
            children=children,
            auth=token,
        )
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            raise NotionUnauthorizedError("Token invalid or revoked") from e
        if e.code == APIErrorCode.ObjectNotFound:
            raise NotionPageNotFoundError("Parent page not found or deleted") from e
        if e.code == APIErrorCode.ValidationError and "archived" in str(e).lower():
            raise NotionPageNotFoundError("Parent page is archived (deleted)") from e
        raise NotionError(f"create_page failed: {e.code} — {e}") from e

    if not isinstance(response, dict):
        raise NotionError(f"create_page: unexpected response type {type(response).__name__}")
    page_id = response.get("id")
    page_url = response.get("url")
    if not page_id or not page_url:
        raise NotionError(f"create_page: response missing id or url (keys: {list(response.keys())})")
    logger.info("Created Notion page: %s", page_id)
    return page_id, page_url


async def append_url_block(
    token: str,
    page_id: str,
    url: str,
    expected_parent_id: str | None = None,
) -> None:
    """
    Append a paragraph block containing url to an existing page.

    If expected_parent_id is provided, we first verify the page's parent matches
    (defence-in-depth: prevents a future bug from appending URLs to the wrong page).

    Raises NotionUnauthorizedError if token is revoked.
    Raises NotionPageNotFoundError if page doesn't exist or parent mismatch.
    Raises NotionError on all other API failures.
    """
    if expected_parent_id is not None:
        try:
            page = await _notion.pages.retrieve(page_id=page_id, auth=token)
        except APIResponseError as e:
            if e.code == APIErrorCode.Unauthorized:
                raise NotionUnauthorizedError("Token invalid or revoked") from e
            if e.code == APIErrorCode.ObjectNotFound:
                raise NotionPageNotFoundError(f"page_id {page_id} not found") from e
            raise NotionError(f"append_url_block parent-check failed: {e.code} — {e}") from e
        actual_parent = page.get("parent", {}).get("page_id")
        if actual_parent != expected_parent_id:
            logger.warning(
                "append_url_block: parent mismatch — page=%s expected_parent=%s actual=%s",
                page_id, expected_parent_id, actual_parent,
            )
            raise NotionPageNotFoundError("Page parent mismatch — refusing to append")

    try:
        await _notion.blocks.children.append(
            block_id=page_id,
            children=[_paragraph(url)],
            auth=token,
        )
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            raise NotionUnauthorizedError("Token invalid or revoked") from e
        if e.code == APIErrorCode.ObjectNotFound:
            raise NotionPageNotFoundError("Page not found when appending URL") from e
        raise NotionError(f"append_url_block failed: {e.code} — {e}") from e
    logger.info("Appended URL block to page %s (url=%.80s)", page_id, url)


async def test_token(token: str) -> bool:
    """Return True if the token is valid, False if unauthorized."""
    try:
        await _notion.users.me(auth=token)
        return True
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            return False
        raise NotionError(f"test_token failed unexpectedly: {e}") from e
