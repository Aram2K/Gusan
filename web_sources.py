"""Safe, compact source links shared by the live answer surfaces."""
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
