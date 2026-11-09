"""Client for the temporary bench function (speed-bench), plus a fake with the same interface.

    BenchClient(url, token)  ping() / tts() / tokens() / translate_stream() / stt() / judge()
    FakeBench()              the same methods, deterministic and offline (--dry-run, self-tests)
    CharBudget(max_chars)    every TTS caller charges it first; it raises BudgetExceeded rather
                             than going over. Real TTS calls also land in a monthly ledger
                             (results/chars_ledger.json) so the month's ~20,000 characters are
                             tracked across runs.
    TTSCache + cached_tts()  a clip (model|voice|lang|text) is paid for once.

The token comes from the env GUSAN_BENCH_TOKEN or bench/interpreter/.bench_token. It is never
printed. Quick checks from a shell:  python bench_client.py ping | ledger | usage
"""
import argparse
import base64
import codecs
import collections
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import benchaudio

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/speed-bench"
TOKEN_ENV = "GUSAN_BENCH_TOKEN"
TOKEN_FILE = BENCH_DIR / ".bench_token"
RESULTS_DIR = BENCH_DIR / "results"
LEDGER_FILE = RESULTS_DIR / "chars_ledger.json"
MONTHLY_CHARS = int(os.environ.get("GUSAN_BENCH_MONTHLY_CHARS") or 20000)

TTS_MODELS = ("eleven_v3", "eleven_multilingual_v2", "eleven_v4_turbo", "eleven_flash_v2_5")
STT_ENGINES = ("scribe_v2", "voxtral", "openai", "gladia")
ROUTES = ("default", "deepseek", "mistral", "openai")
TOKEN_KINDS = ("realtime_scribe", "ttd_websocket")
MAX_TTS_CHARS = 600
JUDGE_BATCH = 20
MAX_CONTEXT = 3          # same as the app (interpreter/translate.py)
MAX_GLOSSARY = 40
SAMPLE_RATE = 16000
TRANSLATE_HEADERS = (("X-Server-Ms", "server_ms"), ("X-Bench-Route", "route"), ("X-Bench-Model", "model"),
                     ("X-Answer-Family", "answer_family"), ("X-Bench-First-Ms", "server_first_ms"),
                     ("X-Bench-Budget-Ms", "budget_ms"), ("X-Bench-Variant", "variant"),
                     ("X-Bench-Attempts", "attempts"), ("X-Bench-Max-Tokens", "max_tokens"))
TIMEOUTS = {"ping": 20, "tts": 90, "tokens": 20, "translate": 30, "stt": 300, "judge": 180}

# ElevenLabs premade voices (the catalog in corpus/voices.json may add more)
KNOWN_VOICES = {
    "EXAVITQu4vr4xnSDxMaL": ("Sarah", "female"), "Xb7hH8MSUJpSbSDYk0k2": ("Alice", "female"),
    "cgSgspJ2msm6clMCkdW9": ("Jessica", "female"), "pFZP5JQG7iQjIQuC4Bku": ("Lily", "female"),
    "cjVigY5qzO86Huf0OWal": ("Eric", "male"), "JBFqnCBsd6RMkjVDRZzb": ("George", "male"),
    "nPczCjzI2devNBz1zQrb": ("Brian", "male"), "iP95p4xoKVk53GoZ742B": ("Chris", "male"),
    "SAz9YHcvj6GT2YYXdXww": ("River", "neutral"),
}


class BenchError(RuntimeError):
    """A bench call failed. status is the HTTP status (None when the server never answered)."""

    def __init__(self, message, *, action="", status=None, code="", body=None):
        super().__init__(message)
        self.action = action
        self.status = status
        self.code = code
        self.body = body


class BudgetExceeded(RuntimeError):
    """A TTS call would go over the character budget; nothing was spent."""


FATAL_STATUS = {401, 404, 410}          # wrong token, no function, stubbed: nothing else will work either
NO_TOKEN = "no_token"
_PROVIDER_STATUS = re.compile(r"^[a-z_]+ (\d{3})$")
_NOT_SENT = ("NewConnectionError", "NameResolutionError", "Failed to establish a new connection",
             "getaddrinfo failed", "Connection refused")     # the request never left this PC


def is_fatal(e):
    """A wrong / missing token or a missing / stubbed function: every later call would fail too."""
    return isinstance(e, BenchError) and (e.status in FATAL_STATUS or e.code == NO_TOKEN)


def _provider_refused(code):
    """The function's "<provider> <status>" error for a provider 4xx (not 429): the same request
    would be refused again, so it is not retried."""
    m = _PROVIDER_STATUS.match(code or "")
    return bool(m) and m.group(1).startswith("4") and m.group(1) != "429"


def retryable(status, code=""):
    """429 and 5xx are tried again - except a provider's own 4xx (wrapped in a 502) and a provider
    without a key (503 "<provider> not_configured"): those answer the same way every time."""
    if status != 429 and (status is None or status < 500):
        return False
    return not _provider_refused(code) and not (code or "").endswith("not_configured")


