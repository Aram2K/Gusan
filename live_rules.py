"""Live Assistant rules, with no Qt in it (so it is testable without a display).

What the user writes in "Context" is a STANDING INSTRUCTION, not a note: every answer of
the session follows it, and it beats the assistant's defaults on language, format and
length. Some instructions are about EVERYTHING that is said - "translate everything to
French", "write subtitles", "summarise each part", "correct my grammar". For those the
assistant must work on every new piece of the conversation, not only when a question is
heard. This module is the logic of that mode:

    looks_like_rule(text)    a cheap multilingual guess: does this text read like such a
                             rule? (only ever used to OFFER the mode - never to switch it on)
    reconcile(on, new, old)  what the switch must do when the context text changes: turn itself off
                             (and say why, once) when the text is cleared or no longer reads like a
                             rule, say so when a different rule takes over - never stay on silently
    declined_mark(text)      a short mark of a context the user said "No" to (not asked twice)
    scope_text(on)           the plain words for the two modes: "Everything you hear" /
                             "Questions only"
    rule_context(...)        what the model is sent for ONE piece: the standing instructions
                             first, a little earlier talk as background, then the piece
    RuleFeed                 the pieces heard -> groups to send: merged within a couple of
                             seconds, at most MAX_WAITING waiting, the OLDEST dropped when
                             the model is slower than the talk ("catching up")
    RuleBudget               a soft cap on how many requests a session may make, so a long
                             call never eats the day's cloud allowance
    feed_html(entries, ...)  the running feed as HTML: the original muted, its result under
                             it, newest first - escaped, never raw Markdown, no links, no images

The transcript is untrusted data (the system prompt says so); the context is the user's own
text and is trusted for instructions. See ui/live_assist.py for the card.
"""
import hashlib
import html
import re
from collections import deque, namedtuple

import invite_context
import live_context

# ── config keys (both on the card and on the Live Assistant page) ──
RULES_KEY = "live_assist_rules_everything"      # bool: apply the context to everything heard
DECLINED_KEY = "live_assist_rules_declined"     # short mark of the context the user said No to

SCOPE_EVERYTHING = "Everything you hear"
SCOPE_QUESTIONS = "Questions only"
SWITCH_LABEL = "Apply to everything I hear"
OFFER_TEXT = ("This sounds like a rule for everything you hear. Apply it to every sentence?")
OFFER_YES, OFFER_NO = "Yes", "No"
# What the switch says about itself when the text changes under it (calm, said once).
SWITCHED_OFF_EMPTY = "Back to questions only - the context is empty."
SWITCHED_OFF_NOT_RULE = ("Back to questions only. This text doesn't read like a rule for everything "
                         "you hear.")
RULE_CHANGED = "Your rule changed. It applies from the next sentence."

# ── the numbers behind the mode ──
MERGE_S = 2.5               # pieces heard within this long of each other go as one
MAX_GROUP_CHARS = 600       # ...or until the group is this long
MAX_WAITING = 3             # groups waiting for the model; more and the oldest is dropped
MIN_GAP_S = 1.0             # at least this long between two requests
PIECE_CHARS = 900           # the most of one group the model is sent
BACKGROUND_CHARS = 500      # earlier talk, for names and meaning only
SESSION_CAP = 400           # requests a session may make before it pauses (Resume = 400 more)
FAILS_BEFORE_PAUSE = 3      # this many errors in a row pause the mode
FEED_KEEP = 40              # entries kept on the card
SRC_SHOWN = 180             # characters of the original shown above its result
RESULT_CHARS = 6000         # the most of one result the feed draws


def scope_text(everything):
    return SCOPE_EVERYTHING if everything else SCOPE_QUESTIONS


def declined_mark(text):
    """A short, stable mark of a context text ("" for none): remembered when the user says
    No to the offer, so the same text never asks twice (a changed text may)."""
    t = " ".join((text or "").split()).lower()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:12] if t else ""


