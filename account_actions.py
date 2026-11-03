"""Account & plan (Gusan 2.0): the page's view-model, delete account, feedback.

Integration notes for Wave 4
----------------------------
* The Account & plan page, the sidebar account row and Settings read
      view = account_actions.account_view(app.auth, app.cfg)
  (pure apart from reading usage.json for the Smart Action tries) and redraw
  on sig_auth_changed (main._on_auth_changed already calls
  main_win.refresh_account()). A guest - not signed in, or a super admin
  previewing Guest (still signed in underneath) - is never shown with a name,
  an email or an initial from the session: name is GUEST_NAME, the rest empty,
  and its one action is SIGN_IN_UP (no upgrade). Keys: signed_in (a person is
  shown: False for every guest), guest, name, email, initial, tier
  ("guest" | "free" | "pro"), badge ("GUEST" | "FREE" | "PRO"), pro, plan,
  plan_label ("Monthly" | "Annual" | "Trial" | "Admin" | "Free" | ""),
  on_trial, trial_days_left,   plan_line ("Annual · renews 4 Oct 2027",
  "Annual · ends 4 Oct 2027", "Yearly · committed until 4 Oct 2027", "Free"),
  smart_tries_left / smart_tries_total / smart_text ("Unlimited",
  "Sign in to use them"), is_admin, admin_tier, and buttons
  {sign_in, sign_out, upgrade, manage, delete, admin}.
* Buttons call the controller as they are: sign_in -> app.show_auth_gate(),
  sign_out -> app.sign_out(), upgrade -> app._pro_upsell(), manage ->
  app.open_billing(), admin tier -> app.set_admin_tier_override(value).
* Delete account: confirm twice in the page (typed word DELETE_WORD, see
  confirm_delete), then on a worker
      api_keys.run_then(account_actions.delete_account, app.auth, then=done)
  done((ok, message)): when ok, call app.sign_out() and show the message.
* Feedback (signed-in users only): turn pasted QImages into PNG bytes on the
  GUI thread (qimage_png), check each with image_problem(), base64 them
  (encode_images), then on a worker
      api_keys.run_then(account_actions.send_feedback, app.auth, text, images, then=done)
  and track "feedback_sent" {"category": "feedback"} when ok.
* main.DELETE_ACCOUNT_URL / FEEDBACK_URL / SUPPORT_EMAIL can import from here
  (same values; test_account_actions checks they match).
"""

import base64
import math
from datetime import datetime, timezone

import entitlements
from app_info import APP_VERSION

FEEDBACK_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/submit-feedback"
DELETE_ACCOUNT_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/delete-account"
SUPPORT_EMAIL = "aibuben.xyz@gmail.com"
TERMS_URL = "https://aibuben.xyz/terms"
PRIVACY_URL = "https://aibuben.xyz/privacy"

DELETE_WORD = "DELETE"
FEEDBACK_MAX_IMAGES = 4
FEEDBACK_MAX_IMAGE_BYTES = 6_000_000

_PLAN_LABELS = {
    "monthly": "Monthly", "annual": "Annual", "yearly": "Yearly",
    "lifetime": "Lifetime", "admin": "Admin",
}

# ── what a guest (and anyone else) is called and offered: ONE place ──────────
# The sidebar card, the tray menu, Account & plan, the Settings link and the Pro
# window all read these, so a label never differs between them.
GUEST_NAME = "User"                      # a guest is never called by a name: no profile name, no initial
GUEST_BADGE = "Guest"                    # the tray's account line; the pill itself reads GUEST
SIGN_IN_UP = "Sign in / Sign up"         # the guest's one action (blue); opens the sign-in sheet
SIGN_IN_UP_A11Y = "Sign in or sign up"   # what a screen reader says for it
UPGRADE = "Upgrade to Pro"               # Free only (violet)
SIGN_IN_FIRST = "Sign in first, then choose a plan."   # the guest's line where Free sees the price
SIGN_IN_TO_UPGRADE = "Sign in to upgrade"   # a guest's button in the Pro window: sign-in comes first, then checkout


