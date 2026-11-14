"""The conversation in another language (founder 2026-11-14: "if I have a caption on and I have a language that I
want the caption on ... if it's not the same as the original language, I want to see original or the language that
has been chosen" - during the call and on the saved meeting).

When live captions are on with a language (captions.CFG_ON + captions.CFG_LANGUAGE; Pro, never in Privacy mode: a
translation sends the words to Gusan Cloud), the meeting's words are also translated into it:

  * during the call, every line of the live transcript, a few at once (Translator); the live view switches
    between Original and the language;
  * after Stop, the saved transcript's paragraphs (translate_saved), kept in the meeting's folder as
    translation.json: {"language": "fr", "lines": {"<a paragraph as spoken>": "<its translation>"}}. Keyed by
    the paragraph itself: the transcript is rewritten when the speakers get their names, its paragraphs stay.

A line that has no translation (yet, or it failed) shows as spoken. The server's "interpret" mode
(action_api.interpret_phrase) translates only - a phrase already in the language comes back unchanged.
Qt-free.
"""
import collections
import json
import logging
import os
import threading

import captions
import rich_text

logger = logging.getLogger("transcribe")

FILE = "translation.json"
WORKERS = 3                 # translations on their way at once
CONTEXT = 2                 # earlier lines sent as context only
MAX_PARAGRAPHS = 600        # of a saved transcript (a paragraph is up to ~550 characters: hours of talk)
MAX_CHARS = 1400            # one request (the server takes 1500)


def target(cfg, pro):
    """The language the conversation is also kept in: the caption language while live captions are on - "" when
    there is none, without Pro, or in Privacy mode."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not pro or cfg.get("privacy_mode") or not cfg.get(captions.CFG_ON):
        return ""
    code = str(cfg.get(captions.CFG_LANGUAGE) or "").strip()
    return code if any(code == c for c, _name in captions.LANGUAGES if c) else ""


def language_name(code):
    """"French" for "fr" (the caption languages' names); the code itself when unknown."""
    return next((name for c, name in captions.LANGUAGES if c and c == code), str(code or ""))


def rows(text):
    """The saved transcript as the pages show it: [(label, body, marker)] - the speaker on the first paragraph of
    a turn, a long turn packed into readable paragraphs, ``marker`` True for a '- Resumed at 11:20 -' line. (The one
    reading of transcript.txt: ui.pages.meeting_views.transcript_rows is this.)"""
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        for i, para in enumerate(rich_text.transcript_paragraphs(line)):
            if rich_text.is_marker(para):
                out.append(("", para, True))
            elif i == 0:
                label, body = rich_text.split_speaker(para)
                out.append((label, body, False))
            else:
                out.append(("", para, False))
    return out


def paragraphs(text):
    """What of a saved transcript is translated: each paragraph's words, once, in order."""
    out, seen = [], set()
    for _label, body, marker in rows(text):
        if marker or not body.strip() or body in seen:
            continue
        seen.add(body)
        out.append(body)
    return out


def load(folder):
    """{"language": code, "lines": {paragraph: translation}} of a meeting folder, or None."""
    try:
        with open(os.path.join(str(folder), FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("lines"), dict) or not data.get("language"):
        return None
    lines = {str(k): str(v) for k, v in data["lines"].items() if isinstance(v, str) and v.strip()}
    return {"language": str(data["language"]), "lines": lines}


def save(folder, language, lines):
    """Write translation.json (the old file or the new one, whatever happens). True when written."""
    path = os.path.join(str(folder), FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "language": str(language), "lines": dict(lines)}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.warning("Could not save the translated transcript", exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _one(translate, text, language, context):
    """One translation, "" when it failed or says nothing. Never raises."""
    try:
        return str(translate(str(text)[:MAX_CHARS], "auto", language, list(context)[-CONTEXT:]) or "").strip()
    except Exception as e:
        logger.info("A line could not be translated (%s): shown as spoken",
                    getattr(e, "code", "") or type(e).__name__)
        return ""


class Translator:
    """The live transcript's lines, translated in the background, WORKERS at once.

    ``translate(text, source_lang, target_lang, context)`` returns the translation (it may raise).
    ``on_done(key, text, translation)`` is called from a worker thread for every line - "" when it failed."""

    def __init__(self, translate, language, on_done, workers=WORKERS, start=True):
        self.language = str(language or "")
        self._translate = translate
        self._on_done = on_done
        self._queue = collections.deque()
        self._more = threading.Condition()
        self._said = collections.deque(maxlen=CONTEXT)
        self._closed = False
        self._threads = []
        if start:
            for n in range(max(1, int(workers))):
                t = threading.Thread(target=self._work, daemon=True, name="gusan-meeting-translate-%d" % (n + 1))
                t.start()
                self._threads.append(t)

    def submit(self, key, text):
        """Translate ``text`` (a line's words); the answer comes to on_done with ``key``."""
        text = str(text or "").strip()
        if not text or self._closed:
            return
        with self._more:
            self._queue.append((key, text, list(self._said)))
            self._said.append(text)
            self._more.notify()

    def pending(self):
        with self._more:
            return len(self._queue)

    def _take(self):
        with self._more:
            while not self._queue and not self._closed:
                self._more.wait(0.5)
            return None if self._closed else self._queue.popleft()

    def _work(self):
        while True:
            item = self._take()
            if item is None:
                return
            self._run(item)

    def _run(self, item):
        key, text, context = item
        translation = _one(self._translate, text, self.language, context)
        if self._closed:
            return
        try:
            self._on_done(key, text, translation)
        except Exception:
            logger.debug("translated line not delivered", exc_info=True)

    def drain(self):
        """Tests: translate what is queued, on this thread."""
        while True:
            with self._more:
                if not self._queue:
                    return
                item = self._queue.popleft()
            self._run(item)

    def close(self):
        self._closed = True
        with self._more:
            self._queue.clear()
            self._more.notify_all()


def translate_saved(folder, language, translate, transcript=None, workers=WORKERS, cancel=None):
    """Translate the saved transcript of ``folder`` into ``language`` and keep it (translation.json). Paragraphs
    already translated into that language are kept (a transcript rewritten with the speakers' names costs
    nothing again). ``cancel()`` True stops between paragraphs. Returns how many paragraphs have a translation."""
    if transcript is None:
        try:
            with open(os.path.join(str(folder), "transcript.txt"), encoding="utf-8") as f:
                transcript = f.read()
        except OSError:
            return 0
    todo = paragraphs(transcript)[:MAX_PARAGRAPHS]
    old = load(folder)
    done = dict(old["lines"]) if old and old["language"] == language else {}
    done = {k: v for k, v in done.items() if k in set(todo)}
    missing = [(i, p) for i, p in enumerate(todo) if p not in done]
    lock = threading.Lock()
    it = iter(missing)

    def work():
        while True:
            if cancel is not None and cancel():
                return
            with lock:
                nxt = next(it, None)
            if nxt is None:
                return
            i, para = nxt
            got = _one(translate, para, language, todo[max(0, i - CONTEXT):i])
            if got:
                with lock:
                    done[para] = got

    threads = [threading.Thread(target=work, daemon=True, name="gusan-transcript-translate-%d" % (n + 1))
               for n in range(max(1, min(int(workers), len(missing))))] if missing else []
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if done or old is None:
        save(folder, language, done)
    return len(done)


__all__ = ["CONTEXT", "FILE", "MAX_PARAGRAPHS", "Translator", "WORKERS", "language_name", "load", "paragraphs",
           "rows", "save", "target", "translate_saved"]
