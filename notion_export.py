"""Send finished meeting notes to a Notion page.

Uses Notion's public REST API with a bearer token. The token is either the
access token Gusan gets when the user presses "Connect Notion" (OAuth, see
notion_connect.py) or an *internal integration* secret the user pasted
themselves - the REST call is identical, only the wording of a failure differs
(``oauth=True`` below). (An MCP server is the analogous plumbing for LLM
clients like Claude; for an app pushing pages INTO Notion, the REST API is the
direct wire.)

What the user must do once on the "own integration" path, and the two errors
it explains:
  1. Create an integration at notion.so/my-integrations and copy the secret
     ("ntn_..." / legacy "secret_..."). A wrong token -> 401.
  2. Share the target page with that integration (page ... menu -> Connections).
     A page that exists but was never shared -> 404 from Notion, which is why
     the 404 message here talks about sharing, not existence.
With OAuth the page was either made by Gusan or picked by the user on Notion's
own consent screen, so a 404 later means "pick another page".

Failures are typed (:class:`NotionError` and two subclasses) and carry a
``code`` (auth | no_access | rate | offline | other), the HTTP ``status`` and
``retry_after`` seconds, so callers decide what to do (refresh the token,
recreate the page, tell the user) without reading the text. The text itself is
always our own plain sentence: nothing Notion or ``requests`` said, and never
a token, goes into a message or the log.

Pure stdlib + requests. No Qt. The only project import is app_info (the
product name), so this stays unit-testable on its own.
"""
import logging
import re
import time

import requests

from app_info import APP_NAME

logger = logging.getLogger("transcribe.notion")

API_BASE = "https://api.notion.com/v1"
# One version for every call (the edge function sends the same one). The token,
# refresh and revoke references list it, and workspace-level page creation is
# documented only against it. If a live call objects, set both to "2022-06-28".
NOTION_VERSION = "2026-03-11"
MAX_RICH_LEN = 1800             # Notion caps rich_text content at 2000 chars
MAX_CHILDREN_PER_REQ = 100      # Notion caps children blocks per request
HTTP_TIMEOUT = 30
RETRY_AFTER_MAX = 20            # a 429 asking for more than this is reported, not waited out

MAX_TOKEN_LEN = 2000

OFFLINE_TEXT = "Couldn't reach Notion. Check your internet."
AUTH_AGAIN_TEXT = "Notion needs to be connected again."
SECRET_REJECTED_TEXT = ("Notion rejected the token - re-copy the integration "
                        "secret from notion.so/my-integrations.")


class NotionError(RuntimeError):
    """A Notion call failed; ``str(e)`` is a plain sentence, never raw output.

    ``code``: auth | no_access | rate | offline | other. ``status`` is the HTTP
    status (None when Notion was never reached), ``retry_after`` the seconds
    Notion asked us to wait (429 only) and ``url`` the page that DID get
    created when only the later part of a long upload failed.
    """
    code = "other"
    status = None
    retry_after = None
    url = ""

    def __init__(self, message="", status=None, code=None, retry_after=None, url=""):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        if retry_after is not None:
            self.retry_after = retry_after
        if url:
            self.url = url


class NotionAuthError(NotionError):
    """401: the token is wrong, revoked or (OAuth) expired."""
    code = "auth"
    status = 401


class NotionNotFoundError(NotionError):
    """404: the page is gone, or was never shared with this token."""
    code = "no_access"
    status = 404


def extract_page_id(raw):
    """A Notion page id from a pasted URL or a bare id, dashed or not.

    Notion URLs end with a 32-hex-char id, usually glued to a title slug:
    https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef
    Returns the dashed UUID form, or None when nothing id-shaped is present.
    """
    if not raw:
        return None
    # Drop query AND fragment: a URL copied after following an anchor looks
    # like .../Slug-<pageid>#<blockid>, and the block id would otherwise win
    # the last-match rule below.
    text = raw.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    candidates = re.findall(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        r"|[0-9a-fA-F]{32}",
        text)
    if not candidates:
        return None
    # A workspace URL can carry several id-shaped runs; the page id is the last.
    h = candidates[-1].replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def is_connected(cfg):
    """True when a token AND a destination page are saved (any connection
    mode). The one test the Settings row, Send to Notion and auto-send share."""
    cfg = cfg or {}
    token = (cfg.get("notion_api_key") or "").strip()
    return bool(token and extract_page_id(cfg.get("notion_parent_page")))


