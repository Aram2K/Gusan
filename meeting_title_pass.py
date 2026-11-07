"""A quiet pass that names the meetings saved before meetings were named.

Gusan names a new meeting from its notes (meeting_session._name_meeting). A meeting saved earlier is still called
"Meeting · Tue 14:30" or "Untitled Meeting": this pass gives those a name too, newest first, a few per run, one short
call each to the engine that writes the person's notes. It runs at most once a launch, only while nothing records,
converts or answers, and it never costs the person anything they did not choose to spend:

  * not in Privacy mode (nothing leaves the computer);
  * not for a Free person on Gusan Cloud (a call would take the month's one cloud meeting note);
  * not with an AI model on this computer (it would hold that model, and the person's dictation waits for it);
  * not with the built-in formatter (no AI to ask).

A call that fails ends the pass (offline: the next launch tries again, nothing is marked); an answer that is not a
title marks that meeting as tried, so it is not asked again.
"""
import logging
import time

import actions
import meeting_store
import meeting_titles

logger = logging.getLogger(__name__)

PASS_MAX = 25                 # meetings named in one run
PAUSE_S = 0.4                 # between two calls: never a burst
NOTES_MIN_CHARS = 40          # notes shorter than this say too little to name a meeting by


def allowed_engine(engine, config):
    """True when ``engine`` (with ``config``, as AppController._resolve_action_engine returns them) may name old
    meetings in the background - see the module's list of what never does."""
    config = config or {}
    if config.get("privacy_mode") or config.get("_live_session_id"):
        return False
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if kind == "managed":
        return bool(config.get("_managed_token"))            # set for Pro only: Free would spend its one cloud note
    if kind == "cloud":
        return bool(actions.engine_has_key(engine, config))
    return False


def candidates(meetings, limit=PASS_MAX):
    """The meetings to name, newest first: notes written, a placeholder title, nobody named it, not tried before."""
    out = []
    for m in meetings:
        if not m.get("has_notes") or not meeting_titles.wants_title(m):
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def run(engine, config, should_stop=None, limit=PASS_MAX, pause=PAUSE_S, process=None, meetings=None):
    """Name up to ``limit`` unnamed meetings. ``should_stop()`` is asked before each call (a recording began, the app
    is quitting): True ends the pass. Returns how many meetings got a name. Never raises. Worker thread."""
    process = process or actions.process
    named = 0
    try:
        if not allowed_engine(engine, config):
            return 0
        items = meeting_store.list_meetings() if meetings is None else meetings
        for m in candidates(items, limit):
            if should_stop is not None and should_stop():
                break
            folder = m.get("dir")
            notes = meeting_store.load_notes(folder)
            if len((notes or "").strip()) < NOTES_MIN_CHARS:
                meeting_store.mark_title_tried(folder)       # too little to name it by: not worth asking again
                continue
            try:
                raw = process(meeting_titles.title_input(notes), actions.ACTION_MEETING_TITLE,
                              source_lang=m.get("language") or "auto", target_lang="en",
                              model=engine, config=dict(config or {}))
            except Exception:
                logger.info("Naming old meetings stopped: the AI could not be reached", exc_info=True)
                break                                         # offline, a limit, a bad key: the next launch tries again
            title = meeting_titles.clean_title(raw)
            if not title:
                meeting_store.mark_title_tried(folder)
            elif meeting_store.set_auto_title(folder, title):
                named += 1
            if pause:
                time.sleep(pause)
    except Exception:
        logger.warning("Naming old meetings failed", exc_info=True)
    return named
