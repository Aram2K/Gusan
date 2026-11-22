"""Invite friends: the client of the server's "invite" function, and everything about invites that needs no Qt.

The program (the founder, 2026-11-18/19): every signed-in person has a personal link and code. A friend counts as
soon as they sign up and join (no days of use); 3 friends = 1 month of Pro to claim, every 3 friends another month,
at most 12 a year. A friend gets 2 weeks of Pro when they join. An account can enter a friend's code once, any
time, until it has joined. A subscriber's claimed month is a credit on the subscription: the next bill is free.

What is here (the Qt side is ui/invite_hub.py; the pages are ui/pages/invite.py and ui/pages/invite_join.py):

* the server's four actions, blocking (call them on a worker thread, never on the GUI thread):
      fetch_me(auth, device=False)       -> the normalised "me" answer, or None
      fetch_hint(auth)                   -> {"found", "name", "code"}
      join(auth, code, source)           -> {"ok": True, "name", "gift_ends_at"} | {"ok": False, "reason"}
      claim(auth, reward_id)             -> {"ok": True, "status": "active" | "next_bill_free", ...}
                                            | {"ok": False, "reason"}
  Every failure is quiet: a reason ("network", "signed_out", "server" or the server's own), never an exception.
* device_hash(): a one-way code of this computer - sha256("gusan-invite-v1:" + the machine id). The machine id
  itself (Windows MachineGuid, the Mac's IOPlatformUUID, else a random id kept in the data folder) never leaves it.
* parse_code(text): a full link or a bare code, any case -> the code ("" when it is neither); display_code(code)
  -> "ARAM-7K2Q" (codes are shown upper-case).
* share_text / share_urls: the short message (the link and the code) and the WhatsApp / Telegram / Email / X /
  LinkedIn links.
* the local cache (invite.json in the data folder, per account): the last "me" answer, so the page and the sidebar
  dots show at once, plus the once-a-day device check (device_due) and the friends already announced.
* view(me, plan): what the page, the sidebar and the tray show (slots, ticket, lead line, dots).
* join_error / claim_error: every reason as one plain sentence.

Privacy: only when someone joins an invite (and once a day when an inviter opens the page) - a one-way code of
this computer. Never what anyone says or types.
"""

import hashlib
import logging
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

import storage

logger = logging.getLogger("transcribe")

INVITE_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/invite"
LINK_HOST = "gusan.aibuben.xyz"
LINK_BASE = "https://%s/i/" % LINK_HOST
TIMEOUT = (4, 8)                    # (connect, read) seconds: an invite is never worth a long wait
CACHE_FILE = "invite.json"
HASH_PREFIX = "gusan-invite-v1:"

FRIENDS_NEEDED = 3                  # friends for one month of Pro
GIFT_DAYS = 14                      # a friend's 2 weeks of Pro
MONTHS_A_YEAR = 12

# Plans whose people do not earn months (their friends still get the gift).
NO_REWARD_PLANS = ("lifetime", "admin")

SHARE_SUBJECT = "Try Gusan with me"
GIFT_LINE = "Your friends get 2 weeks of Pro too"
PRIVACY_LINE = ("Only when you join an invite: a one-way code of this computer. Never what you say or type.")

JOIN_ERRORS = {
    "bad_code": "That link or code doesn't work. Check it and try again.",
    "own_code": "That's your own invite. Share it with a friend instead.",
    "already_joined": "You've already joined an invite.",
    "not_confirmed": "Confirm your email first, then join.",
    "same_computer": "This computer has already been used with an invite.",
    "seen_before": "This email already had a Gusan account, so it can't join an invite.",
    "limit": "This invite can't take more friends right now. Try again later.",
    "same_network": "Too many friends joined from this internet connection lately. Try again from another network "
                    "later.",
    "disposable_email": "Temporary email addresses can't join an invite. Use your regular email.",
    "network": "Couldn't reach Gusan. Check your connection and try again.",
    "signed_out": "Sign in first, then join.",
    "empty": "Type or paste your friend's code first.",
}
JOIN_ERROR_DEFAULT = "Couldn't join right now. Try again in a moment."