def connection_mode(cfg):
    """How Notion was connected: "oauth" (the Connect button), "own" (a pasted
    integration secret) or "" (no token). A token with no saved mode is a
    setup from before OAuth existed, so it reads as "own"."""
    cfg = cfg or {}
    if not (cfg.get("notion_api_key") or "").strip():
        return ""
    return "oauth" if cfg.get("notion_auth_mode") == "oauth" else "own"


# Everything a Notion connection leaves in the settings: what Disconnect, and
# another Gusan account signing in, put back to nothing. The automatic-send
# switch goes back to off: a new connection starts quiet. (The connection's
# owner stamp, notion_owner, is the controller's own: main.py clears it.)
BLANK_SETTINGS = {
    "notion_api_key": "", "notion_refresh_token": "", "notion_auth_mode": "", "notion_workspace_name": "",
    "notion_parent_page": "", "notion_parent_title": "", "notion_hub_page": "",
    "notion_auto_send": False, "notion_reconnect": False,
}


def _rich(text):
    """Rich-text array, chunked under Notion's per-item length cap."""
    text = text or ""
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[i:i + MAX_RICH_LEN]}}
            for i in range(0, len(text), MAX_RICH_LEN)]


def _block(block_type, text):
    return {"object": "block", "type": block_type,
            block_type: {"rich_text": _rich(text)}}


def markdown_to_blocks(md):
    """Translate the notes' markdown subset into Notion blocks.

    Handles what our summaries actually emit: ## / ### headings, - [ ] todos,
    -/* bullets, numbered items, and plain paragraphs. Anything else stays a
    paragraph - never dropped.
    """
    blocks = []
    for raw_line in (md or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            blocks.append(_block("heading_3", line[4:]))
        elif line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_block("heading_1", line[2:]))
        elif (m := re.match(r"^[-*] \[([ xX])\] (.*)$", line)):
            blocks.append({"object": "block", "type": "to_do",
                           "to_do": {"rich_text": _rich(m.group(2).strip()),
                                     "checked": m.group(1).lower() == "x"}})
        elif line.startswith(("- ", "* ")):
            blocks.append(_block("bulleted_list_item", line[2:]))
        elif re.match(r"^\d+[.)] ", line):
            blocks.append(_block("numbered_list_item",
                                 line.split(" ", 1)[1] if " " in line else line))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


def transcript_blocks(transcript):
    """One paragraph per transcript line, under a Transcript heading."""
    if not (transcript or "").strip():
        return []
    blocks = [_block("heading_2", "Transcript")]
    for line in transcript.split("\n"):
        line = line.strip()
        if line:
            blocks.append(_block("paragraph", line))
    return blocks