def maybe_billed(status, code="", exc=None):
    """Could the provider have done (and billed) the work of an attempt that failed this way?
    No when it answered with an error, was rate limited, has no key, or the request never left
    this PC; yes for timeouts, dropped connections after sending, and crashes (500, 546, ...)."""
    if exc is not None:
        if type(exc).__name__ in ("ConnectTimeout", "SSLError", "ProxyError", "InvalidURL", "MissingSchema",
                                  "InvalidSchema"):
            return False
        return not any(s in str(exc) for s in _NOT_SENT)
    if status is None or status < 500 or status == 429:
        return False
    if _PROVIDER_STATUS.match(code or "") or (code or "").endswith("not_configured"):
        return False                                # the provider answered with an error status
    return True


def load_token():
    """The bench password: env GUSAN_BENCH_TOKEN, else the first line of .bench_token."""
    tok = (os.environ.get(TOKEN_ENV) or "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def _write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# -- characters -----------------------------------------------------------------------------

class CharLedger:
    """ElevenLabs characters spent per month, kept in a small JSON file:
    {"2026-11": {"total": n, "calls": k, "by": {"generate": n1, "bench": n2}}}"""

    def __init__(self, path=LEDGER_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def add(self, chars, purpose="bench", when=None):
        month = (when or datetime.now()).strftime("%Y-%m")
        with self._lock:
            data = self._load()
            m = data.setdefault(month, {"total": 0, "calls": 0, "by": {}})
            m["total"] = int(m.get("total", 0)) + int(chars)
            m["calls"] = int(m.get("calls", 0)) + 1
            m.setdefault("by", {})[purpose] = int(m["by"].get(purpose, 0)) + int(chars)
            _write_json_atomic(self.path, data)

    def month(self, month=None):
        month = month or datetime.now().strftime("%Y-%m")
        return dict(self._load().get(month) or {"total": 0, "calls": 0, "by": {}})

    def month_total(self, month=None):
        return int(self.month(month).get("total", 0))


class CharBudget:
    """The characters a run may spend on ElevenLabs voice. charge() before every TTS call: it
    raises BudgetExceeded (and charges nothing) when the call would go over max_chars, or over
    monthly_cap given what the ledger already holds for this month."""

    def __init__(self, max_chars, *, monthly_cap=None, ledger=None):
        self.max_chars = None if max_chars is None else int(max_chars)
        self.monthly_cap = None if monthly_cap is None else int(monthly_cap)
        self.used = 0
        self._month_base = ledger.month_total() if (ledger is not None and monthly_cap is not None) else 0
        self._lock = threading.Lock()

    @property
    def remaining(self):
        left = float("inf") if self.max_chars is None else self.max_chars - self.used
        if self.monthly_cap is not None:
            left = min(left, self.monthly_cap - self._month_base - self.used)
        return max(0, left) if left != float("inf") else left

    def can_spend(self, n):
        return int(n) <= self.remaining

    def charge(self, text_or_n):
        n = int(text_or_n) if isinstance(text_or_n, (int, float)) else len(text_or_n or "")
        with self._lock:
            left = self.remaining
            if n > left:
                why = f"max {self.max_chars}" if self.max_chars is not None else ""
                if self.monthly_cap is not None:
                    why += f"{', ' if why else ''}month {self._month_base}/{self.monthly_cap} used before this run"
                raise BudgetExceeded(f"needs {n} characters, {left} left ({why})")
            self.used += n
        return n

    def refund(self, n):
        with self._lock:
            self.used = max(0, self.used - int(n))

    def add(self, n):
        """Count characters already spent (e.g. the server billed more than the text length)."""
        with self._lock:
            self.used += max(0, int(n))

    def status(self):
        left = self.remaining
        cap = "no limit" if self.max_chars is None else f"max {self.max_chars}"
        return f"{self.used} characters used, {left} left ({cap})"


# -- TTS cache ------------------------------------------------------------------------------

def cache_key(model, voice_id, language, text):
    """sha1 of model|voice|lang|text (language None -> '')."""
    raw = f"{model}|{voice_id}|{language or ''}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class TTSCache:
    """<key>.pcm (16 kHz mono s16le, as the server sent it) + <key>.json (what it is)."""

    def __init__(self, root):
        self.root = Path(root)

    def get(self, key):
        try:
            return (self.root / f"{key}.pcm").read_bytes()
        except OSError:
            return None

    def meta(self, key):
        try:
            return json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def has(self, key):
        return (self.root / f"{key}.pcm").is_file()

    def put(self, key, pcm, meta):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f"{key}.pcm.tmp"
        tmp.write_bytes(pcm)
        os.replace(tmp, self.root / f"{key}.pcm")
        _write_json_atomic(self.root / f"{key}.json", meta)


def cached_tts(bench, cache, budget, text, voice_id, model, language=None):
    """(pcm, info) from the cache when this exact clip was made before (free), else from
    bench.tts() charged to budget and stored. info["cached"] says which; info["chars"] is what
    this call cost (0 for a cache hit)."""
    key = cache_key(model, voice_id, language, text)
    pcm = cache.get(key) if cache is not None else None
    if pcm is not None:
        meta = cache.meta(key) or {}
        return pcm, {"cached": True, "chars": 0, "model": model, "voice_id": voice_id, "language": language,
                     "key": key, "server_ms": meta.get("server_ms"), "sample_rate": SAMPLE_RATE,
                     "language_dropped": bool(meta.get("language_dropped"))}
    pcm, info = bench.tts(text, voice_id, model, language, budget=budget)
    info = dict(info, cached=False, key=key)
    if cache is not None:
        cache.put(key, pcm, {"model": model, "voice_id": voice_id, "language": language, "text": text,
                             "chars": info.get("chars"), "server_ms": info.get("server_ms"),
                             "language_dropped": bool(info.get("language_dropped")),
                             "sample_rate": SAMPLE_RATE, "fake": bool(info.get("fake")),
                             "created": datetime.now().isoformat(timespec="seconds")})
    return pcm, info


# -- the real client ------------------------------------------------------------------------

_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


def clean_translation(text, family=""):
    """What the app speaks (action_api.strip_think + interpreter/translate._clean): a leading
    <think> block dropped (and, for a DeepSeek / Qwen answer, everything up to an orphan
    </think>), then no wrapping quotes."""
    t = text or ""
    lead = t.lstrip()
    if lead.startswith(_THINK_OPEN):
        end = lead.find(_THINK_CLOSE)
        t = "" if end < 0 else lead[end + len(_THINK_CLOSE):]
    elif (family or "").lower() in ("deepseek", "qwen") and _THINK_CLOSE in t:
        t = t[t.find(_THINK_CLOSE) + len(_THINK_CLOSE):]
    t = t.strip()
    for a, b in (('"', '"'), ("“", "”"), ("«", "»"), ("「", "」")):
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            t = t[1:-1].strip()
    return t


class BenchClient:
    """POSTs {"action": ...} to the bench function with the x-bench-token header.

    Retries 429 / 5xx and dropped connections with backoff (at most ``retries`` more tries,
    honouring Retry-After), then raises BenchError with a plain message."""

    fake = False

    def __init__(self, url=DEFAULT_URL, token=None, *, retries=3, backoff=0.8, timeouts=None,
                 ledger=LEDGER_FILE, purpose="bench", trust_env=True, verbose=False):
        import requests                        # only real runs need it
        self._requests = requests
        self.url = url or DEFAULT_URL
        self._token = token if token is not None else load_token()
        self.retries = int(retries)
        self.backoff = float(backoff)
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))
        self.ledger = CharLedger(ledger) if isinstance(ledger, (str, Path)) else ledger
        self.purpose = purpose
        self.trust_env = trust_env
        self.verbose = verbose
        self._local = threading.local()
        self.calls = collections.Counter()
        self.chars = 0                        # TTS characters billed through this client
        self.chars_unsure = 0                 # ... and those of failed tries that may have been billed

    # -- plumbing ---------------------------------------------------------------------------
    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._requests.Session()
            s.trust_env = self.trust_env
            self._local.session = s
        return s

    def _delay(self, attempt, retry_after=None):
        if retry_after is not None:
            try:
                return min(20.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return self.backoff * (2 ** attempt) + random.uniform(0, self.backoff / 4)

    def _explain(self, action, status, data, text):
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error") or data.get("message") or "")
            if data.get("detail"):
                err += f" ({data['detail']})"
        err = err or (text or "")[:200].strip()
        if status == 401:
            return ("the bench function refused the token (HTTP 401). Put the token you deployed with in "
                    f"{TOKEN_FILE} or set {TOKEN_ENV}.")
        if status == 404:
            return f"no bench function at {self.url} (HTTP 404): deploy server/index.ts as speed-bench first (README)."
        if status == 410:
            return "the bench function is stubbed (HTTP 410): deploy server/index.ts again first (README)."
        if status == 413:
            return f"{action}: request too large (HTTP 413) - send shorter audio."
        return f"{action} failed: HTTP {status}: {err or 'no details'}"

    def _check_token(self, action):
        if not self._token:
            raise BenchError(f"no bench token: set {TOKEN_ENV} or write it to {TOKEN_FILE}", action=action,
                             code=NO_TOKEN)

    def last_attempt(self):
        """{"attempts", "attempt_t0", "retry_wait_s"} of this thread's last request: how many tries it
        took, when the successful one started (perf_counter) and the time lost to the failed ones."""
        return dict(getattr(self._local, "last", None) or {"attempts": 0, "attempt_t0": None, "retry_wait_s": 0.0})

    def _request(self, action, body, timeout=None, stream=False, on_fail=None, retries=None):
        """POST with retries -> a successful requests.Response (status < 400).

        timeout: the read timeout in seconds, or a (connect, read) tuple; retries: this call's
        number of retries (default: the client's). on_fail(status, code, exc, will_retry) is
        called after every failed attempt (status None and exc set when no answer came); it may
        raise to stop the retries."""
        self._check_token(action)
        rq = self._requests
        payload = dict(body, action=action)
        headers = {"x-bench-token": self._token, "Content-Type": "application/json"}
        t = timeout or self.timeouts.get(action, 60)
        t = tuple(t) if isinstance(t, (tuple, list)) else (10, t)
        tries = self.retries if retries is None else max(0, int(retries))
        last = None
        t_start = time.perf_counter()
        for attempt in range(tries + 1):
            self.calls[action] += 1
            t_try = time.perf_counter()
            self._local.last = {"attempts": attempt + 1, "attempt_t0": t_try, "retry_wait_s": round(t_try - t_start, 4)}
            try:
                resp = self._session().post(self.url, json=payload, headers=headers, timeout=t, stream=stream)
            except (rq.ConnectionError, rq.Timeout) as e:
                last = BenchError(f"{action}: no answer from the bench function ({type(e).__name__}: {e})",
                                  action=action)
                retry = attempt < tries
                if on_fail is not None:
                    on_fail(None, "", e, retry)
                if retry:
                    time.sleep(self._delay(attempt))
                    continue
                raise last from e
            if resp.status_code < 400:
                return resp
            try:
                data = resp.json()
            except ValueError:
                data = None
            text = "" if data is not None else (resp.text or "")
            resp.close()
            code = str((data or {}).get("error") or "") if isinstance(data, dict) else ""
            last = BenchError(self._explain(action, resp.status_code, data, text), action=action,
                              status=resp.status_code, code=code, body=data)
            retry = retryable(resp.status_code, code) and attempt < tries
            if on_fail is not None:
                on_fail(resp.status_code, code, None, retry)
            if retry:
                if self.verbose:
                    print(f"  {action}: HTTP {resp.status_code}, retrying", file=sys.stderr)
                time.sleep(self._delay(attempt, resp.headers.get("Retry-After")))
                continue
            raise last
        raise last  # pragma: no cover

    def _call(self, action, body, timeout=None, on_fail=None):
        resp = self._request(action, body, timeout, on_fail=on_fail)
        try:
            data = resp.json()
        except ValueError:
            raise BenchError(f"{action}: the bench function did not answer JSON: {resp.text[:200]!r}",
                             action=action, status=resp.status_code)
        if isinstance(data, dict) and data.get("error"):
            raise BenchError(f"{action} failed: {data.get('error')} {data.get('detail') or ''}".strip(),
                             action=action, status=resp.status_code, code=str(data.get("error")), body=data)
        return data

    # -- the API ----------------------------------------------------------------------------
    def ping(self):
        return self._call("ping", {})

    def _ledger_add(self, n, purpose):
        if self.ledger is None or n <= 0:
            return
        try:
            self.ledger.add(n, purpose)
        except OSError as e:
            print(f"  (could not write the character ledger: {e})", file=sys.stderr)

    def tts(self, text, voice_id, model="eleven_v3", language=None, *, budget, **extra):
        """-> (16 kHz mono s16le PCM bytes, info). ``budget`` (a CharBudget) is required: it is
        charged before anything is sent and refunded when ElevenLabs surely billed nothing.

        An attempt that failed after the text may have been voiced (a timeout, a dropped
        connection, a crash) counts as spent: its retry is charged again (BudgetExceeded stops
        the retries) and the ledger notes those characters as "<purpose> unsure".
        extra: voice_settings, seed (passed to the function as they are)."""
        if not text or not text.strip():
            raise ValueError("tts: empty text")
        if len(text) > MAX_TTS_CHARS:
            raise ValueError(f"tts: {len(text)} characters, the bench allows {MAX_TTS_CHARS} per call")
        if budget is None:
            raise ValueError("tts: pass budget=CharBudget(...) - every voice call is counted")
        self._check_token("tts")                 # before charging: nothing is sent without a token
        n = len(text)
        budget.charge(n)
        acct = {"unsure": 0, "last_maybe": False}

        def failed(status, code, exc, will_retry):
            acct["last_maybe"] = maybe_billed(status, code, exc)
            if acct["last_maybe"]:
                acct["unsure"] += n
                if will_retry:
                    budget.charge(n)             # the next try is paid again
        try:
            data = self._call("tts", dict(extra, text=text, voice_id=voice_id, model=model, language=language),
                              on_fail=failed)
        except BudgetExceeded:
            self._ledger_add(acct["unsure"], f"{self.purpose} unsure")
            raise
        except BenchError as e:
            if e.status is not None and e.status < 400:
                acct["unsure"] += n              # a 200 without usable audio: it may have been voiced
            elif not acct["last_maybe"]:
                budget.refund(n)                 # the last try surely voiced nothing
            self._ledger_add(acct["unsure"], f"{self.purpose} unsure")
            raise
        attempt = self.last_attempt()
        client_ms = round((time.perf_counter() - attempt["attempt_t0"]) * 1000)
        pcm = base64.b64decode(data.get("audio") or "")
        if not pcm:
            self._ledger_add(acct["unsure"] + n, f"{self.purpose} unsure")
            raise BenchError("tts: the bench function sent no audio", action="tts")
        sr = int(data.get("sample_rate") or SAMPLE_RATE)
        info = {k: v for k, v in data.items() if k != "audio"}
        info.update(sample_rate=SAMPLE_RATE, server_ms=data.get("server_ms"), client_ms=client_ms,
                    model=data.get("model") or model, voice_id=voice_id, language=language,
                    attempts=attempt["attempts"], unsure_chars=acct["unsure"])
        if sr != SAMPLE_RATE:
            pcm = benchaudio.to_pcm16(benchaudio.resample(benchaudio.from_pcm16(pcm), sr, SAMPLE_RATE))
            info["resampled_from"] = sr
        chars = int(data.get("chars") or n)
        info["chars"] = chars
        if chars > n:
            budget.add(chars - n)
        self.chars += chars
        self.chars_unsure += acct["unsure"]
        billed = data.get("billed_chars")
        self._ledger_add(int(billed) if isinstance(billed, (int, float)) and billed > 0 else chars, self.purpose)
        self._ledger_add(acct["unsure"], f"{self.purpose} unsure")
        return pcm, info

    def tokens(self, kinds, n=1):
        """The server's {"tokens": {kind: [{"token": str}, ...]}}; each kind is also a top-level
        key of the returned dict for convenience."""
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        data = self._call("tokens", {"kinds": kinds, "n": int(n)})
        toks = data.get("tokens") or {}
        out = {"tokens": toks}
        out.update(toks)
        return out

    def translate_stream(self, text, src, dst, context=(), glossary=(), route="default", on_delta=None,
                         timeout=None, model=None, retries=None, on_open=None):
        """Stream one phrase's translation. -> (full text, timings): first_delta_s (request
        sent -> the first words a caption would show, i.e. after a leaked <think> block),
        first_byte_s (-> the first streamed text of any kind), total_s, chars, raw (the text as
        streamed), tries / retry_wait_s (the times are those of the try that answered; a 429 /
        5xx retried before it is not in them), and what the function reports in headers:
        server_ms, route, model, answer_family, server_first_ms, budget_ms, variant, attempts
        (the engines that failed inside the function), max_tokens. The text is cleaned like the
        app cleans it. on_delta gets the raw pieces as they arrive.

        retries: this call's retries (the app itself makes ONE try per phrase: run_interpreter
        passes 0); timeout: read seconds or (connect, read); on_open(headers) is called once the
        answer's headers are in (the function sends them with the first words), before any delta."""
        body = {"text": text, "from": src, "to": dst,
                "context": [c for c in (context or ()) if c][-MAX_CONTEXT:],
                "glossary": [w for w in (glossary or ()) if w][:MAX_GLOSSARY], "route": route}
        if model:
            body["model"] = model
        resp = self._request("translate", body, timeout, stream=True, retries=retries)
        tries = self.last_attempt()
        t0 = tries["attempt_t0"]
        parts, first, first_byte, partial = [], None, None, False
        ctype = resp.headers.get("Content-Type") or ""
        family = resp.headers.get("X-Answer-Family") or ""
        try:
            if on_open is not None:
                on_open(resp.headers)
            if "application/json" in ctype:
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    raise BenchError(f"translate failed: {data.get('error')}", action="translate",
                                     status=resp.status_code, body=data)
                piece = str((data or {}).get("text") or (data or {}).get("translation") or "")
                first = first_byte = time.perf_counter() - t0
                if piece:
                    parts.append(piece)
                    if on_delta:
                        on_delta(piece)
            else:
                dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        piece = dec.decode(chunk) if chunk else ""
                        if not piece:
                            continue
                        now = time.perf_counter() - t0
                        if first_byte is None and piece.strip():
                            first_byte = now
                        parts.append(piece)
                        if first is None and piece.strip() and clean_translation("".join(parts), family):
                            first = now
                        if on_delta:
                            on_delta(piece)
                    tail = dec.decode(b"", final=True)
                    if tail:
                        parts.append(tail)
                        if on_delta:
                            on_delta(tail)
                except self._requests.RequestException as e:
                    if not parts:
                        raise BenchError(f"translate: the stream broke before any text ({e})", action="translate")
                    partial = True
        finally:
            resp.close()
        total = time.perf_counter() - t0
        raw = "".join(parts)
        full = clean_translation(raw, family)
        timings = {"first_delta_s": None if first is None else round(first, 4),
                   "first_byte_s": None if first_byte is None else round(first_byte, 4), "total_s": round(total, 4),
                   "chars": len(full), "raw": raw, "tries": tries["attempts"], "retry_wait_s": tries["retry_wait_s"]}
        for h, k in TRANSLATE_HEADERS:
            v = resp.headers.get(h)
            if v is None or v == "":
                continue
            if k == "attempts":
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            elif v.lstrip("-").isdigit():
                v = int(v)
            timings[k] = v
        if partial:
            timings["partial"] = True
        return full, timings

    def stt(self, wav_bytes, engine, language=None, timeout=None, **extra):
        """Batch speech-to-text of a WAV. -> the server's dict (text, language, language_sent, words,
        server_ms, provider_ms, engine_model, ...) plus client_ms (wall time of the try that
        answered, upload included), client_total_ms (with any retries) and tries.
        language: a code, None (auto) or "multi" (with extra languages=[...]); extra may also
        carry vocabulary=[...], diarize=True, model=..."""
        body = dict({k: v for k, v in extra.items() if v is not None},
                    audio=base64.b64encode(bytes(wav_bytes)).decode("ascii"), engine=engine, language=language)
        t0 = time.perf_counter()
        data = self._call("stt", body, timeout)
        now = time.perf_counter()
        tries = self.last_attempt()
        data = dict(data) if isinstance(data, dict) else {"text": str(data)}
        data["client_ms"] = round((now - (tries["attempt_t0"] or t0)) * 1000)
        data["client_total_ms"] = round((now - t0) * 1000)
        data["tries"] = tries["attempts"]
        data.setdefault("text", "")
        data.setdefault("words", None)
        data.setdefault("language", None)
        return data

    def judge(self, items, route=None, model=None):
        """LLM judge scores [{"adequacy", "fluency", "note"}] for items
        [{"src", "src_lang", "ref", "hyp", "tgt_lang"}], 20 per call."""
        items = list(items)
        out = []
        for i in range(0, len(items), JUDGE_BATCH):
            batch = items[i:i + JUDGE_BATCH]
            body = {"items": batch}
            if route:
                body["route"] = route
            if model:
                body["model"] = model
            data = self._call("judge", body)
            scores = list((data or {}).get("scores") or [])
            if len(scores) != len(batch):
                raise BenchError(f"judge: {len(scores)} scores for {len(batch)} items", action="judge", body=data)
            out.extend(scores)
        return out

    def usage(self):
        """The ElevenLabs account's characters this month: {used, limit, left, reset_unix, tier}
        (needs a key that may read the subscription; raises BenchError otherwise)."""
        return self._call("usage", {})


# -- the fake -------------------------------------------------------------------------------

def _seed(*parts):
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12], 16)