CLAIM_ERRORS = {
    "network": "Couldn't reach Gusan. Check your connection and try again.",
    "signed_out": "Sign in again, then claim your month.",
    "limit": "That's the most free months for this year.",
    "not_ready": "This month isn't ready yet.",
    "not_found": "This month isn't ready yet.",
    "already_claimed": "This month is already claimed.",
    "not_eligible": "Rewards are for Free and subscription plans. Your friends still get 2 weeks of Pro.",
    "stripe_error": "Couldn't apply it right now. Try again in a minute.",
    # A server error after the card company may already have said yes: calm - the next "me" shows the month.
    "server": "This is taking a moment. Check again in a minute - your month shows up here once it's on.",
}
NOT_ELIGIBLE_SHORT = "Rewards are for Free and subscription plans."
CLAIM_ERROR_DEFAULT = "Couldn't claim it right now. Try again in a moment."

# Why a friend does not count, in a few words (the meta line of their row).
NOT_COUNTED = {
    "same_computer": "Their computer was already used with an invite",
    "seen_before": "Their email had a Gusan account before",
    "not_confirmed": "Email not confirmed yet",
    "deleted": "Account deleted",
}
NOT_COUNTED.update({"same_network": "Joined from a busy shared connection",
                    "disposable_email": "Used a temporary email address"})
NOT_COUNTED_DEFAULT = "Doesn't count for this invite"

UNKNOWN_NAME = "A friend"           # the server's word for a friend whose name it does not share

_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
# A code inside a sentence (the whole shared message pasted): the server's own shape - a name part, a dash and four
# characters of its alphabet (no i, l, o, 0 or 1), so ordinary words never pass for one.
_CODE_IN_TEXT = re.compile(r"(?:^|[^a-z0-9-])([a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4})(?![a-z0-9-])")
_lock = threading.RLock()
_device = {"hash": None}


# ── the network ──────────────────────────────────────────────────────────────
class InviteError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _requests_post(url, **kwargs):
    import requests
    return requests.post(url, **kwargs)


# Tests replace this (one place every call goes through).
_post = _requests_post


def _token(auth):
    if auth is None or not getattr(auth, "is_authenticated", False):
        return None
    fn = getattr(auth, "get_access_token", None)
    if not callable(fn):
        return None
    try:
        return fn() or None
    except Exception:
        logger.debug("invite: no access token", exc_info=True)
        return None


def call(auth, action, body=None, post=None, timeout=TIMEOUT):
    """POST {action, ...body} to the invite function with the person's token. Blocking (a worker).
    Returns the JSON object; raises InviteError("signed_out" | "network" | "server" | <the server's reason>)."""
    token = _token(auth)
    if not token:
        raise InviteError("signed_out")
    payload = {"action": action}
    payload.update(body or {})
    try:
        resp = (post or _post)(INVITE_URL, json=payload, timeout=timeout,
                               headers={"Authorization": "Bearer %s" % token, "Content-Type": "application/json"})
    except Exception as e:                       # requests' errors and anything else: quiet
        logger.info("invite %s: network (%s)", action, type(e).__name__)
        raise InviteError("network")
    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        data = resp.json()
    except Exception:
        data = None
    if status in (401, 403):
        raise InviteError("signed_out")
    if not isinstance(data, dict):
        logger.info("invite %s: HTTP %s without JSON", action, status)
        raise InviteError("server")
    if status >= 400 and data.get("ok") is not False:
        reason = str(data.get("reason") or data.get("error") or "server")
        raise InviteError(reason if reason in JOIN_ERRORS or reason in CLAIM_ERRORS else "server")
    return data


def _failed(e):
    return {"ok": False, "reason": getattr(e, "reason", None) or "server"}


def fetch_me(auth, device=False, post=None):
    """The "me" answer, normalised (see normalize_me); None when it could not be read. ``device``: send this
    computer's one-way code too (the inviter's once-a-day check)."""
    body = {"device": device_hash()} if device else {}
    try:
        return normalize_me(call(auth, "me", body, post=post))
    except InviteError:
        return None