# ── does this read like a rule for everything heard? ──
# A cheap keyword guess in the app's main languages. Strong words (an imperative that is
# already a rule) are enough; a noun like "translation" or "subtitles" needs a word for
# "everything"/"each" as well, so "Call with the translation team" does not count. It only
# ever makes an OFFER the user can refuse.
_STRONG = {
    "en": r"translate|translates|translating|subtitle|proofread\w*|interpret (?:everything|it|what|for)|"
          r"transcribe (?:everything|it|what)|(?:fix|correct) (?:my |the |every |each |all )?"
          r"(?:grammar|english|spelling|mistakes|sentences?|wording|phrasing)",
    "fr": r"traduis\w*|traduire|traduisez|sous-?titre\w*|corrige\w* (?:mes|ma|mon|les|chaque|tout)|"
          r"reformule\w*",
    "ru": r"перевод\w*|переведи\w*|переводить|субтитр\w*|исправляй|исправь\w*|переформулир\w*",
    "hy": r"թարգմանի\w*|թարգմանե\w*|թարգմանել|ենթագր\w*|ուղղի\w*",
    "de": r"übersetz\w*|uebersetz\w*|untertitel\w*|dolmetsch\w*|korrigier\w*",
    "es": r"traduc\w*|traduz\w*|traduci\w*|subtitul\w*|corrig\w*|corrij\w*",
    "it": r"tradu\w*|sottotitol\w*|corregg\w*",
    "pt": r"tradu\w*|legend\w*|corrij\w*",
    "tr": r"çevir\w*|altyazı\w*|düzelt\w*",
    "ar": r"ترجم\w*|صحح\w*",
}
_WEAK = {
    "en": r"translation|translations|subtitles|captions?|summari[sz]\w*|summary|repeat\w*|rephras\w*|"
          r"paraphras\w*|interpret\w*|transcri\w*|rewrite\w*",
    "fr": r"traduction\w*|résum\w*|resum\w*|répète\w*|repete\w*|reformul\w*|transcri\w*",
    "ru": r"перевод\w*|резюм\w*|пересказ\w*|повтор\w*|транскрип\w*|суммир\w*",
    "hy": r"թարգմանություն\w*|ամփոփ\w*|կրկնի\w*|կրկնել",
    "de": r"übersetzung\w*|zusammenfass\w*|wiederhol\w*|untertitel\w*",
    "es": r"traducci\w*|resum\w*|repit\w*|repite\w*",
    "it": r"traduzion\w*|riassum\w*|ripet\w*",
    "pt": r"tradu\w*|resum\w*|repit\w*",
    "tr": r"çeviri\w*|özet\w*|tekrar\w*",
    "ar": r"ترجمة\w*|لخص\w*|كرر\w*",
}
_QUANT = {
    "en": r"every ?thing|every\b|each\b|all (?:of )?(?:it|that|what|the)|whatever|anything|all the time",
    "fr": r"tout\b|toute?s?\b|chaque|tout ce",
    "ru": r"всё|все\b|всего|каждое|каждую|каждый|каждого|всем",
    "hy": r"ամեն|բոլոր|ամբողջ|յուրաքանչյուր",
    "de": r"alles\b|alle\b|jede\w*|jeden",
    "es": r"todo\b|toda[sn]?\b|todos\b|cada\b",
    "it": r"tutto\b|tutt[eoi]\b|ogni\b",
    "pt": r"tudo\b|todo[sa]?\b|cada\b",
    "tr": r"hepsi\w*|her\b|bütün\w*",
    "ar": r"كل\b|جميع",
}


_STRONG_RE = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join("(?:%s)" % p for p in _STRONG.values()),
                        re.I | re.U)
_WEAK_RE = re.compile(r"(?<!\w)(?:%s)" % "|".join("(?:%s)" % p for p in _WEAK.values()), re.I | re.U)
_QUANT_RE = re.compile(r"(?<!\w)(?:%s)" % "|".join("(?:%s)" % p for p in _QUANT.values()), re.I | re.U)


def looks_like_rule(text):
    """True when ``text`` reads like a rule for everything that is heard: translate it,
    write subtitles, summarise each part, correct my grammar, repeat what is said... in
    English, French, Russian, Armenian, German, Spanish, Italian, Portuguese, Turkish or
    Arabic. False for a plain description of a call and for a text with no such word."""
    t = " ".join((text or "").split())
    if len(t) < 6:
        return False
    if _STRONG_RE.search(t):
        return True
    return bool(_WEAK_RE.search(t) and _QUANT_RE.search(t))