# ── view-model ───────────────────────────────────────────────────────────────
def _parse_when(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_date(value):
    """'2027-10-04T12:00:00Z' -> '4 Oct 2027' ("" when it can't be read)."""
    dt = _parse_when(value)
    if dt is None:
        return ""
    return f"{dt.day} {dt.strftime('%b')} {dt.year}"


def trial_days_left(auth, now=None):
    """Whole days until period_end, rounded up (0 when unknown or past)."""
    end = _parse_when(getattr(auth, "period_end", None))
    if end is None:
        return 0
    now = now or datetime.now(timezone.utc)
    return max(0, math.ceil((end - now).total_seconds() / 86400.0))


def plan_label(plan):
    return _PLAN_LABELS.get(str(plan or "").lower(), "")


def _on_trial(auth, pro):
    # Trials are gone. A stale "trialing" status must not paint a trial banner.
    return False


def _plan_line(auth, pro, signed_in, on_trial, days, plan):
    if not pro:
        return "Free" if signed_in else ""
    label = plan_label(plan)
    if plan == "admin":
        return "Admin"
    if plan == "lifetime":
        return "Lifetime"
    commitment = format_date(getattr(auth, "commitment_ends_at", None))
    cancels = format_date(getattr(auth, "cancels_at", None)) if getattr(auth, "cancels_at", None) else ""
    if plan == "yearly" and commitment:
        if cancels or getattr(auth, "cancel_at_period_end", False):
            return f"Yearly · ends {cancels or commitment}"
        return f"Yearly · committed until {commitment}"
    when = format_date(getattr(auth, "period_end", None))
    if label and when:
        verb = "ends" if getattr(auth, "cancel_at_period_end", False) else "renews"
        return f"{label} · {verb} {when}"
    return label or "Pro"


def account_view(auth, cfg=None, *, now=None, smart_remaining=None):
    """Everything Account & plan shows (see the module notes). ``pro`` is
    feature access (entitlements.has_pro_access: a super admin's tier preview
    wins), the same rule every Pro gate uses."""
    tier = entitlements.tier(auth, cfg)
    pro = entitlements.has_pro_access(auth, cfg)
    if pro:
        tier = entitlements.TIER_PRO
    guest = tier == entitlements.TIER_GUEST
    # A person is shown only when the plan says so: a super admin previewing
    # Guest is still signed in underneath (their profile name is still in
    # auth), and a guest must never wear it.
    signed_in = bool(auth is not None and getattr(auth, "is_authenticated", False)) and not guest
    email = (getattr(auth, "user_email", None) or "") if signed_in else ""
    name = ""
    if signed_in:
        name = getattr(auth, "user_name", None) or (email.split("@")[0] if email else "") or "Your account"
    initial = (name or email or "?")[:1].upper() if signed_in else ""
    plan = getattr(auth, "plan", None) if signed_in else None
    on_trial = _on_trial(auth, pro) and signed_in
    days = trial_days_left(auth, now) if on_trial else 0

    total = entitlements.FREE_SMART_ACTION_TRIES
    if pro or (signed_in and not guest):
        left, smart_text = None, "Unlimited"
    else:
        left, smart_text = None, "Sign in to use them"

    is_admin = entitlements.is_super_admin(auth)
    real_plan = str(plan or "").lower()
    return {
        "signed_in": signed_in,
        "guest": guest,
        "name": name or GUEST_NAME,
        "email": email,
        "initial": initial,
        "tier": tier,
        "badge": tier.upper(),
        "pro": pro,
        "plan": plan,
        "plan_label": plan_label(plan) if pro else ("Free" if signed_in else ""),
        "on_trial": on_trial,
        "trial_days_left": days,
        "plan_line": _plan_line(auth, pro, signed_in, on_trial, days, real_plan),
        "smart_tries_left": left,
        "smart_tries_total": total,
        "smart_text": smart_text,
        "is_admin": is_admin,
        "admin_tier": (cfg or {}).get("admin_tier_override", "auto") if is_admin else "auto",
        "buttons": {
            "sign_in": not signed_in,
            "sign_out": signed_in,
            "upgrade": not pro and not guest,   # Free only: a guest's one action is sign_in
            # A Stripe subscription (monthly, the yearly commitment, or a legacy
            # annual plan) has a portal. Lifetime is a one-time payment: receipts
            # live there too once a customer exists, so it is included.
            "manage": signed_in and pro and real_plan in ("monthly", "annual", "yearly", "lifetime"),
            "delete": signed_in,
            "admin": is_admin,
        },
    }


def app_tier(app):
    """'pro' | 'free' | 'guest' for the controller: the one rule the sidebar,
    the tray, the Pro window and the greetings share. The controller's own
    current_tier() carries a super admin's tier preview (so a signed-in admin
    previewing Guest IS a guest here); without it the sign-in decides."""
    try:
        if app.is_pro():
            return entitlements.TIER_PRO
    except Exception:
        pass
    tier = None
    fn = getattr(app, "current_tier", None)
    if callable(fn):
        try:
            tier = fn()
        except Exception:
            tier = None
    if tier not in (entitlements.TIER_PRO, entitlements.TIER_FREE, entitlements.TIER_GUEST):
        signed_in = bool(getattr(getattr(app, "auth", None), "is_authenticated", False))
        tier = entitlements.TIER_FREE if signed_in else entitlements.TIER_GUEST
    return tier


def is_guest(app):
    return app_tier(app) == entitlements.TIER_GUEST


def first_name(app):
    """The signed-in person's first name (their profile name, else the front of
    their email), "" for a guest: whatever a session still holds - a super
    admin previewing Guest is signed in underneath - a guest is never called
    by it."""
    if is_guest(app):
        return ""
    auth = getattr(app, "auth", None)
    name = str(getattr(auth, "user_name", None) or "").strip()
    if not name:
        email = str(getattr(auth, "user_email", None) or "").strip()
        name = email.split("@")[0] if email else ""
    return name.split()[0] if name else ""


# ── delete account ───────────────────────────────────────────────────────────
def confirm_delete(typed):
    """The second confirmation: the user typed DELETE."""
    return str(typed or "").strip() == DELETE_WORD


def _post(post):
    if post is not None:
        return post
    import requests
    return requests.post


def delete_account(auth, post=None, url=DELETE_ACCOUNT_URL, timeout=30):
    """GDPR erasure: the server cancels any subscription, erases the account
    and its server data. Blocking (worker). Returns (ok, message); when ok the
    caller signs out. Local transcripts are not touched."""
    if not (auth is not None and getattr(auth, "is_authenticated", False)):
        return False, "You're not signed in."
    try:
        token = auth.get_access_token()
    except Exception:
        token = None
    if not token:
        return False, "Your session expired. Sign in again and retry."
    try:
        resp = _post(post)(url, json={"confirm": DELETE_WORD},
                           headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except Exception:
        return False, "Couldn't reach the server. Check your connection and try again."
    if resp.status_code == 200:
        return True, "Your account was deleted."
    if resp.status_code in (401, 403):
        return False, "Your session expired. Sign in again and retry."
    return False, f"Couldn't delete the account (HTTP {resp.status_code}). Try again later."


# ── feedback ─────────────────────────────────────────────────────────────────
def image_problem(png_bytes, already=0):
    """"" when this image can be attached, else why not."""
    if already >= FEEDBACK_MAX_IMAGES:
        return f"You can attach up to {FEEDBACK_MAX_IMAGES} images."
    if len(png_bytes or b"") > FEEDBACK_MAX_IMAGE_BYTES:
        return "That image is too large (max about 6 MB)."
    return ""


def encode_images(png_list):
    """PNG bytes -> the base64 strings the feedback function takes."""
    return [base64.b64encode(bytes(b)).decode("ascii") for b in png_list or []][:FEEDBACK_MAX_IMAGES]


def qimage_png(qimage):
    """A QImage as PNG bytes (Qt, GUI thread)."""
    from PySide6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    qimage.save(buf, "PNG")
    return bytes(buf.data())


def send_feedback(auth, message, images_b64=(), version=APP_VERSION, category="feedback",
                  post=None, url=FEEDBACK_URL, timeout=30):
    """Send feedback (signed-in users). Blocking (worker). Returns (ok, message)."""
    message = str(message or "").strip()
    if not message:
        return False, "Type a message first."
    if not (auth is not None and getattr(auth, "is_authenticated", False)):
        return False, "Sign in first so we can follow up with you."
    try:
        token = auth.get_access_token()
    except Exception:
        token = None
    if not token:
        return False, "Your session expired. Sign in again."
    payload = {"message": message, "app_version": version, "category": category}
    images = list(images_b64 or [])[:FEEDBACK_MAX_IMAGES]
    if images:
        payload["images"] = images
    try:
        resp = _post(post)(url, json=payload, headers={"Authorization": f"Bearer {token}"},
                           timeout=timeout)
    except Exception:
        return False, "Couldn't send it. Check your connection and try again."
    if resp.status_code == 200:
        return True, "Thanks! We read every message."
    if resp.status_code == 503:
        return False, "Feedback isn't set up on the server yet. Please try later."
    if resp.status_code == 429:
        return False, "You've sent a lot today - thank you! Please try again tomorrow."
    if resp.status_code in (401, 403):
        return False, "Your session expired. Sign in again and retry."
    return False, f"Couldn't send it right now (HTTP {resp.status_code}). Please try again later."