FAKE_ENGINE = {   # base word error rate, real-time factor, fixed overhead (s)
    "scribe_v2": (0.03, 0.06, 0.4), "voxtral": (0.05, 0.12, 0.6),
    "openai": (0.045, 0.10, 0.7), "gladia": (0.06, 0.15, 0.9),
}
FAKE_WEAK = {"openai": {"hy": 2.5, "ka": 2.0}, "voxtral": {"hy": 3.0, "ka": 3.0}, "gladia": {"hy": 1.5}}
FAKE_VOXTRAL_LANGS = {"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru"}   # as server/index.ts MISTRAL_LANGS
_FILLERS = ("uh", "the", "um", "a")


def _fake_pieces(token):
    """A written token as the fake engine's words: runs of Japanese / Chinese characters (written
    without spaces) in 2-character pieces, anything else in the token (a Latin word, a number)
    whole."""
    out, run, dense = [], "", None
    for ch in token + "\0":
        d = benchaudio._is_dense_char(ch)
        if run and (d != dense or ch == "\0"):
            out += [run[i:i + 2] for i in range(0, len(run), 2)] if dense else [run]
            run = ""
        if ch != "\0":
            run += ch
            dense = d
    return out


def _garble(word, rng):
    """A near miss: one letter dropped or two swapped (keeps character errors small)."""
    if len(word) <= 1:
        return word + word
    i = int(rng.integers(0, len(word) - 1))
    if rng.random() < 0.5:
        return word[:i] + word[i + 1:]
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