def reconcile(on, new_text, old_text=""):
    """What "Apply to everything I hear" must do now that the context text is ``new_text`` (it was
    ``old_text``): (turn_off, message). The mode is a standing rule for every sentence, so it must not
    outlive its rule:
      * off already                                   -> (False, "")
      * the text is empty                              -> (True, SWITCHED_OFF_EMPTY)
      * the text no longer reads like a rule           -> (True, SWITCHED_OFF_NOT_RULE)
      * a different text that still reads like one     -> (False, RULE_CHANGED): it stays on, but
        never silently - the new rule is what is applied from the next sentence
      * the same text                                   -> (False, "")
    The guess is looks_like_rule (cheap, multilingual); a switch the person turned on by hand stays
    theirs only as long as the text keeps reading like a rule - they can turn it on again."""
    if not on:
        return False, ""
    new = " ".join((new_text or "").split())
    old = " ".join((old_text or "").split())
    if not new:
        return True, SWITCHED_OFF_EMPTY
    if not looks_like_rule(new):
        return True, SWITCHED_OFF_NOT_RULE
    if new != old:
        return False, RULE_CHANGED
    return False, ""


# ── what the model is sent for one piece ──
def rule_context(piece, recent="", session_context="", title="", attendees="", output_lang="auto"):
    """The text the model gets for ONE piece in "everything you hear" mode: the user's
    standing instructions first (the same block as every other answer), the meeting line,
    a little earlier talk as background (never repeated), then the piece to work on.
    Documents, the screen and earlier answers are not sent: this runs on every sentence,
    so it stays small."""
    parts = []
    block = live_context.standing_block(session_context)
    if block:
        parts.append(block)
    meeting = live_context.defuse_header(invite_context.live_lines(title, attendees))
    if meeting:
        parts.append(meeting)
    background =live_context.defuse(" ".join((recent or "").split())[-BACKGROUND_CHARS:])
    if background:
        parts.append("Earlier in the conversation (background only - never repeat it):\n"
                     + background)
    piece = live_context.defuse(" ".join((piece or "").split())[-PIECE_CHARS:])
    parts.append("Latest piece of the conversation (apply the standing instructions to this "
                 "piece, and only to it):\n" + (piece or "(nothing)"))
    line = live_context.respond_line(output_lang, bool(block))
    if line:
        parts.append(line)
    return "\n\n".join(parts)


# ── the pieces heard -> groups to send ──
Group = namedtuple("Group", "id text first_at skipped recent")
Group.__doc__ = """One request's worth of speech: ``text`` (pieces joined), when its first piece
was heard, how many groups were dropped just before it (``skipped``), and the talk that came
before it (``recent``, background for the model)."""


class RuleFeed:
    """Pieces in, groups out. Used on the GUI thread only.

    * ``add(text, now)``: a freshly transcribed piece. Pieces heard within MERGE_S of the
      group's first piece (and while it is shorter than MAX_GROUP_CHARS) join one group.
    * ``take(now, idle)``: the next group to send, or None. Waiting groups go oldest
      first; when the model is idle the open group goes at once (no waiting for more).
    * More than MAX_WAITING groups waiting: the OLDEST are dropped - the model is slower
      than the talk, and the newest speech matters most. ``catching_up`` says so until
      the line has cleared; ``skipped`` counts what was dropped (shown to the user).
    """

    def __init__(self, window_s=MERGE_S, max_chars=MAX_GROUP_CHARS, max_waiting=MAX_WAITING):
        self.window_s = float(window_s)
        self.max_chars = int(max_chars)
        self.max_waiting = int(max_waiting)
        self._open = []                  # [(text)] of the group being built
        self._open_at = 0.0
        self._open_chars = 0
        self._open_recent = ""           # what was said before the open group began
        self._said = ""                  # everything added so far (its tail only)
        self._waiting = deque()
        self._next_id = 1
        self._owed = 0                   # dropped groups not yet reported with a group
        self.dropped = 0                 # dropped since the line was last clear
        self.dropped_total = 0
        self.catching_up = False

    # -- feeding --
    def add(self, text, now):
        text = " ".join((text or "").split())
        if not text:
            return
        if self._open and now - self._open_at >= self.window_s:
            self._seal()
        if not self._open:
            self._open_at = now
            self._open_recent = self._said[-BACKGROUND_CHARS:].lstrip()
        self._open.append(text)
        self._open_chars += len(text) + 1
        self._said = (self._said + " " + text)[-4 * BACKGROUND_CHARS:]
        if self._open_chars >= self.max_chars:
            self._seal()

    def poll(self, now):
        """Close the open group once it is old enough (call about once a second)."""
        if self._open and now - self._open_at >= self.window_s:
            self._seal()

    def _seal(self):
        if not self._open:
            return
        text = " ".join(self._open)
        group = Group(self._next_id, text, self._open_at, 0, self._open_recent)
        self._next_id += 1
        self._open, self._open_chars = [], 0
        self._waiting.append(group)
        while len(self._waiting) > self.max_waiting:
            self._waiting.popleft()
            self._owed += 1
            self.dropped += 1
            self.dropped_total += 1
        self._refresh_state()

    # -- taking --
    def take(self, now, idle=True):
        """The next group to send, or None. ``idle``: the model is not busy."""
        self.poll(now)
        if not idle:
            return None
        if not self._waiting and self._open:
            self._seal()
        if not self._waiting:
            self._refresh_state()
            return None
        group = self._waiting.popleft()
        if self._owed:
            group = group._replace(skipped=self._owed)
            self._owed = 0
        self._refresh_state()
        return group

    def _refresh_state(self):
        if not self._waiting and not self._open:
            self.dropped = 0
        self.catching_up = bool(self.dropped or len(self._waiting) >= 2)

    def pending(self):
        """How many groups are waiting (not counting the one being built)."""
        return len(self._waiting)

    def has_work(self):
        return bool(self._waiting or self._open)

    def clear(self):
        self._open, self._open_chars = [], 0
        self._waiting.clear()
        self._owed = self.dropped = 0
        self.catching_up = False


