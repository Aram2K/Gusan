"""What's new: the short list Gusan shows ONCE after it was updated (ui/whats_new_window.py), and again from
Settings > Updates & about ("What's new"). Qt-free.

The founder (2026-11-09): when people update, show what has changed - simple yet informative, clear whether
something is a new feature, something better or a fix, nicely presented.

NOTES holds, per release, a handful of items (kind, title, one line). Write them with each release, next to its
docs/release-notes file - plain words, what the person notices, never "limit" or "Armenia(n)" (the release-notes
rules apply). Kinds: "new" (a feature), "better" (something that works better), "fixed" (a bug fix).

When it shows (due):
  * cfg ``whats_new_seen`` is the version whose notes were last shown (or that was running when it was first set).
  * Newer than that and there are notes for the versions in between: shown once - every version since, newest
    first. Someone who skipped releases sees what each one brought.
  * A fresh install (``onboarding_done`` still False) shows nothing: the welcome does that job.
  * An existing user who never had the key (updated from a version before this): the notes of the version they
    updated from onward when the install handed over said where from (cfg update_attempt "from"), else this
    version's.
"""

SEEN_KEY = "whats_new_seen"

KINDS = {
    # kind -> (chip word, icon, colour family: "<family>-tx" text, "<family>-sub" tile)
    "new": ("New", "sparkle", "pro"),
    "better": ("Better", "bolt", "blue"),
    "fixed": ("Fixed", "check", "ok"),
}

NOTES = (
    ("2.1.6", (
        ("new", "Live captions in meetings",
         "Your call's words at the bottom of the screen, as spoken or in your language."),
        ("new", "Transcribes with, for meetings",
         "Pick how meetings are transcribed for each language. Captions follow it."),
        ("new", "Press or hold",
         "On Windows: press to start and stop, or hold the shortcut while you talk."),
        ("better", "Smart Actions show one step at a time",
         "Transcribing, searching the web, summarizing: one icon at a time, in order."),
        ("better", "Your last words are kept",
         "Dictation listens half a second longer, so the end of a sentence is not cut."),
        ("fixed", "Every result stays on your clipboard",
         "If a paste does not land, press Ctrl+V and it is there."),
    )),
    ("2.1.5", (
        ("fixed", "Best accuracy sticks in Smart Actions",
         "Choosing Best accuracy under Transcribes with no longer goes back to Fast."),
        ("fixed", "Lists open smoothly", "Every dropdown opens in place at once, without shaking."),
        ("better", "Speed and accuracy for Gusan Cloud",
         "Fast and Best accuracy show their dots, like the models on this computer."),
        ("better", "A shorter dictation history",
         "Today and yesterday stay in view; everything older waits under Earlier."),
    )),
    ("2.1.4", (
        ("new", "Choose your avatar",
         "Hover your avatar in Account & plan: pick a colour, a picture or your own photo."),
        ("new", "Updates right in Settings",
         "The download shows in Updates & about, and this window tells you what changed."),
        ("better", "Best accuracy is truly the most accurate",
         "It writes with ElevenLabs Scribe v2 first, not with the live stream."),
        ("better", "Gusan Cloud first in every list",
         "Each model list starts with Gusan Cloud and shows the model that really runs."),
        ("fixed", "Notes in your meeting's language",
         "A few words in another language no longer switch the notes to it."),
        ("fixed", "A sharp avatar", "The small avatar is no longer cut at its edges."),
    )),
    ("2.1.3", (
        ("better", "Shortcuts never type a letter",
         "Alt+S and the other shortcuts no longer type their letter into the app in front."),
        ("new", "Best accuracy in Smart Actions", "Pick Fast or Best accuracy under Transcribes with."),
        ("new", "A clear message when your microphone is silent",
         "If it stops sending sound, Gusan says so after a couple of seconds."),
        ("better", "Clean lists and a real record button",
         "Every dropdown is one rounded card; the record button sinks in when you press it."),
        ("fixed", "The web, without excuses",
         "Smart Actions and the Live Assistant search when you ask, and never say they can't browse."),
    )),
    ("2.1.2", (
        ("new", "Real time", "With Gusan Cloud a dictation is ready the moment you stop."),
        ("new", "Smart Actions on your selection", "Select text, press the shortcut and say what to do with it."),
        ("new", "Search the web", "Smart Actions, the Live Assistant and the meeting chat can look things up."),
        ("new", "Meetings with real names", "Every meeting gets a short title, and the list is grouped by day."),
        ("fixed", "Updating at Quit no longer freezes the window", ""),
    )),
)


def _v(value):
    try:
        parts = [int(x) for x in str(value or "").strip().lstrip("vV").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])
    except (TypeError, ValueError):
        return (0, 0, 0)


def sections(previous, current):
    """[(version, items)] newest first: the releases after ``previous`` up to ``current`` that have notes.
    ``previous`` "" = just ``current``'s own."""
    cur = _v(current)
    prev = _v(previous) if previous else None
    out = []
    for version, items in NOTES:
        v = _v(version)
        if v > cur:
            continue
        if prev is None:
            if v == cur:
                out.append((version, list(items)))
            continue
        if v > prev:
            out.append((version, list(items)))
    return out


def due(cfg, current):
    """(previous, sections) to show now after an update, or None. Never on a fresh install; never twice."""
    cfg = cfg or {}
    seen = str(cfg.get(SEEN_KEY) or "").strip()
    if seen:
        if _v(current) <= _v(seen):
            return None
        previous = seen
    else:
        if not cfg.get("onboarding_done", False):
            return None                 # a fresh install: the welcome shows what Gusan does
        attempt = cfg.get("update_attempt") if isinstance(cfg.get("update_attempt"), dict) else {}
        previous = str(attempt.get("from") or "")
        if previous and _v(previous) >= _v(current):
            return None
    found = sections(previous, current)
    return (previous, found) if found else None


def all_sections(current):
    """Everything up to ``current``, newest first (Settings' "What's new")."""
    return [(v, list(items)) for v, items in NOTES if _v(v) <= _v(current)]


def count(found):
    return sum(len(items) for _v_, items in found)