class FakeBench:
    """Offline stand-in for BenchClient: same methods, deterministic answers.

    tts         a voiced buzz, ~0.065 s per character, pitch by the voice's gender
    translate   "[dst] text" (or a translation from ``translations``: {text: str} or a callable)
    stt         the text of the turns registered for that exact audio (register_timeline),
                with a few injected errors per engine / language / noise; word timings from the
                timeline; leaked turns show up as echo words (speakers mode)
    judge       scores from chrF
    tokens      fake-<kind>-<n>
    simulate_latency=True sleeps the fake server times (scaled by latency_scale)."""

    fake = True

    def __init__(self, *, seed=0, voices=None, translations=None, simulate_latency=False,
                 latency_scale=1.0, error_scale=1.0):
        self.seed = seed
        self.translations = translations or {}
        self.simulate_latency = simulate_latency
        self.latency_scale = latency_scale
        self.error_scale = error_scale
        self.calls = collections.Counter()
        self.chars = 0
        self._gender = {k: v[1] for k, v in KNOWN_VOICES.items()}
        for v in voices or ():
            if v.get("id"):
                self._gender[v["id"]] = v.get("gender") or "neutral"
        self._audio = {}           # sha1 of the samples -> registration
        self._lock = threading.Lock()

    def _sleep(self, s):
        if self.simulate_latency and s > 0:
            time.sleep(s * self.latency_scale)

    # -- API --------------------------------------------------------------------------------
    def ping(self):
        self.calls["ping"] += 1
        return {"ok": True, "fake": True, "providers": {k: True for k in
                                                        ("elevenlabs", "llm", "openai", "mistral", "gladia")}}

    def tts(self, text, voice_id, model="eleven_v3", language=None, *, budget=None, **extra):
        if not text or not text.strip():
            raise ValueError("tts: empty text")
        if len(text) > MAX_TTS_CHARS:
            raise ValueError(f"tts: {len(text)} characters, the bench allows {MAX_TTS_CHARS} per call")
        if budget is not None:
            budget.charge(text)
        self.calls["tts"] += 1
        gender = self._gender.get(voice_id, "neutral")
        f0 = benchaudio.GENDER_F0.get(gender, 165.0) * (0.94 + 0.12 * (_seed(voice_id) % 1000) / 1000.0)
        x = benchaudio.synth_voice(text, f0=f0, seed=_seed(voice_id, text, self.seed))
        server_ms = int(150 + 1.5 * len(text))
        self._sleep(server_ms / 1000.0)
        with self._lock:
            self.chars += len(text)
        return benchaudio.to_pcm16(x), {"sample_rate": SAMPLE_RATE, "chars": len(text), "server_ms": server_ms,
                                        "client_ms": server_ms + 40, "model": model, "voice_id": voice_id,
                                        "language": language, "fake": True}

    def tokens(self, kinds, n=1):
        self.calls["tokens"] += 1
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        toks = {k: [{"token": f"fake-{k}-{i + 1}"} for i in range(int(n))] for k in kinds}
        out = {"tokens": toks, "fake": True}
        out.update(toks)
        return out

    def _translation(self, text, src, dst):
        tr = self.translations
        if callable(tr):
            got = tr(text, src, dst)
            if got:
                return got
        elif text in tr:
            return tr[text]
        elif (text, dst) in tr:
            return tr[(text, dst)]
        return f"[{dst}] {text}"

    def translate_stream(self, text, src, dst, context=(), glossary=(), route="default", on_delta=None,
                         timeout=None, model=None, retries=None, on_open=None):
        self.calls["translate"] += 1
        out = self._translation(text, src, dst)
        first = round(0.25 + 0.002 * len(text), 4)
        pieces = re.findall(r"\S+\s*", out) or [out]
        self._sleep(first)
        if on_open is not None:                 # the function sends its headers with the first words
            on_open({"Content-Type": "text/plain; charset=utf-8", "X-Bench-Route": route, "X-Bench-Model": "fake",
                     "X-Answer-Family": ""})
        for p in pieces:
            if on_delta:
                on_delta(p)
        total = round(first + 0.02 * len(pieces), 4)
        self._sleep(total - first)
        return clean_translation(out), {"first_delta_s": first, "first_byte_s": first, "total_s": total,
                                        "chars": len(out), "raw": out, "tries": 1, "retry_wait_s": 0.0,
                                        "route": route, "model": "fake", "server_ms": int(total * 1000) - 30,
                                        "fake": True}

    def usage(self):
        self.calls["usage"] += 1
        return {"used": self.chars, "limit": MONTHLY_CHARS, "left": MONTHLY_CHARS - self.chars, "fake": True}

    def judge(self, items, route=None, model=None):
        import metrics                       # local: metrics never imports this module
        self.calls["judge"] += 1
        out = []
        for it in items:
            score = metrics.chrf(it.get("ref") or "", it.get("hyp") or "", lang=it.get("tgt_lang"))
            out.append({"adequacy": int(round(min(100.0, score))), "fluency": 90 if (it.get("hyp") or "").strip() else 0,
                        "note": "fake judge (chrF)"})
        return out

    # -- fake speech-to-text ----------------------------------------------------------------
    def register_audio(self, wav_or_frames, turns, leak_turns=(), variant="clean", leak_delay=0.035):
        """Tell the fake STT what is said in this audio: ``turns`` (timeline turns) are the
        expected speech; ``leak_turns`` leak in through the room (speakers mode)."""
        frames = wav_or_frames
        if bytes(wav_or_frames[:4]) == b"RIFF":
            frames = benchaudio.wav_frames(wav_or_frames)
        key = hashlib.sha1(bytes(frames)).hexdigest()
        self._audio[key] = {"turns": [dict(t) for t in turns], "leak": [dict(t) for t in leak_turns],
                            "variant": variant, "leak_delay": leak_delay}
        return key

    def register_timeline(self, timeline, audio_dir):
        """Register every variant file of one conversation (timeline.json + its folder)."""
        audio_dir = Path(audio_dir)
        turns = timeline.get("turns") or []
        mic = [t for t in turns if t.get("track") == "mic"]
        system = [t for t in turns if t.get("track") != "mic"]
        n = 0
        for variant, files in (timeline.get("variants") or {}).items():
            for inp, name in (files or {}).items():
                if not isinstance(name, str):
                    continue
                path = audio_dir / name
                if not path.is_file():
                    continue
                expected = mic if inp == "mic" else system if inp == "system" else turns
                # the call heard through the room: extra words in the mic track (in the mix it is only
                # a quiet echo of speech that is there anyway)
                leak = system if (variant == "speakers" and inp == "mic") else []
                self.register_audio(path.read_bytes(), expected, leak, variant=variant)
                n += 1
        return n

    def stt(self, wav_bytes, engine, language=None, timeout=None, **extra):
        self.calls["stt"] += 1
        frames = benchaudio.wav_frames(wav_bytes)
        key = hashlib.sha1(frames).hexdigest()
        audio_s = len(frames) / 2.0 / SAMPLE_RATE
        base, rtf, overhead = FAKE_ENGINE.get(engine, (0.08, 0.2, 1.0))
        server_ms = int((overhead + rtf * audio_s) * 1000)
        self._sleep(server_ms / 1000.0)
        reg = self._audio.get(key)
        if reg is None:
            return {"text": "", "language": language, "words": [], "server_ms": server_ms,
                    "client_ms": server_ms + 80, "client_total_ms": server_ms + 80, "tries": 1,
                    "engine_model": f"fake-{engine}", "fake": True, "note": "audio not registered with the fake"}
        rng = np.random.default_rng(_seed(engine, key, language, self.seed))
        covered = None
        if engine == "voxtral" and language and language != "multi":
            covered = language in FAKE_VOXTRAL_LANGS
            if not covered:
                language = None                   # like the function: Voxtral is sent no language it lacks
        noisy = 1.6 if reg["variant"] == "noisy" else 1.0
        words, speakers = [], {}
        items = [(t, False) for t in reg["turns"]] + [(t, True) for t in reg["leak"]]
        items.sort(key=lambda it: float(it[0].get("start") or 0))
        for t, leaked in items:
            langs = t.get("langs") or [t.get("lang")]
            p = base * FAKE_WEAK.get(engine, {}).get(t.get("lang"), 1.0) * noisy * self.error_scale
            if language == "multi":
                p *= 1.2 if len(langs) > 1 else 1.0
            elif language and language not in langs:
                p *= 4.0
            elif len(langs) > 1:
                p *= 2.0 if language else 1.5
            p = min(p, 0.7)
            start, end = float(t.get("start") or 0), float(t.get("end") or 0)
            if leaked:
                start, end = start + reg["leak_delay"], end + reg["leak_delay"]
            spk = speakers.setdefault(t.get("speaker"), f"speaker_{len(speakers)}")
            src = []                                    # (piece, glued to the piece before)
            for w in re.sub(r"\[[^\]]{1,40}\]", " ", t.get("text") or "").split():
                src += [(piece, k > 0) for k, piece in enumerate(_fake_pieces(w))]
            out = []
            for w, glue in src:
                if leaked and rng.random() > 0.35:      # the room lets some of it through
                    continue
                r = rng.random()
                if r < p * 0.6:
                    out.append((_garble(w, rng), glue))
                elif r < p * 0.85:
                    continue
                elif r < p:
                    out += [(w, glue), (_FILLERS[int(rng.integers(0, len(_FILLERS)))], False)]
                else:
                    out.append((w, glue))
            step = (end - start) / max(1, len(out))
            for k, (w, glue) in enumerate(out):
                ws = start + k * step
                words.append({"text": w, "start": round(ws, 3), "end": round(ws + 0.9 * step, 3), "speaker": spk,
                              "_glue": glue})
        words.sort(key=lambda w: w["start"])
        text = "".join(("" if w.pop("_glue") or not i else " ") + w["text"] for i, w in enumerate(words))
        langs = collections.Counter(t.get("lang") for t in reg["turns"])
        detected = language if language and language != "multi" else (langs.most_common(1)[0][0] if langs else None)
        sent = language if language and language != "multi" else (
            ",".join(extra.get("languages") or []) or None if language == "multi" else None)
        out = {"text": text, "language": detected, "language_sent": sent, "words": words, "server_ms": server_ms,
               "client_ms": server_ms + 80, "client_total_ms": server_ms + 80, "tries": 1,
               "engine_model": f"fake-{engine}", "fake": True}
        if covered is not None:
            out["covered"] = covered
        return out


