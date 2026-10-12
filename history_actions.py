"""Delete / export history (Gusan 2.0 · Settings › Privacy).

Integration notes for Wave 4
----------------------------
* "Delete all history…" on Settings › Privacy:
      keep = history_actions.active_meeting_dir(app)      # a meeting still recording
      n = history_actions.counts(keep)                   # {"dictations", "meetings"}
      ask: f"Permanently delete {history_actions.describe(n)}?" with
           "Delete everything" (include_meetings=True) and, when there are
           both, "Only dictations" (include_meetings=False); nothing to delete
           -> history_actions.EMPTY_TEXT
      res = history_actions.clear(include_meetings, keep, track=app.track)
      show history_actions.result_text(res), then refresh Home › Recent and
      the Meetings list (Wave 4: a sig_history_changed or the pages' refresh).
  clear() is quick (a JSON rewrite plus folder deletes); run it on a worker
  (api_keys.run_then) if there are many meetings with recordings.
* active_meeting_dir(app) asks app.meeting_session (active_dir(), or state +
  meeting_dir/folder for a duck-typed engine). The 1.x MeetingsWindow it also
  knew is retired.
* Export: export_csv(path) / export_txt(path) (history.export_*), with
  default_export_name(fmt) for the save dialog; track "history_exported"
  {"format", "count", "from": "settings"}.
"""

import os

EMPTY_TEXT = "History is already empty."
_ACTIVE_STATES = ("recording", "processing", "paused")


def _hist(hist):
    if hist is not None:
        return hist
    import history
    return history


def _store(store):
    if store is not None:
        return store
    import meeting_store
    return meeting_store


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def active_meeting_dir(app):
    """The folder of a meeting still being recorded or processed (never
    deleted), or None."""
    if app is None:
        return None
    session = getattr(app, "meeting_session", None)
    if session is not None:
        fn = getattr(session, "active_dir", None)
        if callable(fn):
            try:
                folder = fn()
            except Exception:
                folder = None
            if folder:
                return str(folder)
        elif getattr(session, "state", None) in _ACTIVE_STATES:
            for attr in ("meeting_dir", "folder", "_meeting_dir"):
                folder = getattr(session, attr, None)
                if folder:
                    return str(folder)
    return None


def _meetings(store, keep_dir):
    try:
        listed = store.list_meetings()
    except Exception:
        return []
    if not keep_dir:
        return list(listed)
    keep = _norm(keep_dir)
    return [m for m in listed if _norm(m["dir"]) != keep]


def counts(keep_dir=None, hist=None, store=None):
    """{"dictations": n, "meetings": n} that "Delete all history" would remove
    (the meeting in keep_dir is not counted)."""
    try:
        dictations = len(_hist(hist).load())
    except Exception:
        dictations = 0
    return {"dictations": dictations, "meetings": len(_meetings(_store(store), keep_dir))}


def clear(include_meetings, keep_dir=None, hist=None, store=None, track=None, source="settings"):
    """Delete every dictation and, with include_meetings, every meeting
    (notes, transcript, recording) except the one in keep_dir. Returns
    {"dictations": deleted, "meetings": deleted, "meetings_left": that
    couldn't be deleted, "kept_active": meetings were deleted but the one
    in keep_dir was spared}."""
    hist, store = _hist(hist), _store(store)
    try:
        dictations = len(hist.load())
    except Exception:
        dictations = 0
    hist.clear()
    deleted = left = 0
    if include_meetings:
        before = _meetings(store, keep_dir)
        try:
            store.delete_all_meetings(keep=keep_dir)
        except Exception:
            pass
        # Count what really went: a recording open in another app survives.
        after = {_norm(m["dir"]) for m in _meetings(store, None)}
        deleted = sum(1 for m in before if _norm(m["dir"]) not in after)
        left = len(before) - deleted
    # "The meeting you're recording was kept" only makes sense when meetings went.
    result = {"dictations": dictations, "meetings": deleted, "meetings_left": left,
              "kept_active": bool(keep_dir) and bool(include_meetings)}
    if track is not None:
        try:
            track("history_cleared", {"count": dictations, "meetings": deleted, "from": source})
        except Exception:
            pass
    return result


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(n):
    """{"dictations": 3, "meetings": 1} -> '3 dictations and 1 meeting'."""
    parts = []
    if n.get("dictations"):
        parts.append(_plural(n["dictations"], "dictation"))
    if n.get("meetings"):
        parts.append(_plural(n["meetings"], "meeting"))
    return " and ".join(parts)


def result_text(res):
    """The note after clearing."""
    done = describe(res) or "nothing"
    text = f"Deleted {done}."
    if res.get("meetings_left"):
        text += " Some meetings couldn't be deleted - a recording may be open in another app."
    if res.get("kept_active"):
        text += " The meeting you're recording was kept."
    return text


def default_export_name(fmt):
    return f"gusan_history.{'csv' if fmt == 'csv' else 'txt'}"


def export_csv(path, entries=None, hist=None):
    """Write dictations to a CSV; returns how many."""
    return _hist(hist).export_csv(path, entries)


def export_txt(path, entries=None, hist=None):
    """Write dictations to a text file; returns how many."""
    return _hist(hist).export_txt(path, entries)
