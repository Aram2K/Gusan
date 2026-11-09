"""Translate one spoken phrase through Gusan Cloud (smart-action, mode "interpret"), streamed.

The server picks the model (DeepSeek V4.1 Flash first, then Mistral Small, then OpenAI nano):
measured 2026-11-07, DeepSeek answered ~0.5 s after the phrase reached it; GPT-5.4 nano was
~0.3 s slower. The request carries the phrase, the two languages, the last few phrases (only to
resolve meaning) and the Dictionary words (names stay as written). The messages are also sent,
built here exactly like the server builds them, so every smart-action version can answer.

Each thread keeps its connection open (every direction translates on its own worker thread), so
only its first phrase pays for the TCP and TLS handshake - and warm_up() pays it before the first
phrase comes (bench 2026-11-09, S3: the first phrase each way was 0.6-0.9 s slower).
"""
import json
import logging
import threading

import requests

import action_api
from speech_langs import language_name

logger = logging.getLogger(__name__)

MAX_CONTEXT = 3
MAX_GLOSSARY = 40

_local = threading.local()


class TranslateError(RuntimeError):
    """code: "auth", "pro_required", "quota", "server", "network", or "cut" - the answer stopped
    before its end (``partial``: the words that came)."""

    def __init__(self, message, code="", partial=""):
        super().__init__(message)
        self.code = code
        self.partial = partial


def _http():
    """This thread's kept-open connection to Gusan Cloud."""
    s = getattr(_local, "http", None)
    if s is None:
        s = _local.http = requests.Session()
    return s


def _close(resp):
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


# smart-action's own warm-up: answered 202 right after the sign-in and Pro check, no quota used;
# it also wakes the live model, the interpreter's first engine
WARM_UP = {"warmup": True, "mode": "interpret"}


def warm_up(token, *, url=None, timeout=(5, 10), session=None):
    """Wake Gusan Cloud before a phrase needs it, over THIS thread's kept connection (call it on the
    thread that translates): the TLS handshake, the function and its model are warm when the phrase
    comes. Sent on a side thread, so a phrase never waits for it; a failure is ignored. ``token``: the
    access token, or a function that gives it (read on the side thread too)."""
    http = session or _http()

    def send():
        try:
            tok = token() if callable(token) else token
            if tok:
                _close(http.post(url or action_api.SMART_ACTION_URL, json=dict(WARM_UP),
                                 headers={"Authorization": f"Bearer {tok}"}, timeout=timeout))
        except Exception:  # noqa: BLE001 - only a warm-up
            logger.debug("interpreter warm-up failed", exc_info=True)
    t = threading.Thread(target=send, name="interp-translate-warm", daemon=True)
    t.start()
    return t


def _give_back(resp, lines):
    """After [DONE]: read the rest of the stream (the server ends it right away), so the
    connection goes back to the pool for the next phrase. On a side thread: the answer never
    waits for it."""
    def drain():
        try:
            for _ in lines:
                pass
        except Exception:  # noqa: BLE001
            pass
        _close(resp)
    threading.Thread(target=drain, name="interp-translate-drain", daemon=True).start()


def build_messages(text, source_lang, target_lang, context=(), glossary=()):
    src = language_name(source_lang)
    dst = language_name(target_lang)
    system = (f"You are a live interpreter. Translate the speaker's phrase from {src} into {dst}. "
              "Keep names as they are. Use the earlier phrases only to understand the meaning. "
              "Output ONLY the translation: no quotes, no notes.")
    words = [w for w in glossary if w][:MAX_GLOSSARY]
    if words:
        system += " Write these words exactly as given: " + ", ".join(words) + "."
    ctx = [c for c in context if c][-MAX_CONTEXT:]
    user = (("Earlier phrases:\n" + "\n".join(ctx) + "\n\nPhrase:\n") if ctx else "") + text
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def translate_phrase(text, source_lang, target_lang, context, on_delta, *, token, glossary=(),
                     url=None, timeout=(5, 12), session=None):
    """Return the translation, and stream its pieces to on_delta: each piece adds to what came
    before, an action_api.ReplaceText replaces it (reasoning a model leaked is dropped that way).
    Raises TranslateError; an answer that stopped before its end is code "cut"."""
    if not token:
        raise TranslateError("Sign in to use the interpreter.", code="auth")
    body = {
        "mode": "interpret", "stream": True, "max_tokens": 300,
        "text": text, "source_lang": source_lang, "target_lang": target_lang,
        "context": [c for c in context if c][-MAX_CONTEXT:],
        "glossary": [w for w in glossary if w][:MAX_GLOSSARY],
        "messages": build_messages(text, source_lang, target_lang, context, glossary),
    }
    post = (session or _http()).post
    try:
        resp = post(url or action_api.SMART_ACTION_URL, json=body,
                    headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=timeout)
    except requests.RequestException as e:
        raise TranslateError(f"Network error: {e}", code="network")
    if resp.status_code >= 400:
        _close(resp)
    if resp.status_code == 401:
        raise TranslateError("Sign in again to use the interpreter.", code="auth")
    if resp.status_code in (402, 403):
        raise TranslateError("The interpreter is part of Gusan Pro.", code="pro_required")
    if resp.status_code == 429:
        raise TranslateError("Too many translations today. Try again tomorrow.", code="quota")
    if resp.status_code >= 400:
        raise TranslateError(f"Translation failed ({resp.status_code}).", code="server")
    content_type = resp.headers.get("Content-Type") or ""
    template_opened = action_api._answer_family(resp) in ("deepseek", "qwen")
    if "text/event-stream" not in content_type:
        try:
            out = action_api.strip_think((resp.json() or {}).get("text") or "", template_opened)
        except ValueError:
            out = ""
        finally:
            _close(resp)
        if out:
            on_delta(out)
        return out.strip()
    parts = []
    think = action_api.ThinkFilter(template_opened)
    lines = action_api._sse_data_lines(resp)
    done = False        # [DONE] came: the stream is over
    ended = False       # the model said it had finished ([DONE] or a finish reason)
    cut = False         # ... or it stopped early (an error after the first words, a token limit)
    try:
        for data in lines:
            if data == "[DONE]":
                done = ended = True
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("error"):
                if not parts:
                    raise TranslateError(str(obj["error"]), code="server")
                cut = True
                break
            choice = (obj.get("choices") or [{}])[0]
            reason = choice.get("finish_reason")
            if reason:
                ended = True
                cut = cut or str(reason) in action_api._CUT_REASONS
            action_api._emit(think.feed((choice.get("delta") or {}).get("content") or ""), parts, on_delta)
    except requests.RequestException as e:
        if not parts:
            raise TranslateError(f"Network error: {e}", code="network")
    finally:
        if done:
            _give_back(resp, lines)
        else:
            _close(resp)
    action_api._emit(think.flush(), parts, on_delta)
    out = _clean("".join(parts))
    if out and (cut or not ended):
        # never said as if it were the whole phrase ("We can sign" of "We can sign only if ...")
        raise TranslateError("The translation was cut off.", code="cut", partial=out)
    return out


def _clean(text):
    """Models sometimes wrap a one-line answer in quotes; a spoken translation never needs them."""
    t = text.strip()
    for a, b in (('"', '"'), ("“", "”"), ("«", "»"), ("「", "」")):
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            t = t[1:-1].strip()
    return t
