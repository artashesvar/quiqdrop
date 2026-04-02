# Notion API integration — OAuth token exchange, page search, page creation
import logging
from datetime import datetime, timezone

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

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, auth=auth) as resp:
            body = await resp.json()
            if resp.status != 200:
                # Do NOT log the code — it's single-use and sensitive
                logger.error("Notion token exchange failed: status=%d body=%s", resp.status, body)
                raise NotionOAuthError(f"Token exchange failed ({resp.status}): {body.get('error')}")

    access_token: str = body["access_token"]
    workspace_name: str = body.get("workspace_name", "Notion")
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
    # Truncate transcript to stay within Notion's 2000-char rich_text limit
    if len(transcript) > _RICH_TEXT_LIMIT - 10:
        logger.warning(
            "Transcript truncated from %d to %d chars for Notion block",
            len(transcript), _RICH_TEXT_LIMIT - 10,
        )
        transcript = transcript[: _RICH_TEXT_LIMIT - 10] + "…"

    children: list[dict] = [
        _heading2("Summary"),
        _paragraph(structured["summary"]),
        _divider(),
    ]

    if structured.get("key_points"):
        children.append(_heading2("Key Points"))
        for point in structured["key_points"]:
            children.append(_bullet(point))

    if structured.get("action_items"):
        children.append(_heading2("Action Items"))
        for item in structured["action_items"]:
            children.append(_todo(item))

    if structured.get("decisions"):
        children.append(_heading2("Decisions"))
        for decision in structured["decisions"]:
            children.append(_bullet(decision))

    # Transcript goes in a toggle — keeps the page scannable; full text accessible on demand
    children.extend([
        _divider(),
        {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": _rt("Full Transcript"),
                "children": [_paragraph(transcript)],
            },
        },
    ])

    properties = {
        "title": {"title": _rt(structured["title"])}
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

    page_id: str = response["id"]
    page_url: str = response["url"]
    logger.info("Created Notion page: %s", page_id)
    return page_id, page_url


async def test_token(token: str) -> bool:
    """Return True if the token is valid, False if unauthorized."""
    try:
        await _notion.users.me(auth=token)
        return True
    except APIResponseError as e:
        if e.code == APIErrorCode.Unauthorized:
            return False
        raise NotionError(f"test_token failed unexpectedly: {e}") from e