def make_bench(dry_run=False, url=None, purpose="bench", voices=None, **kw):
    """FakeBench for a dry run, else BenchClient (token from env / .bench_token)."""
    if dry_run:
        return FakeBench(voices=voices)
    return BenchClient(url or DEFAULT_URL, purpose=purpose, **kw)


# -- CLI ------------------------------------------------------------------------------------

def _utf8_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _utf8_console()
    ap = argparse.ArgumentParser(description="Bench function quick checks.")
    ap.add_argument("what", choices=("ping", "ledger", "usage"),
                    help="ping: is the function up, which providers it has; ledger: this month's characters "
                         "spent by the bench (local); usage: the ElevenLabs account's own count")
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args(argv)
    if args.what == "ledger":
        led = CharLedger()
        m = led.month()
        print(f"{datetime.now():%Y-%m}: {m.get('total', 0)} of {MONTHLY_CHARS} ElevenLabs characters used "
              f"({m.get('calls', 0)} calls) {json.dumps(m.get('by') or {})}")
        return 0
    try:
        t0 = time.perf_counter()
        client = BenchClient(args.url)
        data = client.usage() if args.what == "usage" else client.ping()
        print(json.dumps(data, indent=1), f"\n({(time.perf_counter() - t0) * 1000:.0f} ms)")
        return 0
    except BenchError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