def fetch_hint(auth, post=None):
    """{"found": bool, "name": str, "code": str}: whether the server saw this person open a friend's link."""
    try:
        data = call(auth, "hint", {}, post=post)
    except InviteError:
        return {"found": False, "name": "", "code": ""}
    code = parse_code(data.get("code"))
    name = clean_name(data.get("name"))
    return {"found": bool(data.get("found")) and bool(code), "name": name, "code": code}


def join(auth, code, source="paste", post=None):
    typed = code
    code = parse_code(typed)
    if not code:
        return {"ok": False, "reason": "empty" if not str(typed or "").strip() else "bad_code"}
    try:
        data = call(auth, "join", {"code": code, "device": device_hash(),
                                   "source": "hint" if source == "hint" else "paste"}, post=post)
    except InviteError as e:
        return _failed(e)
    if data.get("ok") is True:
        return {"ok": True, "name": clean_name(data.get("name")), "gift_ends_at": data.get("gift_ends_at")}
    return {"ok": False, "reason": str(data.get("reason") or "server")}


def claim(auth, reward_id, post=None):
    try:
        data = call(auth, "claim", {"id": reward_id}, post=post)
    except InviteError as e:
        return _failed(e)
    if data.get("ok") is True:
        if data.get("status") == "next_bill_free":            # a subscriber: a one-month credit on the subscription
            return {"ok": True, "status": "next_bill_free", "amount": data.get("amount"),
                    "currency": str(data.get("currency") or "")}
        return {"ok": True, "status": "active", "starts_at": data.get("starts_at"), "ends_at": data.get("ends_at")}
    return {"ok": False, "reason": str(data.get("reason") or "server")}


def join_error(reason):
    return JOIN_ERRORS.get(str(reason or ""), JOIN_ERROR_DEFAULT)


def claim_error(reason):
    return CLAIM_ERRORS.get(str(reason or ""), CLAIM_ERROR_DEFAULT)


# ── this computer's one-way code ─────────────────────────────────────────────
def _windows_machine_guid():
    import winreg
    flags = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, flags) as k:
        return str(winreg.QueryValueEx(k, "MachineGuid")[0] or "")


def _mac_platform_uuid():
    out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                         timeout=5).stdout or ""
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    return m.group(1) if m else ""


