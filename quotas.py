"""Server calls for the free cloud allowances and for Gusan Pro checkout.

The numbers live in Postgres (begin_live_session, consume_meeting_note,
reserve_lifetime_spot). This module only talks to those RPCs and to the
create-checkout function. It never decides that someone is Pro.
"""

import logging

import pricing_policy

logger = logging.getLogger("transcribe.quotas")

def _urls():
    import auth
    base = auth.SUPABASE_URL.rstrip("/")
    return base, base + "/functions/v1/create-checkout"


def _key():
    import auth
    return auth.SUPABASE_PUBLISHABLE_KEY


def _post(post, url, token, payload, timeout=20):
    headers = {"apikey": _key(), "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return post(url, json=payload, headers=headers, timeout=timeout)


def _rpc(token, name, payload, post=None, timeout=20):
    if post is None:
        import requests
        post = requests.post
    base, _checkout = _urls()
    url = f"{base}/rest/v1/rpc/{name}"
    try:
        resp = _post(post, url, token, payload, timeout=timeout)
    except Exception as e:
        logger.warning("quota rpc %s failed: %s", name, e)
        return None, "unavailable"
    if resp.status_code == 401:
        return None, "signed_out"
    if resp.status_code != 200:
        logger.warning("quota rpc %s HTTP %s", name, resp.status_code)
        return None, "unavailable"
    try:
        return resp.json(), ""
    except ValueError:
        return None, "unavailable"


def begin_live_session(token, post=None):
    """{ok, reason, session_id} from begin_live_session. ok False on failure."""
    if not token:
        return {"ok": False, "reason": "signed_out"}
    data, problem = _rpc(token, "begin_live_session", {}, post=post)
    if problem:
        return {"ok": False, "reason": problem}
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {"ok": False, "reason": "unavailable"}
    return {
        "ok": bool(data.get("ok")),
        "reason": data.get("reason") or "",
        "session_id": data.get("session_id") or "",
    }


def end_live_session(token, session_id, post=None):
    if not token or not session_id:
        return False
    data, problem = _rpc(token, "end_live_session", {"p_session_id": session_id}, post=post)
    return problem == "" and data is True


def lifetime_spots_left(token=None, post=None):
    """How many lifetime spots are left, or None when the server can't be asked."""
    data, problem = _rpc(token, "lifetime_spots_left", {}, post=post)
    if problem or data is None:
        return None
    try:
        return max(0, int(data))
    except (TypeError, ValueError):
        return None


def create_checkout(token, plan, post=None, url=None):
    """(url or None, problem, info). problem is "" on success.

    problem is "signed_out", "sold_out", "already_lifetime", "unknown_plan",
    or "unavailable". info may carry spots_left.
    """
    if not token:
        return None, "signed_out", {}
    if plan not in ("monthly", "yearly", "lifetime"):
        return None, "unknown_plan", {}
    if url is None:
        _base, url = _urls()
    if post is None:
        import requests
        post = requests.post
    try:
        resp = post(url, json={"plan": plan},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=25)
    except Exception as e:
        logger.warning("checkout unreachable: %s", e)
        return None, "unavailable", {}
    try:
        payload = resp.json() or {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    info = {}
    if "spots_left" in payload and payload.get("spots_left") is not None:
        try:
            info["spots_left"] = int(payload["spots_left"])
        except (TypeError, ValueError):
            pass
    if resp.status_code == 200:
        checkout = payload.get("url") or ""
        if isinstance(checkout, str) and checkout.startswith("https://checkout.stripe.com/"):
            return checkout, "", info
        return None, "unavailable", info
    if resp.status_code == 401:
        return None, "signed_out", info
    if resp.status_code == 409:
        reason = payload.get("error") or "sold_out"
        if reason not in ("sold_out", "already_lifetime"):
            reason = "sold_out"
        return None, reason, info
    if payload.get("error") == "terms_url_required":
        info["message"] = str(payload.get("message") or "")
        return None, "terms_url_required", info
    logger.warning("checkout HTTP %s", resp.status_code)
    return None, "unavailable", info


def allowance_from_status(status, payload):
    """'meeting_notes' / 'live_session' when the server refused a free allowance."""
    if status != 402 or not isinstance(payload, dict):
        return None
    if payload.get("error") != "allowance_used":
        return None
    kind = payload.get("allowance")
    if kind in (pricing_policy.ALLOWANCE_MEETING_NOTES, pricing_policy.ALLOWANCE_LIVE_SESSION):
        return kind
    return None
