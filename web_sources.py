"""Safe, compact source links shared by the live answer surfaces; and when a Smart Action searches the web."""
import re
from html import escape
from urllib.parse import urlsplit

from action_api import sanitize_web_sources


def sources_html(sources):
    rows = sanitize_web_sources(sources)
    if not rows:
        return ""
    links = []
    for n, row in enumerate(rows, 1):
        domain = urlsplit(row["url"]).hostname or ""
        links.append('[%d] <a href="%s">%s</a> — %s' % (
            n, escape(row["url"], quote=True), escape(row["title"] or domain), escape(domain)))
    return "<b>Sources</b><br>" + "<br>".join(links)


# ── Smart Actions: when to search ────────────────────────────────────────────────────────────
# The same two patterns as the server's smartWebQuery (supabase/functions/smart-action, tests/test_web_search keeps
# them equal). A Smart Action is mostly dictation ("the release is today"): it searches only when the person asks for
# it, or the request needs live figures - never on a word like "today" or "latest" alone.
SMART_ASK = (r"\b(?:search\s+(?:the\s+)?(?:web|online|internet)(?:\s+for)?|google|look\s+up|(?:check|find)\s+online|"
             r"can\s+you\s+search(?:\s+for)?|search\s+for|browse|what\s+does\s+the\s+internet\s+say\s+about|"
             r"cherche(?:r|z)?\s+sur\s+internet|busca(?:r)?\s+en\s+internet|such(?:e)?\s+im\s+internet)\b")
SMART_LIVE = (r"\b(?:exchange rate|interest rate|inflation|forecast|market cap)\b|"
              r"\bcurrent\s+(?:[\w/-]+\s+){0,4}(?:rate|price|version|status|CEO|president|weather|score)\b")
_ASK_RE = re.compile(SMART_ASK, re.I)
_LIVE_RE = re.compile(SMART_LIVE, re.I)


def smart_search_wanted(text):
    """True when a Smart Action's spoken request will search the web (if the person turned web search on)."""
    q = " ".join(str(text or "").split())
    return len(q) >= 8 and bool(_ASK_RE.search(q) or _LIVE_RE.search(q))