def _fallback_id():
    """A random id made once and kept in the data folder (no machine id could be read)."""
    with _lock:
        data = _read_cache()
        fid = str(data.get("fallback_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", fid):
            fid = uuid.uuid4().hex
            data["fallback_id"] = fid
            _write_cache(data)
        return fid


def machine_id():
    """This computer's own id (never sent anywhere: see device_hash)."""
    raw = ""
    try:
        if sys.platform == "win32":
            raw = _windows_machine_guid()
        elif sys.platform == "darwin":
            raw = _mac_platform_uuid()
    except Exception:
        logger.debug("invite: machine id unreadable", exc_info=True)
        raw = ""
    raw = str(raw or "").strip()
    return raw or "fallback:" + _fallback_id()


def hash_id(raw):
    return hashlib.sha256((HASH_PREFIX + str(raw)).encode("utf-8")).hexdigest()


def device_hash():
    """sha256("gusan-invite-v1:" + this computer's id), 64 hex characters. Worked out once per run (a worker:
    on a Mac it runs ioreg)."""
    if _device["hash"] is None:
        _device["hash"] = hash_id(machine_id())
    return _device["hash"]


# ── links and codes ──────────────────────────────────────────────────────────
def parse_code(text):
    """A full invite link ("https://gusan.aibuben.xyz/i/aram-7k2q", with or without https://, a ?code= / ?i= link),
    a bare code ("aram-7k2q") or a message with the link in it -> the code, lower case; "" when there is none."""
    text = str(text or "").strip().strip("<>\"'").strip().rstrip(".,;:!?)").strip()
    if not text or len(text) > 600:
        return ""
    if _CODE.fullmatch(text):
        return text.lower()
    if any(c.isspace() for c in text):                  # a sentence: the link (or the code) inside it
        for word in text.split():
            word = word.strip("<>\"'()[]{},.;:!?")
            if ("/" in word or "." in word) and not any(c.isspace() for c in word):
                code = parse_code(word)
                if code:
                    return code
        m = _CODE_IN_TEXT.search(text.lower())
        return m.group(1) if m else ""
    candidate = text if "://" in text else "https://" + text
    try:
        u = urlparse(candidate)
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if not host or "." not in host:
        return ""
    parts = [p for p in (u.path or "").split("/") if p]
    for i, p in enumerate(parts[:-1]):
        if p.lower() in ("i", "invite", "join") and _CODE.fullmatch(parts[i + 1]):
            return parts[i + 1].lower()
    q = parse_qs(u.query or "")
    for key in ("code", "i", "invite", "ref"):
        for v in q.get(key, []):
            if _CODE.fullmatch(v.strip()):
                return v.strip().lower()
    return ""


def link_for(code):
    code = parse_code(code)
    return LINK_BASE + code if code else ""


def display_link(link):
    """The link as the page shows it: no https://, no trailing slash."""
    link = str(link or "").strip()
    for prefix in ("https://", "http://"):
        if link.lower().startswith(prefix):
            link = link[len(prefix):]
    return link.rstrip("/")


def display_code(code):
    """A code as the app shows it: upper case ("ARAM-7K2Q"); typed in any case, it works the same."""
    return parse_code(code).upper()


SHARE_LEAD = "I talk instead of typing with Gusan. Get 2 weeks of Pro free"


def share_text(link, code=""):
    """The short friendly message every share button sends: the link, and the code for typing it in the app."""
    code = display_code(code)
    text = "%s with my link: %s" % (SHARE_LEAD, link or "")
    return text + (" (or enter my code %s in Gusan)" % code if code else "")


def _short_text(code=""):
    """The message without the link (Telegram carries the link in its own field)."""
    code = display_code(code)
    return SHARE_LEAD + (" - or enter my code %s in Gusan" % code if code else "")


def share_urls(link, text=None, code=""):
    """{"whatsapp", "telegram", "email", "x", "linkedin"} -> the address that opens each with the message."""
    text = text if text is not None else share_text(link, code)
    short = _short_text(code)
    q = lambda s: quote(str(s or ""), safe="")
    return {
        "whatsapp": "https://wa.me/?text=" + q(text),
        "telegram": "https://t.me/share/url?url=%s&text=%s" % (q(link), q(short)),
        "email": "mailto:?subject=%s&body=%s" % (q(SHARE_SUBJECT), q(text)),
        "x": "https://x.com/intent/post?text=" + q(text),
        "linkedin": "https://www.linkedin.com/sharing/share-offsite/?url=" + q(link),
    }


# ── the "me" answer ──────────────────────────────────────────────────────────
def _int(value, lo=0, hi=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = lo
    n = max(lo, n)
    return min(hi, n) if hi is not None else n


def clean_name(value, limit=40):
    """A first name as it may be shown: plain text, one line, at most ``limit`` characters."""
    text = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(value or ""))
    text = " ".join(text.split())
    return text[:limit].strip()


def _when(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def short_date(value):
    """'2026-12-18T10:00:00Z' -> '18 Dec' ("" when unreadable)."""
    dt = _when(value)
    if dt is None:
        return ""
    dt = dt.astimezone()
    return "%d %s" % (dt.day, dt.strftime("%b"))


def normalize_me(data):
    """The server's "me" answer made safe to show: every key present, every type right, names plain. A friend counts
    as soon as they join ("counted"), or does not ("not_counted", with a reason)."""
    if not isinstance(data, dict):
        return None
    code = parse_code(data.get("code"))
    link = str(data.get("link") or "")
    if not link.lower().startswith("https://") or not code:
        link = link_for(code)
    friends = []
    for f in data.get("friends") or []:
        if not isinstance(f, dict):
            continue
        raw_name = clean_name(f.get("name"))
        known = bool(raw_name) and raw_name.lower() != UNKNOWN_NAME.lower()
        name = raw_name if known else UNKNOWN_NAME
        initial = clean_name(f.get("initial"), 2)[:1].upper()
        if not initial and known and "initial" not in f:
            initial = name[:1].upper()
        if not known:
            initial = ""                                 # no letter: the neutral avatar with the person icon
        status = str(f.get("status") or "counted")
        # (an older answer's "joined" is a friend who signed up and joined: that is what counts now)
        status = "counted" if status in ("counted", "joined") else "not_counted"
        friends.append({"name": name, "initial": initial, "known": known, "status": status,
                        "joined_at": f.get("joined_at") if isinstance(f.get("joined_at"), str) else None,
                        "reason": str(f.get("reason") or "")})
    rewards = []
    for r in data.get("rewards") or []:
        if not isinstance(r, dict) or r.get("id") in (None, ""):
            continue
        status = str(r.get("status") or "")
        if status == "next_bill_free":
            status = "credited"
        if status not in ("ready", "crediting", "active", "credited", "used"):        # crediting: a claim in progress
            continue
        rewards.append({"id": r.get("id"), "status": status,
                        "starts_at": r.get("starts_at") if isinstance(r.get("starts_at"), str) else None,
                        "ends_at": r.get("ends_at") if isinstance(r.get("ends_at"), str) else None})
    gift = data.get("gift") if isinstance(data.get("gift"), dict) else None
    if gift is not None:
        gift = {"active": bool(gift.get("active")),
                "ends_at": gift.get("ends_at") if isinstance(gift.get("ends_at"), str) else None}
    inviter = data.get("invited_by") if isinstance(data.get("invited_by"), dict) else None
    if inviter is not None:
        inviter = {"name": clean_name(inviter.get("name"))}       # "" when the server leaves it out
    counted = _int(data.get("counted"), 0)
    return {
        "code": code, "link": link, "first_name": clean_name(data.get("first_name")),
        "counted": max(counted, sum(1 for f in friends if f["status"] == "counted")),
        "joined_total": max(_int(data.get("joined_total"), 0), len(friends)),
        "rewards_left_this_year": _int(data.get("rewards_left_this_year", MONTHS_A_YEAR), 0, MONTHS_A_YEAR),
        "friends": friends, "rewards": rewards, "gift": gift, "invited_by": inviter,
        # Any time: an account can enter a friend's code once, until it has joined.
        "can_join": bool(data.get("can_join")) and inviter is None,
    }


def friend_key(f):
    return "%s|%s" % (f.get("name") or "", f.get("joined_at") or "")


# ── what the screens show ────────────────────────────────────────────────────
def _names(names):
    """["Mariam", "A friend", "A friend"] -> "Mariam and 2 friends"; unknown names are counted, not repeated."""
    known = [n for n in names if n and n != UNKNOWN_NAME]
    unknown = sum(1 for n in names if n == UNKNOWN_NAME)
    if unknown:
        if not known:
            return UNKNOWN_NAME if unknown == 1 else "%s friends" % _NUM.get(unknown, str(unknown))
        known.append("a friend" if unknown == 1 else "%d friends" % unknown)
    if len(known) <= 1:
        return "".join(known)
    return ", ".join(known[:-1]) + " and " + known[-1]


_NUM = {1: "One", 2: "Two", 3: "Three"}

AFTER_LEADS = {
    "active": "Your free month is on. 3 more friends make another one.",
    "credited": "Your next bill is free. 3 more friends make another one.",
}


def _lead(counted, ready, limit_reached, after=None):
    if limit_reached:
        return "That's 12 free months this year. Your friends still get 2 weeks of Pro."
    if ready:
        return "3 friends joined. Your month is ready."
    if not counted:
        return AFTER_LEADS.get(after) or "Share your link or code. When 3 friends join, your month is ready."
    empty = FRIENDS_NEEDED - len(counted)
    line = "%s %s." % (_names([f["name"] for f in counted]), "counts" if len(counted) == 1 else "count")
    if empty:
        line += " %s more friend%s to go." % (_NUM.get(empty, str(empty)), "" if empty == 1 else "s")
    return line


def to_go(counted):
    left = max(0, FRIENDS_NEEDED - counted)
    if left == FRIENDS_NEEDED:
        return "3 friends to go"
    if left == 0:
        return "Ready"
    return "%d more friend%s to go" % (left, "" if left == 1 else "s")


def eligible(plan):
    return str(plan or "").lower() not in NO_REWARD_PLANS


def _by_join(friends):
    return sorted(friends, key=lambda f: (f.get("joined_at") or "", f.get("name") or ""))


def view(me, plan=None, can_earn=None):
    """What the Invite page, the sidebar and the tray show for a "me" answer (None: nothing known yet).

    Returns {slots: [{kind: done|empty, name, initial, sub, idx}] x3, cycle_counted, ticket: locked | ready | active |
    credited (a subscriber's next bill is free) | ineligible, ready_id, to_go, until, lead, dots: (counted, 0),
    ready: bool, friends: [rows], eligible: bool, limit_reached: bool}. ``can_earn=False``: this account earns no
    months whatever its plan says (the server answered a claim with "not_eligible")."""
    me = me or {}
    friends = list(me.get("friends") or [])
    rewards = list(me.get("rewards") or [])
    ok_plan = eligible(plan) if can_earn is None else bool(can_earn) and eligible(plan)
    ready_reward = next((r for r in rewards if r["status"] == "ready"), None) if ok_plan else None
    crediting = next((r for r in rewards if r["status"] == "crediting"), None) if ok_plan else None
    pending = ready_reward or crediting                 # this cycle's month: to claim, or being applied
    claimed = [r for r in rewards if r["status"] in ("active", "credited", "used")]
    counted = _by_join([f for f in friends if f["status"] == "counted"])
    done_before = FRIENDS_NEEDED * (len(claimed) + (1 if pending else 0))
    if pending:
        start = max(0, done_before - FRIENDS_NEEDED)
        cycle = counted[start:start + FRIENDS_NEEDED]
        if len(cycle) < FRIENDS_NEEDED:                  # the list is shorter than the server's count: the last 3
            cycle = counted[-FRIENDS_NEEDED:]
    else:
        cycle = counted[done_before:done_before + FRIENDS_NEEDED]
    cycle_counted = FRIENDS_NEEDED if pending else min(FRIENDS_NEEDED, len(cycle))
    order = {friend_key(f): i for i, f in enumerate(_by_join(friends))}      # each friend's own colour
    active = next((r for r in rewards if r["status"] == "active"), None)
    credited = next((r for r in rewards if r["status"] == "credited"), None)
    shown = cycle
    if ok_plan and not pending and not cycle and (active or credited) and len(counted) >= FRIENDS_NEEDED:
        # A month was just claimed and nobody new joined since: the three friends who earned it stay on the track.
        shown = counted[max(0, done_before - FRIENDS_NEEDED):done_before] or counted[-FRIENDS_NEEDED:]
    slots = [{"kind": "done", "name": f["name"], "initial": f["initial"], "sub": "Counts",
              "idx": order.get(friend_key(f))} for f in shown[:FRIENDS_NEEDED]]
    while len(slots) < FRIENDS_NEEDED:
        slots.append({"kind": "done" if pending else "empty",              # ready: three ticks whatever the list
                      "name": "Friend %d" % (len(slots) + 1), "initial": "",
                      "sub": "Counts" if pending else "Not yet"})
    left = int(me.get("rewards_left_this_year", MONTHS_A_YEAR)) if me else MONTHS_A_YEAR
    limit_reached = ok_plan and not pending and left <= 0
    if not ok_plan:
        ticket = "ineligible"
    elif ready_reward:
        ticket = "ready"
    elif crediting:
        ticket = "crediting"                            # ready, busy: "Applying your free month…"
    elif cycle_counted == 0 and active is not None:
        ticket = "active"
    elif cycle_counted == 0 and credited is not None:
        ticket = "credited"
    else:
        ticket = "locked"
    until = short_date((active or {}).get("ends_at")) if ticket == "active" else ""
    rows = []
    for f in _by_join(friends):
        joined = short_date(f.get("joined_at"))
        if f["status"] == "counted":
            chip, tone, meta = "Counts", "ok", ("Joined %s" % joined) if joined else "Joined"
        else:
            chip, tone = "Doesn't count", "neutral"
            meta = NOT_COUNTED.get(f.get("reason") or "", NOT_COUNTED_DEFAULT)
        rows.append({"name": f["name"], "initial": f["initial"], "chip": chip, "tone": tone, "meta": meta,
                     "status": f["status"], "idx": order.get(friend_key(f))})
    after = "active" if active is not None else ("credited" if credited is not None else None)
    return {
        "slots": slots, "cycle_counted": cycle_counted,
        "ticket": ticket, "ready_id": ready_reward["id"] if ready_reward else None,
        "to_go": to_go(cycle_counted), "until": until,
        "lead": ("3 friends joined. Applying your free month…" if crediting and not ready_reward else
                 _lead(cycle if not pending else counted[-FRIENDS_NEEDED:], bool(ready_reward), limit_reached, after)),
        "dots": (cycle_counted, 0), "ready": bool(ready_reward),
        "friends": rows, "eligible": ok_plan, "limit_reached": limit_reached,
        "has_link": bool(me.get("link")),
    }


# ── the cache (invite.json, per account) ─────────────────────────────────────
def cache_path():
    return storage.path_for(CACHE_FILE)


def _read_cache():
    try:
        data = storage.read_json(cache_path(), {})
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _write_cache(data):
    try:
        storage.atomic_write_json(cache_path(), data)
        return True
    except Exception:
        logger.debug("invite cache not saved", exc_info=True)
        return False


def load_state(user_key):
    """This account's cached invite state (a copy): {"me", "me_at", "device_day", "announced", "credited_ids"};
    {} for none."""
    if not user_key:
        return {}
    with _lock:
        users = _read_cache().get("users")
        st = users.get(str(user_key)) if isinstance(users, dict) else None
        return dict(st) if isinstance(st, dict) else {}


def update_state(user_key, **changes):
    """Merge ``changes`` into this account's cached state and save it. Returns the new state (a copy)."""
    if not user_key:
        return dict(changes)
    with _lock:
        data = _read_cache()
        users = data.get("users") if isinstance(data.get("users"), dict) else {}
        st = users.get(str(user_key)) if isinstance(users.get(str(user_key)), dict) else {}
        st = dict(st, **changes)
        users[str(user_key)] = st
        data["users"] = users
        data["v"] = 1
        _write_cache(data)
        return dict(st)


def today(now=None):
    """The local calendar day, '2026-11-18' (``now``: a datetime or a date, for tests)."""
    now = now or datetime.now()
    return (now.date() if isinstance(now, datetime) else now).isoformat()


def device_due(state, day):
    """True when the Invite page should send this computer's code with "me" (once a day)."""
    return (state or {}).get("device_day") != day


def newly_counted(state, me):
    """(friends counted since the last look, the new "announced" list). The first look at an account announces
    nobody - its friends that already count are simply remembered (no burst of notes on a new computer)."""
    counted = [f for f in (me or {}).get("friends") or [] if f.get("status") == "counted"]
    keys = [friend_key(f) for f in counted]
    known = (state or {}).get("announced")
    if not isinstance(known, list):
        return [], keys
    fresh = [f for f in counted if friend_key(f) not in known]
    return fresh, list(dict.fromkeys(list(known) + keys))


def counted_note(friend, cycle_counted):
    """("Mariam counts", "2 more friends for your free month") for the small note when a friend counts."""
    left = max(0, FRIENDS_NEEDED - cycle_counted)
    title = "%s counts" % (friend.get("name") or "A friend")
    if left == 0:
        return title, "Your free month is ready"
    return title, "%d more friend%s for your free month" % (left, "" if left == 1 else "s")


__all__ = [
    "INVITE_URL", "LINK_BASE", "FRIENDS_NEEDED", "GIFT_DAYS", "InviteError",
    "fetch_me", "fetch_hint", "join", "claim", "call", "join_error", "claim_error", "display_code",
    "device_hash", "hash_id", "machine_id", "parse_code", "link_for", "display_link", "share_text", "share_urls",
    "normalize_me", "view", "to_go", "eligible", "short_date", "clean_name", "friend_key",
    "load_state", "update_state", "today", "device_due", "newly_counted", "counted_note",
]
