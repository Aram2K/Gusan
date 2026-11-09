"""Client for the interpreter-session and voice-clone server functions (contract: section 8.1 of
docs/redesign/interpreter-spec.md).

The server checks the account (Pro, time left this month) and hands out ElevenLabs single-use
tokens, so the PC talks to ElevenLabs directly without ever holding the key. Errors come back as
InterpreterError with a code the UI turns into one calm sentence.
"""
import logging

import requests

import auth

logger = logging.getLogger(__name__)

FUNCTIONS = auth.SUPABASE_URL + "/functions/v1/"
SESSION_URL = FUNCTIONS + "interpreter-session"
VOICE_URL = FUNCTIONS + "voice-clone"

# What each server error means to a person (the UI shows these as they are).
MESSAGES = {
    "auth": "Sign in again to use the interpreter.",
    "pro_required": "The interpreter is part of Gusan Pro.",
    "allowance_used": "You've used this month's interpreter time.",
    # The server's daily abuse ceilings (interpreter-session: speech passes; voice-clone: voice samples).
    "quota_exceeded": "The interpreter hit today's limit. Try again tomorrow.",
    "no_voice": "Create your voice first.",
    "consent_required": "Tick the box to confirm it's your own voice.",
    "voice_too_short": "Those recordings were too short to make a voice. Try Record now.",
    "voice_samples_rejected": "Couldn't make a voice from those recordings. Try Record now in a quiet room.",
    "provider_unavailable": "The interpreter can't reach its voice service right now. Try again in a minute.",
    "provider_credits": "The interpreter's voice service is out of credit. We're on it.",
    "bad_request": "Something in that request was off. Try again.",
    "network": "No connection to Gusan Cloud.",
    "server": "Gusan Cloud had a problem. Try again in a minute.",
}


class InterpreterError(RuntimeError):
    def __init__(self, code, message="", status=0, seconds_left=None):
        super().__init__(message or MESSAGES.get(code, MESSAGES["server"]))
        self.code = code
        self.status = status
        self.seconds_left = seconds_left


def _post(url, token, body, timeout=15, http=None):
    if not token:
        raise InterpreterError("auth")
    try:
        resp = (http or requests).post(url, json=body, headers={"Authorization": f"Bearer {token}"},
                                       timeout=timeout)
    except requests.RequestException as e:
        logger.info("interpreter server unreachable: %s", e)
        raise InterpreterError("network")
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    if resp.status_code >= 400 or data.get("error"):
        code = str(data.get("error") or "")
        if code not in MESSAGES:
            code = {401: "auth", 402: "pro_required", 429: "allowance_used", 400: "bad_request",
                    404: "no_voice", 503: "provider_unavailable"}.get(resp.status_code, "server")
        raise InterpreterError(code, status=resp.status_code, seconds_left=data.get("seconds_left"))
    return data


class InterpreterSession:
    """One interpreter run: start -> tokens; heartbeat every ~30 s with the seconds used; stop.

    token_getter() -> the user's current access token (auth.get_access_token)."""

    def __init__(self, token_getter, http=None):
        self._token_getter = token_getter
        self._http = http
        self.id = ""
        self.info = {}
        self.seconds_left = None

    def _call(self, body, timeout=15):
        return _post(SESSION_URL, self._token_getter(), body, timeout=timeout, http=self._http)

    def start(self, hear=None, speak=None):
        """hear / speak: (from_lang, to_lang) or None for a direction that is off."""
        body = {"action": "start"}
        if hear:
            body["hear"] = {"from": hear[0], "to": hear[1]}
        if speak:
            body["speak"] = {"from": speak[0], "to": speak[1]}
        data = self._call(body)
        self.id = data.get("session_id") or ""
        self.info = data
        self.seconds_left = data.get("seconds_left")
        return data

    def token(self, kind, direction=None):
        """A fresh single-use token: kind "scribe" or "tts" (a reconnect, or one per voice socket)."""
        body = {"action": "token", "session_id": self.id, "kind": kind}
        if direction:
            body["direction"] = direction
        return self._call(body, timeout=8).get("token") or ""

    def heartbeat(self, seconds):
        data = self._call({"action": "heartbeat", "session_id": self.id, "seconds": int(seconds)}, timeout=8)
        self.seconds_left = data.get("seconds_left", self.seconds_left)
        return self.seconds_left

    def stop(self, seconds):
        if not self.id:
            return self.seconds_left
        try:
            data = self._call({"action": "stop", "session_id": self.id, "seconds": int(seconds)}, timeout=6)
            self.seconds_left = data.get("seconds_left", self.seconds_left)
        except InterpreterError as e:
            logger.info("interpreter stop not recorded: %s", e.code)
        return self.seconds_left


class VoiceStore:
    """The person's cloned voice on the server (voice-clone function)."""

    def __init__(self, token_getter, http=None):
        self._token_getter = token_getter
        self._http = http

    def _call(self, body, timeout=20):
        return _post(VOICE_URL, self._token_getter(), body, timeout=timeout, http=self._http)

    def get(self):
        return self._call({"action": "get"}).get("voice")

    def create(self, clips_b64, source, consent_phrase):
        """clips_b64: base64 WAV clips (16 kHz mono). Returns {"voice_id", "created_at"}."""
        return self._call({"action": "create", "clips": list(clips_b64), "source": source,
                           "consent": True, "consent_phrase": consent_phrase}, timeout=90)

    def preview(self, text, language):
        """Returns (pcm bytes, sample_rate) of the person's voice saying ``text``."""
        import base64
        data = self._call({"action": "preview", "text": text, "language": language}, timeout=30)
        return base64.b64decode(data.get("audio") or b""), int(data.get("sample_rate") or 16000)

    def delete(self):
        return bool(self._call({"action": "delete"}).get("deleted"))