def _headers(token):
    return {"Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


def _retry_after(resp):
    """Seconds from the Retry-After header, or None when absent or unreadable."""
    try:
        return max(0.0, float(resp.headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def _request(method, url, token, **kw):
    """One Notion HTTP call with the standard headers.

    Waits and retries ONCE when Notion answers 429/529 with a Retry-After of
    at most RETRY_AFTER_MAX seconds; anything else comes back as it is for
    :func:`_raise_for`. A network failure raises NotionError(code "offline")
    with a fixed sentence (the exception text holds addresses and changes on
    every try, so it only goes to the log). ``requests.<method>`` is looked up
    on each call so tests can patch it.
    """
    kw.setdefault("timeout", HTTP_TIMEOUT)
    send = getattr(requests, method.lower())
    for attempt in (0, 1):
        try:
            resp = send(url, headers=_headers(token), **kw)
        except requests.RequestException as e:
            logger.info("Notion %s: couldn't connect (%s)", method.lower(), type(e).__name__)
            raise NotionError(OFFLINE_TEXT, code="offline") from e
        if attempt == 0 and resp.status_code in (429, 529):
            wait = _retry_after(resp)
            if wait is not None and wait <= RETRY_AFTER_MAX:
                time.sleep(wait)
                continue
        break
    return resp


def _json(resp):
    """The response body as a dict ({} when it is empty or not JSON)."""
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _raise_for(resp, oauth=False):
    """Raise the right NotionError for a failed response (nothing for 2xx/3xx).

    The default wording is for the pasted-secret path and talks about the
    secret and Connections; ``oauth=True`` never mentions either, because the
    user never saw a secret.
    """
    status = resp.status_code
    if status < 400:
        return
    # Notion's own error code ("validation_error", ...) is a fixed word, so it
    # is safe to log; its message is free text and stays out of both.
    logger.info("Notion answered %s (%s)", status, str(_json(resp).get("code") or "")[:40])
    if status == 401:
        raise NotionAuthError(AUTH_AGAIN_TEXT if oauth else SECRET_REJECTED_TEXT)
    if status == 404:
        raise NotionNotFoundError(
            f"{APP_NAME} can't reach that Notion page anymore." if oauth else
            "Notion can't see that page. Open the page in Notion "
            "-> ⋯ menu -> Connections -> add your integration, "
            "then try again.")
    if status == 403:
        raise NotionError(
            f"Notion won't let {APP_NAME} add pages there." if oauth else
            "Notion won't let the integration add pages there - give it "
            "\"Insert content\" in its settings, and share the page with it.",
            status=403, code="no_access")
    if status in (429, 529):
        raise NotionError("Notion rate limit hit - wait a moment and try again.",
                          status=status, code="rate", retry_after=_retry_after(resp))
    if status >= 500:
        raise NotionError(f"Notion had a problem (error {status}) - try again in a minute.",
                          status=status)
    raise NotionError(f"Notion couldn't take that (error {status}).", status=status)


def clean_token(token, oauth=False):
    """The token as it can be sent (trimmed), or a NotionAuthError for one that
    is missing or cannot be a Notion token (spaces or non-ASCII inside, absurdly
    long): ``requests`` fails on those with an error that would read as "no
    internet", and no request should leave the computer with them."""
    token = (token or "").strip()
    if not token:
        raise NotionAuthError(AUTH_AGAIN_TEXT if oauth else
                              "Add your Notion integration token first.")
    if not token.isascii() or len(token) > MAX_TOKEN_LEN or re.search(r"\s", token):
        raise NotionAuthError(AUTH_AGAIN_TEXT if oauth else SECRET_REJECTED_TEXT)
    return token


def call(method, path, token, *, json=None, oauth=False, timeout=HTTP_TIMEOUT):
    """One Notion API call (``path`` like "/search"); returns the parsed JSON
    object ({} for an empty body). Raises NotionError / NotionAuthError /
    NotionNotFoundError."""
    kw = {"timeout": timeout}
    if json is not None:
        kw["json"] = json
    resp = _request(method, f"{API_BASE}{path}", clean_token(token, oauth), **kw)
    _raise_for(resp, oauth=oauth)
    return _json(resp)


def publish_meeting(token, parent_page, title, notes_md, transcript="", meta_line="",
                    *, oauth=False):
    """Create a Notion page under ``parent_page`` and return its URL.

    Never partial-fails silently: the page is created with the first batch of
    blocks, and remaining batches are appended; an append error still leaves a
    valid page, so the error carries the URL that DID get created (``.url``).
    ``oauth`` picks the failure wording (see :func:`_raise_for`).
    """
    token = clean_token(token, oauth)
    page_id = extract_page_id(parent_page)
    if not page_id:
        raise NotionError("That doesn't look like a Notion page link or id.",
                          code="no_access")

    blocks = []
    if meta_line:
        blocks.append(_block("paragraph", meta_line))
    blocks.extend(markdown_to_blocks(notes_md))
    blocks.extend(transcript_blocks(transcript))
    if not blocks:
        raise NotionError("Nothing to send yet - generate the notes first.")

    first, rest = blocks[:MAX_CHILDREN_PER_REQ], blocks[MAX_CHILDREN_PER_REQ:]
    resp = _request(
        "post", f"{API_BASE}/pages", token,
        json={
            "parent": {"page_id": page_id},
            "properties": {"title": {"title": _rich(title or "Meeting notes")}},
            "children": first,
        })
    _raise_for(resp, oauth=oauth)
    data = _json(resp)
    new_id, url = data.get("id"), data.get("url", "")

    for i in range(0, len(rest), MAX_CHILDREN_PER_REQ):
        batch = rest[i:i + MAX_CHILDREN_PER_REQ]
        time.sleep(0.34)        # Notion allows ~3 requests/second
        try:
            r = _request("patch", f"{API_BASE}/blocks/{new_id}/children", token,
                         json={"children": batch})
            _raise_for(r, oauth=oauth)
        except NotionError as e:
            # A plain NotionError on purpose (not NotionAuthError): the page
            # exists, so a caller that refreshes the token and sends again on
            # an auth error must not do that and make a second page.
            raise NotionError(
                f"Page created ({url}) but part of the transcript failed to "
                f"upload: {e}", status=e.status, code=e.code,
                retry_after=e.retry_after, url=url) from e
    return url
