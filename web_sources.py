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
# The server's own patterns (supabase/functions/smart-action: explicitWebQuery, smartWebQuery - tests/test_web_search
# keeps them equal), so the pill can say "Searching the web" before the answer. A Smart Action is mostly dictation
# ("the release is today"): it searches only when the request starts with a search command ("search the web for...",
# "look up...", "google...", or "find ... online"), or needs live figures - never on a word like "today" alone.
SMART_ASK = '^(?:(?:please|(?:can|could|would)\\s+you)\\s+)*(?:search\\s+(?:the\\s+)?(?:web|online|internet)(?:\\s+for)?|google|look\\s+up(?:\\s+(?:on\\s+)?(?:the\\s+)?(?:web|online|internet))?|(?:check|find)\\s+online|(?:can|could|would)\\s+you\\s+(?:please\\s+)?search(?:\\s+for)?|search\\s+for|browse(?:\\s+(?:the\\s+)?(?:web|online|internet))?|what\\s+does\\s+the\\s+internet\\s+say\\s+about|cherche(?:r|z)?\\s+sur\\s+internet|busca(?:r)?\\s+en\\s+internet|such(?:e)?\\s+im\\s+internet)\\b'
SMART_TRAILING_WEB = '\\s+(?:on\\s+(?:the\\s+)?(?:web|internet|google)|online|sur\\s+(?:internet|le\\s+web|google)|en\\s+(?:internet|ligne|l[ií]nea|google)|im\\s+(?:internet|netz)|auf\\s+google)[.!?]*$'
SMART_ASK_TRAILING = '^(?:(?:please|(?:can|could|would)\\s+you)\\s+)*(?:search(?:\\s+for)?|find|cherche(?:r|z)?|busca(?:r)?|such(?:e)?)\\b'
SMART_POLITE = "[,\\s]*(?:please|thanks|thank you|s'il vous plaît|por favor|bitte)[.!?\\s]*$"
SMART_LIVE = (r"\b(?:exchange rate|interest rate|inflation|forecast|market cap)\b|"
              r"\bcurrent\s+(?:[\w/-]+\s+){0,4}(?:rate|price|version|status|CEO|president|weather|score)\b")
_ASK_RE = re.compile(SMART_ASK, re.I)
_TRAILING_RE = re.compile(SMART_TRAILING_WEB, re.I)
_ASK_TRAILING_RE = re.compile(SMART_ASK_TRAILING, re.I)
_POLITE_RE = re.compile(SMART_POLITE, re.I)
_LIVE_RE = re.compile(SMART_LIVE, re.I)


def smart_search_wanted(text):
    """True when a Smart Action's spoken request will search the web (if the person turned web search on)."""
    q = " ".join(str(text or "").split())
    if len(q) < 8:
        return False
    command = _POLITE_RE.sub("", q).strip()
    if _ASK_RE.search(command) or (_TRAILING_RE.search(command) and _ASK_TRAILING_RE.search(command)):
        return True
    return bool(_LIVE_RE.search(q))