class RuleBudget:
    """A soft cap on requests per session (Resume starts it again): one request per
    sentence adds up, and the cloud has a daily allowance shared with everything else."""

    def __init__(self, cap=SESSION_CAP):
        self.cap = int(cap)
        self.used = 0

    def spend(self):
        self.used += 1

    @property
    def exhausted(self):
        return self.used >= self.cap

    def reset(self):
        self.used = 0


# ── the running feed, as HTML ──
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_CODE = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|$)", re.S)
_BULLET = re.compile(r"^[ \t]*[-*•][ \t]+", re.M)


def inline_html(text):
    """A model's reply as safe HTML for the feed: escaped first (so nothing in it is ever a
    tag, a link or an image), then **bold**, `code`, ``` fences and "- " bullets are shown
    as such instead of as raw Markdown characters; line breaks stay."""
    raw = (text or "").strip()[:RESULT_CHARS]       # (a result is a few lines: never a page-long one, whose
    pre = []                                        # fences and stars would make the patterns below crawl)

    def _stash(m):
        pre.append(html.escape(m.group(1).strip("\n"), quote=False))
        return "\x00%d\x00" % (len(pre) - 1)

    raw = _FENCE.sub(_stash, raw)
    out = html.escape(raw, quote=False)
    out = _BULLET.sub("• ", out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = out.replace("\n", "<br>")
    for i, block in enumerate(pre):
        out = out.replace("\x00%d\x00" % i, "<pre>%s</pre>" % block)
    return out


Entry = namedtuple("Entry", "id src out state note")
Entry.__doc__ = """One line of the feed: ``src`` (what was heard), ``out`` (its result so far),
``state`` ("working" | "done" | "failed" | "skipped") and a short ``note`` (why it failed)."""


def _cut(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def feed_html(entries, colors):
    """The feed, newest first, as HTML for the card's read-only box. ``entries``: Entry
    values, newest first. ``colors``: {"text", "muted", "faint"} hex strings."""
    text, muted, faint = colors["text"], colors["muted"], colors["faint"]
    blocks = []
    for e in entries:
        src = html.escape(_cut(e.src, SRC_SHOWN), quote=False)
        if e.state in ("failed", "skipped"):
            note = html.escape(e.note or ("Skipped to catch up." if e.state == "skipped"
                                          else "Couldn't apply the rule to this part."), quote=False)
            body = '<span style="color:%s"><i>%s</i></span>' % (faint, note)
        elif e.out and e.out.strip():
            body = '<span style="color:%s">%s</span>' % (text, inline_html(e.out))
        else:
            body = '<span style="color:%s">…</span>' % faint
        head = ('<div style="color:%s;font-size:11px">%s</div>' % (muted, src)) if src else ""
        size = "12px" if e.state in ("failed", "skipped") else "13.5px"        # a note is quieter than a result
        blocks.append('<div style="margin:0 0 9px 0">%s<div style="font-size:%s">%s</div></div>'
                      % (head, size, body))
    return "".join(blocks)
